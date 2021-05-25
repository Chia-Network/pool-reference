import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Set, List, Tuple, Dict

import aiosqlite
from blspy import G1Element
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.ints import uint32, uint64

from chia.util.streamable import streamable, Streamable


@dataclass(frozen=True)
@streamable
class FarmerRecord(Streamable):
    singleton_genesis: bytes32  # This uniquely identifies the singleton on the blockchain (ID for this farmer)
    authentication_public_key: G1Element  # This is the latest public key of the farmer (signs all partials)
    authentication_public_key_timestamp: uint64  # The timestamp of approval of the latest public key, by the owner key
    owner_public_key: G1Element  # The public key of the owner of the singleton, must be on the blockchain
    target_puzzle_hash: bytes32  # Target puzzle hash in the singleton
    relative_lock_height: uint32  # Relative lock height in the singleton
    p2_singleton_puzzle_hash: bytes32  # This is the puzzle hash in the plots, coinbase rewards get stored here
    blockchain_height: uint32  # Height of the singleton (might not be the last one)
    singleton_coin_id: bytes32  # Coin id of the singleton (might not be the last one)
    points: uint64  # Total points accumulated since last rest (or payout)
    difficulty: uint64  # Current difficulty for this farmer
    pool_payout_instructions: bytes  # This is where the pool will pay out rewards to the farmer
    is_pool_member: bool  # If the farmer leaves the pool, this gets set to False


class PoolStore:
    connection: aiosqlite.Connection
    lock: asyncio.Lock

    @classmethod
    async def create(cls):
        self = cls()
        self.db_path = Path("pooldb.sqlite")
        self.connection = await aiosqlite.connect(self.db_path)
        self.lock = asyncio.Lock()
        await self.connection.execute("pragma journal_mode=wal")
        await self.connection.execute("pragma synchronous=2")
        await self.connection.execute(
            (
                "CREATE TABLE IF NOT EXISTS farmer("
                "singleton_genesis text PRIMARY KEY,"
                " authentication_public_key text,"
                " authentication_public_key_timestamp bigint,"
                " owner_public_key text,"
                " target_puzzle_hash text,"
                " relative_lock_height bigint,"
                " p2_singleton_puzzle_hash text,"
                " blockchain_height bigint,"
                " singleton_coin_id text,"
                " points bigint,"
                " difficulty bigint,"
                " pool_payout_instructions text,"
                " is_pool_member tinyint)"
            )
        )

        await self.connection.execute(
            "CREATE TABLE IF NOT EXISTS partial(singleton_genesis text, timestamp bigint, difficulty bigint)"
        )

        await self.connection.execute("CREATE INDEX IF NOT EXISTS scan_ph on farmer(p2_singleton_puzzle_hash)")
        await self.connection.execute("CREATE INDEX IF NOT EXISTS timestamp_index on partial(timestamp)")
        await self.connection.execute(
            "CREATE INDEX IF NOT EXISTS singleton_genesis_index on partial(singleton_genesis)"
        )

        await self.connection.commit()

        return self

    @staticmethod
    def _row_to_farmer_record(row) -> FarmerRecord:
        return FarmerRecord(
            bytes.fromhex(row[0]),
            G1Element.from_bytes(bytes.fromhex(row[1])),
            row[2],
            G1Element.from_bytes(bytes.fromhex(row[3])),
            bytes.fromhex(row[4]),
            row[5],
            bytes.fromhex(row[6]),
            row[7],
            bytes.fromhex(row[8]),
            row[9],
            row[10],
            bytes.fromhex(row[11]),
            True if row[12] == 1 else False,
        )

    async def add_farmer_record(self, farmer_record: FarmerRecord):
        cursor = await self.connection.execute(
            f"INSERT OR REPLACE INTO farmer VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                farmer_record.singleton_genesis.hex(),
                bytes(farmer_record.authentication_public_key).hex(),
                farmer_record.authentication_public_key_timestamp,
                bytes(farmer_record.owner_public_key).hex(),
                farmer_record.target_puzzle_hash.hex(),
                farmer_record.relative_lock_height,
                farmer_record.p2_singleton_puzzle_hash.hex(),
                farmer_record.blockchain_height,
                farmer_record.singleton_coin_id.hex(),
                farmer_record.points,
                farmer_record.difficulty,
                farmer_record.pool_payout_instructions.hex(),
                int(farmer_record.is_pool_member),
            ),
        )
        await cursor.close()
        await self.connection.commit()

    async def get_farmer_record(self, singleton_genesis: bytes32) -> Optional[FarmerRecord]:
        # TODO: use cache
        cursor = await self.connection.execute(
            "SELECT * from farmer where singleton_genesis=?",
            (singleton_genesis.hex(),),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_farmer_record(row)

    async def update_difficulty(self, singleton_genesis: bytes32, difficulty: uint64):
        cursor = await self.connection.execute(
            f"UPDATE farmer SET difficulty=? WHERE singleton_genesis=?", (difficulty, singleton_genesis.hex())
        )
        await cursor.close()
        await self.connection.commit()

    async def get_pay_to_singleton_phs(self) -> Set[bytes32]:
        cursor = await self.connection.execute("SELECT p2_singleton_puzzle_hash from farmer")
        rows = await cursor.fetchall()

        all_phs: Set[bytes32] = set()
        for row in rows:
            all_phs.add(bytes32(bytes.fromhex(row[0])))
        return all_phs

    async def get_farmer_records_for_p2_singleton_phs(self, puzzle_hashes: Set[bytes32]) -> List[FarmerRecord]:
        if len(puzzle_hashes) == 0:
            return []
        puzzle_hashes_db = tuple([ph.hex() for ph in list(puzzle_hashes)])
        cursor = await self.connection.execute(
            f'SELECT * from farmer WHERE p2_singleton_puzzle_hash in ({"?," * (len(puzzle_hashes_db) - 1)}?) ',
            puzzle_hashes_db,
        )
        rows = await cursor.fetchall()
        return [self._row_to_farmer_record(row) for row in rows]

    async def get_farmer_points_and_payout_instructions(self) -> List[Tuple[uint64, bytes]]:
        cursor = await self.connection.execute(f"SELECT points, pool_payout_instructions from farmer")
        rows = await cursor.fetchall()
        accumulated: Dict[bytes32, uint64] = {}
        for row in rows:
            points: uint64 = uint64(row[0])
            ph: bytes32 = bytes32(bytes.fromhex(row[1]))
            if ph in accumulated:
                accumulated[ph] += points
            else:
                accumulated[ph] = points

        ret: List[Tuple[uint64, bytes32]] = []
        for ph, total_points in accumulated.items():
            ret.append((total_points, ph))
        return ret

    async def clear_farmer_points(self) -> None:
        cursor = await self.connection.execute(f"UPDATE farmer set points=0")
        await cursor.close()
        await self.connection.commit()

    async def add_partial(self, singleton_genesis: bytes32, timestamp: uint64, difficulty: uint64):
        cursor = await self.connection.execute(
            "INSERT into partial VALUES(?, ?, ?)",
            (singleton_genesis.hex(), timestamp, difficulty),
        )
        await cursor.close()
        await self.connection.commit()

    async def get_recent_partials(self, singleton_genesis: bytes32, count: int) -> List[Tuple[uint64, uint64]]:
        print(count, singleton_genesis.hex())
        cursor = await self.connection.execute(
            "SELECT timestamp, difficulty from partial WHERE singleton_genesis=? ORDER BY timestamp DESC LIMIT ?",
            (singleton_genesis.hex(), count),
        )
        rows = await cursor.fetchall()
        ret: List[Tuple[uint64, uint64]] = [(uint64(timestamp), uint64(difficulty)) for timestamp, difficulty in rows]
        return ret
