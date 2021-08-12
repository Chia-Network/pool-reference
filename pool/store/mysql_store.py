from pathlib import Path
from typing import Optional, Set, List, Tuple, Dict
import pymysql
from blspy import G1Element
from chia.pools.pool_wallet_info import PoolState
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_spend import CoinSpend
from chia.util.ints import uint64
import logging
from .abstract import AbstractPoolStore
from ..record import FarmerRecord
from ..util import RequestMetadata


class MysqlPoolStore(AbstractPoolStore):
    """
    Pool store based on SQLite.
    """

    def __init__(self, db_path: Path = Path("pooldb.sqlite")):
        super().__init__()
        self.db_path = db_path
        self.log = logging.getLogger(__name__)
        self.connection: Optional[pymysql.Connection] = None

    async def connect(self):
        # self.connection = await aiosqlite.connect(self.db_path)
        # await self.connection.execute("pragma journal_mode=wal")
        # await self.connection.execute("pragma synchronous=2")
        # await self.connection.execute(
        #     (
        #         "CREATE TABLE IF NOT EXISTS farmer("
        #         "launcher_id text PRIMARY KEY,"
        #         " p2_singleton_puzzle_hash text,"
        #         " delay_time bigint,"
        #         " delay_puzzle_hash text,"
        #         " authentication_public_key text,"
        #         " singleton_tip blob,"
        #         " singleton_tip_state blob,"
        #         " points bigint,"
        #         " difficulty bigint,"
        #         " payout_instructions text,"
        #         " is_pool_member tinyint)"
        #     )
        # )
        #
        # await self.connection.execute(
        #     "CREATE TABLE IF NOT EXISTS partial(launcher_id text, timestamp bigint, difficulty bigint)"
        # )
        #
        # await self.connection.execute("CREATE INDEX IF NOT EXISTS scan_ph on farmer(p2_singleton_puzzle_hash)")
        # await self.connection.execute("CREATE INDEX IF NOT EXISTS timestamp_index on partial(timestamp)")
        # await self.connection.execute("CREATE INDEX IF NOT EXISTS launcher_id_index on partial(launcher_id)")
        #
        # await self.connection.commit()
        self.connection = pymysql.connect(host='192.168.135.153', port=3306, user='chia_pool',
                                          password='P3apPwiW4NAdTjPs',
                                          db='chia_pool')
        # 获得游标
        self.cursor = self.connection.cursor()

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
        self.cursor.execute(
            f"REPLACE INTO farmer VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
        self.connection.commit()

    async def get_farmer_record(self, launcher_id: bytes32) -> Optional[FarmerRecord]:
        # TODO(pool): use cache
        self.cursor.execute(
            f"SELECT * from farmer where launcher_id=%s", launcher_id
        )
        row = self.cursor.fetchone()
        if row is None:
            return None
        return self._row_to_farmer_record(row)

    async def update_difficulty(self, launcher_id: bytes32, difficulty: uint64):
        self.cursor.execute(
            f"UPDATE farmer SET difficulty=%s WHERE launcher_id=%s", launcher_id, difficulty
        )
        self.connection.commit()

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
        self.cursor.execute(
            f"UPDATE farmer SET singleton_tip=%s, singleton_tip_state=%s, is_pool_member=%s WHERE launcher_id=%s",
            entry,
        )
        self.connection.commit()

    async def get_pay_to_singleton_phs(self) -> Set[bytes32]:
        # self.connection()
        self.cursor.execute("SELECT p2_singleton_puzzle_hash from farmer")
        rows = self.cursor.fetchall()

        all_phs: Set[bytes32] = set()
        for row in rows:
            all_phs.add(bytes32(bytes.fromhex(row[0])))
        return all_phs

    async def get_farmer_records_for_p2_singleton_phs(self, puzzle_hashes: Set[bytes32]) -> List[FarmerRecord]:
        if len(puzzle_hashes) == 0:
            return []
        puzzle_hashes_db = tuple([ph.hex() for ph in list(puzzle_hashes)])
        self.cursor.execute(
            f'SELECT * from farmer WHERE p2_singleton_puzzle_hash in ({"?," * (len(puzzle_hashes_db) - 1)}?) ',
            puzzle_hashes_db,
        )
        rows = await self.cursor.fetchall()
        return [self._row_to_farmer_record(row) for row in rows]

    async def get_farmer_points_and_payout_instructions(self) -> List[Tuple[uint64, bytes]]:
        self.cursor.execute(f"SELECT points, payout_instructions from farmer")
        rows = await self.cursor.fetchall()
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
        self.cursor.execute(f"UPDATE farmer set points=0")
        self.connection.commit()

    async def add_partial(self, launcher_id: bytes32, timestamp: uint64, difficulty: uint64):
        self.cursor.execute(
            "INSERT into partial VALUES(?, ?, ?)",
            (launcher_id.hex(), timestamp, difficulty),
        )
        self.cursor.close()
        self.cursor.execute(f"SELECT points from farmer where launcher_id=?", (launcher_id.hex(),))
        row = self.cursor.fetchone()
        points = row[0]
        self.cursor.close()
        self.cursor.execute(
            f"UPDATE farmer set points=? where launcher_id=?", (points + difficulty, launcher_id.hex())
        )
        self.cursor.close()
        self.connection.commit()

    async def get_recent_partials(self, launcher_id: bytes32, count: int) -> List[Tuple[uint64, uint64]]:
        self.cursor.execute(
            "SELECT timestamp, difficulty from partial WHERE launcher_id=? ORDER BY timestamp DESC LIMIT ?",
            (launcher_id.hex(), count),
        )
        rows = await self.cursor.fetchall()
        ret: List[Tuple[uint64, uint64]] = [(uint64(timestamp), uint64(difficulty)) for timestamp, difficulty in rows]
        return ret