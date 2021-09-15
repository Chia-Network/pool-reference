import os
import yaml
import logging
from pathlib import Path
from typing import Optional, Set, List, Tuple, Dict

import asyncio
import aiomysql
import pymysql
from blspy import G1Element
from chia.pools.pool_wallet_info import PoolState
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_solution import CoinSolution
from chia.util.ints import uint64

from .abstract import AbstractPoolStore
from ..record import FarmerRecord
from ..util import RequestMetadata

pymysql.converters.encoders[uint64] = pymysql.converters.escape_int
pymysql.converters.conversions = pymysql.converters.encoders.copy()
pymysql.converters.conversions.update(pymysql.converters.decoders)

class MariadbPoolStore(AbstractPoolStore):
    """
    Pool store based on MariaDB.
    """
    async def connect(self):
        try:
            #initialize logging 
            self.log = logging
            #load config 
            with open(os.getcwd() + "/config.yaml") as f:
                config: Dict = yaml.safe_load(f)
            self.pool = await aiomysql.create_pool(
            minsize=1, 
            maxsize=12,
            host=config["db_host"],
            port=config["db_port"],
            user=config["db_user"],
            password=config["db_password"],
            db=config["db_name"],
            )
        except pymysql.err.OperationalError as e:
                self.log.error("Error In Database Config. Check your config file! %s", e)
                raise ConnectionError('Unable to Connect to SQL Database.')
        self.connection = await self.pool.acquire()
        self.cursor = await self.connection.cursor()
        await self.cursor.execute(
            (
                "CREATE TABLE IF NOT EXISTS farmer("
                "launcher_id VARCHAR(200) PRIMARY KEY,"
                " p2_singleton_puzzle_hash text,"
                " delay_time bigint,"
                " delay_puzzle_hash text,"
                " authentication_public_key text,"
                " singleton_tip blob,"
                " singleton_tip_state blob,"
                " points bigint,"
                " difficulty bigint,"
                " payout_instructions text,"
                " is_pool_member tinyint,"
                " blocks int,"
                " xch_paid float)"
            )
        )

        await self.cursor.execute(
            "CREATE TABLE IF NOT EXISTS partial(launcher_id text, timestamp bigint, difficulty bigint)"
        )

        await self.cursor.execute("CREATE INDEX IF NOT EXISTS scan_ph on farmer(p2_singleton_puzzle_hash)")
        await self.cursor.execute("CREATE INDEX IF NOT EXISTS timestamp_index on partial(timestamp)")
        await self.cursor.execute("CREATE INDEX IF NOT EXISTS launcher_id_index on partial(launcher_id)")
        await self.connection.commit()
        self.pool.release(self.connection)
        

    @staticmethod
    def _row_to_farmer_record(row) -> FarmerRecord:
        return FarmerRecord(
            bytes.fromhex(row[0]),
            bytes.fromhex(row[1]),
            row[2],
            bytes.fromhex(row[3]),
            G1Element.from_bytes(bytes.fromhex(row[4])),
            CoinSolution.from_bytes(row[5]),
            PoolState.from_bytes(row[6]),
            row[7],
            row[8],
            row[9],
            True if row[10] == 1 else False,
        )

    async def add_farmer_record(self, farmer_record: FarmerRecord, metadata: RequestMetadata):
        with (await self.pool) as connection:
            cursor = await connection.cursor()
            await cursor.execute(
                f"INSERT INTO farmer VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE launcher_id=%s, p2_singleton_puzzle_hash=%s, delay_time=%s, delay_puzzle_hash=%s, authentication_public_key=%s, singleton_tip=%s, singleton_tip_state=%s, points=%s, difficulty=%s, payout_instructions=%s,is_pool_member=%s",
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
            await connection.commit()

    async def get_farmer_record(self, launcher_id: bytes32) -> Optional[FarmerRecord]:
        # TODO(pool): use cache
        with (await self.pool) as connection:
            cursor = await connection.cursor()
            await cursor.execute(
                f"SELECT * FROM farmer WHERE launcher_id=%s",(launcher_id.hex(),),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return self._row_to_farmer_record(row)

    async def update_difficulty(self, launcher_id: bytes32, difficulty: uint64):
        with (await self.pool) as connection:
            connection = await self.pool.acquire()
            cursor = await connection.cursor()
            await cursor.execute(
                f"UPDATE farmer SET difficulty=%s WHERE launcher_id=%s", (difficulty, launcher_id.hex())
            )
            await connection.commit()
        

    async def update_singleton(
        self,
        launcher_id: bytes32,
        singleton_tip: CoinSolution,
        singleton_tip_state: PoolState,
        is_pool_member: bool,
    ):
        entry = (bytes(singleton_tip), bytes(singleton_tip_state), int(is_pool_member), launcher_id.hex())
        with (await self.pool) as connection:
            cursor = await connection.cursor()
            await cursor.execute(
                f"UPDATE farmer SET singleton_tip=%s, singleton_tip_state=%s, is_pool_member=%s WHERE launcher_id=%s",
                entry,
            )
            await connection.commit()

    async def get_pay_to_singleton_phs(self) -> Set[bytes32]:
        with (await self.pool) as connection:
            cursor = await connection.cursor()
            await cursor.execute("SELECT p2_singleton_puzzle_hash from farmer")
            rows = await cursor.fetchall()
            await cursor.close()

            all_phs: Set[bytes32] = set()
            for row in rows:
              all_phs.add(bytes32(bytes.fromhex(row[0])))
            return all_phs

    async def get_farmer_records_for_p2_singleton_phs(self, puzzle_hashes: Set[bytes32]) -> List[FarmerRecord]:
        if len(puzzle_hashes) == 0:
            return []
        puzzle_hashes_db = tuple([ph.hex() for ph in list(puzzle_hashes)])
        with (await self.pool) as connection:
            cursor = await connection.cursor()
            await cursor.execute(
                f'SELECT * from farmer WHERE p2_singleton_puzzle_hash in ({"%s," * (len(puzzle_hashes_db) - 1)}%s) ',
                puzzle_hashes_db,
            )
            rows = await cursor.fetchall()
            await cursor.close()
            return [self._row_to_farmer_record(row) for row in rows]

    async def get_farmer_points_and_payout_instructions(self) -> List[Tuple[uint64, bytes]]:
        with (await self.pool) as connection:
            cursor = await connection.cursor()
            await cursor.execute(f"SELECT points, payout_instructions FROM farmer")
            rows = await cursor.fetchall()
            await cursor.close()
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
        with (await self.pool) as connection:
            cursor = await connection.cursor()
            await cursor.execute(f"UPDATE farmer SET points=0")
            await cursor.close()
            await connection.commit()


    async def add_partial(self, launcher_id: bytes32, timestamp: uint64, difficulty: uint64):
        with (await self.pool) as connection:
            cursor = await connection.cursor()
            await cursor.execute("INSERT INTO partial VALUES(%s, %s, %s)",(launcher_id.hex(), timestamp, difficulty),
            )
            await connection.commit()
        with (await self.pool) as connection:
            cursor = await connection.cursor()
            await cursor.execute(
                f"UPDATE farmer SET points=points+%s WHERE launcher_id=%s", (difficulty, launcher_id.hex())
            )
            await connection.commit()
            await cursor.close()

    async def get_recent_partials(self, launcher_id: bytes32, count: int) -> List[Tuple[uint64, uint64]]:
        with (await self.pool) as connection:
            cursor = await connection.cursor()
            await cursor.execute(
                "SELECT timestamp, difficulty from partial WHERE launcher_id=%s ORDER BY timestamp DESC LIMIT %s",
                (launcher_id.hex(), count),
            )
            rows = await cursor.fetchall()
            ret: List[Tuple[uint64, uint64]] = [(uint64(timestamp), uint64(difficulty)) for timestamp, difficulty in rows]
            return ret
        
