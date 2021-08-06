from pathlib import Path
from typing import Optional, Set, List, Tuple, Dict

from blspy import G1Element
from chia.pools.pool_wallet_info import PoolState
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_spend import CoinSpend
from chia.util.ints import uint64

from .abstract import AbstractPoolStore
from ..record import FarmerRecord
from ..util import RequestMetadata
from ..pay_record import PaymentRecord
from ..reward_record import RewardRecord

import asyncpg


class PGStore(AbstractPoolStore):
    """
    Pool store based on SQLite.
    """
    def __init__(self):
        super().__init__()
        self.connection: Optional[asyncpg.Connection] = None

    async def connect(self):
        print('Connecting to the postgreSQL database...')
        self.connection = await asyncpg.connect('postgresql://postgres@localhost/maxipool')
        await self.connection.execute(
            (
                "CREATE TABLE IF NOT EXISTS farmer("
                "launcher_id text PRIMARY KEY,"
                " p2_singleton_puzzle_hash text,"
                " delay_time bigint,"
                " delay_puzzle_hash text,"
                " authentication_public_key text,"
                " singleton_tip bytea,"
                " singleton_tip_state bytea,"
                " points bigint,"
                " difficulty bigint,"
                " payout_instructions text,"
                " is_pool_member smallint)"
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
                "launcher_id text,  /* farmer */"
                "amount bigint,     /* amount paid */"
                "payment_type text, /* payment type: xch, wxch, maxi */"
                "timestamp bigint,  /* payment timestamp */"
                "points bigint,     /* points of farmer */"
                "txid text,         /* payment tx id*/"
                "note text          /* additional note */)"
            )
        )

        await self.connection.execute("CREATE INDEX IF NOT EXISTS payment_launcher_index on payment(launcher_id)")

        # snapshot table to keep track of farmer's points
        await self.connection.execute(
            (
                "CREATE TABLE IF NOT EXISTS points_ss("
                "launcher_id text, /* farmer */"
                "points bigint,    /* farmer's points */"
                "timestamp bigint  /* snapshot timestamp */)"
            )
        )
        await self.connection.execute("CREATE INDEX IF NOT EXISTS ss_launcher_id_index on points_ss(launcher_id)")

        # create rewards tx table
        await self.connection.execute(
            (
                "CREATE TABLE IF NOT EXISTS rewards_tx("
                "launcher_id text,  /* farmer*/"
                "claimable bigint,  /* block reward*/"
                "height bigint,     /* block height of the reward*/"
                "coins text,        /* coin hash*/"
                "timestamp bigint   /* timestamp of the record */)"
            )
        )
        await self.connection.execute("CREATE INDEX IF NOT EXISTS re_launcher_id_index on rewards_tx(launcher_id)")

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
        await self.connection.execute(f"DELETE FROM farmer WHERE launcher_id=$1", farmer_record.launcher_id.hex())
        await self.connection.execute(
            f"INSERT INTO farmer VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
            *(
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

    async def get_farmer_record(self, launcher_id: bytes32) -> Optional[FarmerRecord]:
        row = await self.connection.fetchrow(
            "SELECT * from farmer where launcher_id=$1",
            launcher_id.hex(),
        )
        if row is None:
            return None
        return self._row_to_farmer_record(row)

    async def update_difficulty(self, launcher_id: bytes32, difficulty: uint64):
        await self.connection.execute(
            f"UPDATE farmer SET difficulty=$1 WHERE launcher_id=$2", difficulty, launcher_id.hex()
        )

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
        await self.connection.execute(
            f"UPDATE farmer SET singleton_tip=$1, singleton_tip_state=$2, is_pool_member=$3 WHERE launcher_id=$4",
            *entry,
        )

    async def get_pay_to_singleton_phs(self) -> Set[bytes32]:
        all_phs: Set[bytes32] = set()
        for row in await self.connection.fetch("SELECT p2_singleton_puzzle_hash from farmer"):
            all_phs.add(bytes32(bytes.fromhex(row[0])))
        return all_phs

    async def get_farmer_records_for_p2_singleton_phs(self, puzzle_hashes: Set[bytes32]) -> List[FarmerRecord]:
        if len(puzzle_hashes) == 0:
            return []
        puzzle_hashes_db = tuple([ph.hex() for ph in list(puzzle_hashes)])
        return [self._row_to_farmer_record(row) for row in await self.connection.fetch(
            f'SELECT * from farmer WHERE p2_singleton_puzzle_hash in (${",$".join(map(str, range(1, len(puzzle_hashes_db)+1)))}) ',
            *puzzle_hashes_db,
        )]

    async def get_farmer_points_and_payout_instructions(self) -> List[Tuple[uint64, bytes]]:
        accumulated: Dict[bytes32, uint64] = {}
        for row in await self.connection.fetch(f"SELECT points, payout_instructions from farmer"):
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
                "INSERT into points_ss (launcher_id, points, timestamp)"
                "SELECT launcher_id, points, extract(epoch from now()) * 1000 from farmer WHERE points != 0"
            )
        )

    async def clear_farmer_points(self) -> None:
        await self.connection.execute(f"UPDATE farmer set points=0")

    async def add_partial(self, launcher_id: bytes32, harvester_id: bytes32, timestamp: uint64, difficulty: uint64):
        await self.connection.execute(
            "INSERT into partial (launcher_id, harvester_id, timestamp, difficulty) VALUES($1, $2, $3, $4)",
            launcher_id.hex(), harvester_id.hex(), timestamp, difficulty,
        )
        row = await self.connection.fetchrow(f"SELECT points from farmer where launcher_id=$1", launcher_id.hex())
        points = row[0]
        await self.connection.execute(
            f"UPDATE farmer set points=$1 where launcher_id=$2", points + difficulty, launcher_id.hex()
        )

    async def get_recent_partials(self, launcher_id: bytes32, count: int) -> List[Tuple[uint64, uint64]]:
        ret: List[Tuple[uint64, uint64]] = [(uint64(timestamp), uint64(difficulty))
                                            for timestamp, difficulty in await self.connection.fetch(
                "SELECT timestamp, difficulty from partial WHERE launcher_id=$1 ORDER BY timestamp DESC LIMIT $2",
                launcher_id.hex(), count,
            )]
        return ret

    async def add_payment(self, payment: PaymentRecord):
        await self.connection.execute(
            f"INSERT into payment (launcher_id, amount, payment_type, timestamp, points, txid, note) VALUES($1, $2, $3, $4, $5, $6, $7)",
            *(
                payment.launcher_id.hex(),
                payment.payment_amount,
                payment.payment_type,
                payment.timestamp,
                payment.points,
                payment.txid,
                payment.note
            ),
        )

    async def add_reward_record(self, reward: RewardRecord):
        await self.connection.execute(
            f"INSERT INTO rewards_tx(launcher_id, claimable, height, coins, timestamp) VALUES($1, $2, $3, $4, $5)",
            *(
                reward.launcher_id.hex(),
                reward.claimable,
                reward.height,
                reward.coins.hex(),
                reward.timestamp
            ),
        )
