from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Set

import aiosqlite
from blspy import G1Element
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.ints import uint32, uint64

from chia.util.lru_cache import LRUCache
from chia.util.streamable import streamable, Streamable


@dataclass(frozen=True)
@streamable
class FarmerRecord(Streamable):
    singleton_genesis: bytes32
    owner_public_key: G1Element
    pool_puzzle_hash: bytes32
    relative_lock_height: uint32
    p2_singleton_puzzle_hash: bytes32
    blockchain_height: uint32  # Height of the singleton (might not be the last one)
    singleton_coin_id: bytes32  # Coin id of the singleton (might not be the last one)
    points: uint64
    difficulty: uint64


class PoolStore:
    connection: aiosqlite.Connection

    @classmethod
    async def create(cls):
        self = cls()
        self.db_path = Path("pooldb.sqlite")
        self.connection = await aiosqlite.connect(self.db_path)
        await self.connection.execute("pragma journal_mode=wal")
        await self.connection.execute("pragma synchronous=2")
        await self.connection.execute(
            (
                "CREATE TABLE IF NOT EXISTS farmer("
                "singleton_genesis text PRIMARY KEY,"
                " owner_public_key text,"
                " pool_puzzle_hash text,"
                " relative_lock_height bigint,"
                " p2_singleton_puzzle_hash text,"
                " blockchain_height bigint,"
                " singleton_coin_id text,"
                " points bigint,"
                " difficulty bigint)"
            )
        )

        # Useful for reorg lookups
        await self.connection.execute("CREATE INDEX IF NOT EXISTS scan_ph on farmer(p2_singleton_puzzle_hash)")

        await self.connection.commit()
        self.coin_record_cache = LRUCache(1000)

        return self

    async def add_farmer_record(self, farmer_record: FarmerRecord):
        cursor = await self.connection.execute(
            f"INSERT OR REPLACE INTO farmer VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                farmer_record.singleton_genesis.hex(),
                bytes(farmer_record.owner_public_key).hex(),
                farmer_record.pool_puzzle_hash.hex(),
                farmer_record.relative_lock_height,
                farmer_record.p2_singleton_puzzle_hash.hex(),
                farmer_record.blockchain_height,
                farmer_record.singleton_coin_id.hex(),
                farmer_record.points,
                farmer_record.difficulty,
            ),
        )
        await cursor.close()
        await self.connection.commit()

    async def get_farmer_record(self, singleton_genesis: bytes32) -> Optional[FarmerRecord]:
        # TODO: use cache
        cursor = await self.connection.execute(
            "SELECT * from farmer where singleton_genesis=?", (singleton_genesis.hex(),)
        )
        row = await cursor.fetchone()
        if row is None:
            return None

        return FarmerRecord(
            bytes.fromhex(row[0]),
            G1Element.from_bytes(bytes.fromhex(row[1])),
            bytes.fromhex(row[2]),
            row[3],
            bytes.fromhex(row[4]),
            row[5],
            bytes.fromhex(row[6]),
            row[7],
            row[8],
        )

    async def get_pay_to_singleton_phs(self) -> Set[bytes32]:
        cursor = await self.connection.execute("SELECT p2_singleton_puzzle_hash from farmer")
        rows = await cursor.fetchall()

        all_phs: Set[bytes32] = set()
        for row in rows:
            all_phs.add(bytes32(bytes.fromhex(row[0])))
        return all_phs
