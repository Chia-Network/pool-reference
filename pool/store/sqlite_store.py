from pathlib import Path
from typing import Optional, Set, List, Tuple, Dict

import aiosqlite
from blspy import G1Element
from chia.pools.pool_wallet_info import PoolState
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_spend import CoinSpend
from chia.util.ints import uint64

from .abstract import AbstractPoolStore
from ..record import FarmerRecord
from ..util import RequestMetadata
from ..pay_record import PaymentRecord

class SqlitePoolStore(AbstractPoolStore):
    """
    Pool store based on SQLite.
    """

    def __init__(self, db_path: Path = Path("pooldb.sqlite")):
        super().__init__()
        self.db_path = db_path
        self.connection: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self.connection = await aiosqlite.connect(self.db_path)
        await self.connection.execute("pragma journal_mode=wal")
        await self.connection.execute("pragma synchronous=2")
        await self.connection.execute(
            (
                "CREATE TABLE IF NOT EXISTS farmer("
                "launcher_id text PRIMARY KEY,"
                " p2_singleton_puzzle_hash text,"
                " delay_time bigint,"
                " delay_puzzle_hash text,"
                " authentication_public_key text,"
                " singleton_tip blob,"
                " singleton_tip_state blob,"
                " points bigint,"
                " difficulty bigint,"
                " payout_instructions text,"
                " is_pool_member tinyint)"
            )
        )

        await self.connection.execute(
            "CREATE TABLE IF NOT EXISTS partial(launcher_id text, harvester_id text, timestamp bigint, difficulty bigint)"
        )

        await self.connection.execute("CREATE INDEX IF NOT EXISTS scan_ph on farmer(p2_singleton_puzzle_hash)")
        await self.connection.execute("CREATE INDEX IF NOT EXISTS timestamp_index on partial(timestamp)")
        await self.connection.execute("CREATE INDEX IF NOT EXISTS launcher_id_index on partial(launcher_id)")

        await self.connection.execute(
            (
                "CREATE TABLE IF NOT EXISTS payment("
                "launcher_id text,"
                "amount bigint,"
                "payment_type text,"
                "timestamp bigint,"
                "points bigint,"
                "txid text,"
                "note text)"
            )
        )

        await self.connection.execute("CREATE INDEX IF NOT EXISTS payment_launcher_index on payment(launcher_id)")

        # snapshot table to keep track of farmer's points
        await self.connection.execute(
            (
                "CREATE TABLE IF NOT EXISTS points_ss("
                "launcher_id text,"
                "points bigint,"
                "timestamp bigint)"
            )
        )
        await self.connection.execute("CREATE INDEX IF NOT EXISTS ss_launcher_id_index on points_ss(launcher_id)")

        await self.connection.commit()


    @staticmethod
    def _row_to_farmer_record(row) -> FarmerRecord:
        return FarmerRecord(
            bytes.fromhex(row[0]),
            bytes.fromhex(row[1]),
            row[2],
            bytes.fromhex(row[3]),
            G1Element.from_bytes(bytes.fromhex(row[4])),
            CoinSpend.from_bytes(row[5]),
            PoolState.from_bytes(row[6]),
            row[7],
            row[8],
            row[9],
            True if row[10] == 1 else False,
        )

    async def add_farmer_record(self, farmer_record: FarmerRecord, metadata: RequestMetadata):
        cursor = await self.connection.execute(
            f"INSERT OR REPLACE INTO farmer VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                farmer_record.launcher_id.hex(),
                farmer_record.p2_singleton_puzzle_hash.hex(),
                farmer_record.delay_time,
                farmer_record.delay_puzzle_hash.hex(),
                bytes(farmer_record.authentication_public_key).hex(),
                bytes(farmer_record.singleton_tip),
                bytes(farmer_record.singleton_tip_state),
                farmer_record.points,
                farmer_record.difficulty,
                farmer_record.payout_instructions,
                int(farmer_record.is_pool_member),
            ),
        )
        await cursor.close()
        await self.connection.commit()

    async def get_farmer_record(self, launcher_id: bytes32) -> Optional[FarmerRecord]:
        # TODO(pool): use cache
        cursor = await self.connection.execute(
            "SELECT * from farmer where launcher_id=?",
            (launcher_id.hex(),),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_farmer_record(row)

    async def update_difficulty(self, launcher_id: bytes32, difficulty: uint64):
        cursor = await self.connection.execute(
            f"UPDATE farmer SET difficulty=? WHERE launcher_id=?", (difficulty, launcher_id.hex())
        )
        await cursor.close()
        await self.connection.commit()

    async def update_singleton(
        self,
        launcher_id: bytes32,
        singleton_tip: CoinSpend,
        singleton_tip_state: PoolState,
        is_pool_member: bool,
    ):
        if is_pool_member:
            entry = (bytes(singleton_tip), bytes(singleton_tip_state), 1, launcher_id.hex())
        else:
            entry = (bytes(singleton_tip), bytes(singleton_tip_state), 0, launcher_id.hex())
        cursor = await self.connection.execute(
            f"UPDATE farmer SET singleton_tip=?, singleton_tip_state=?, is_pool_member=? WHERE launcher_id=?",
            entry,
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
        cursor = await self.connection.execute(f"SELECT points, payout_instructions from farmer")
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

    async def snapshot_farmer_points(self) -> None:
        await self.connection.execute(
            (
                "INSERT into points_ss(launcher_id, points, timestamp)"
                "SELECT launcher_id, points, datetime('now', 'unixepoch') from farmer"
                "WHERE points != 0"
            )
        )

    async def clear_farmer_points(self) -> None:
        cursor = await self.connection.execute(f"UPDATE farmer set points=0")
        await cursor.close()
        await self.connection.commit()

    async def add_partial(self, launcher_id: bytes32, harvester_id: bytes32, timestamp: uint64, difficulty: uint64):
        cursor = await self.connection.execute(
            "INSERT into partial VALUES(?, ?, ?, ?)",
            (launcher_id.hex(), harvester_id.hex(), timestamp, difficulty),
        )
        await cursor.close()
        cursor = await self.connection.execute(f"SELECT points from farmer where launcher_id=?", (launcher_id.hex(),))
        row = await cursor.fetchone()
        points = row[0]
        await cursor.close()
        cursor = await self.connection.execute(
            f"UPDATE farmer set points=? where launcher_id=?", (points + difficulty, launcher_id.hex())
        )
        await cursor.close()
        await self.connection.commit()

    async def get_recent_partials(self, launcher_id: bytes32, count: int) -> List[Tuple[uint64, uint64]]:
        cursor = await self.connection.execute(
            "SELECT timestamp, difficulty from partial WHERE launcher_id=? ORDER BY timestamp DESC LIMIT ?",
            (launcher_id.hex(), count),
        )
        rows = await cursor.fetchall()
        ret: List[Tuple[uint64, uint64]] = [(uint64(timestamp), uint64(difficulty)) for timestamp, difficulty in rows]
        return ret

    async def add_payment(self, payment: PaymentRecord):
        cursor = await self.connection.execute(
            f"INSERT into payment (launcher_id, amount, payment_type, timestamp, points, txid, note) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
		payment.launcher_id.hex(),
		payment.payment_amount,
		payment.payment_type,
		payment.timestamp,
		payment.points,
		payment.txid,
		payment.note
	    ),
        )
        await cursor.close()
        await self.connection.commit()
