from abc import ABC, abstractmethod
import asyncio
from typing import Optional, Set, List, Tuple

from chia.pools.pool_wallet_info import PoolState
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_spend import CoinSpend
from chia.util.ints import uint64

from ..record import FarmerRecord
from ..util import RequestMetadata
from ..pay_record import PaymentRecord
from ..reward_record import RewardRecord

class AbstractPoolStore(ABC):
    """
    Base class for asyncio-related pool stores.
    """

    def __init__(self):
        self.lock = asyncio.Lock()
        self.connection = None

    @abstractmethod
    async def connect(self):
        """Perform IO-related initialization"""

    @abstractmethod
    async def add_farmer_record(self, farmer_record: FarmerRecord, metadata: RequestMetadata):
        """Persist a new Farmer in the store"""

    @abstractmethod
    async def get_farmer_record(self, launcher_id: bytes32) -> Optional[FarmerRecord]:
        """Fetch a farmer record for given ``launcher_id``. Returns ``None`` if no record found"""

    @abstractmethod
    async def update_difficulty(self, launcher_id: bytes32, difficulty: uint64):
        """Update difficulty for Farmer identified by ``launcher_id``"""

    @abstractmethod
    async def update_singleton(
        self,
        launcher_id: bytes32,
        singleton_tip: CoinSpend,
        singleton_tip_state: PoolState,
        is_pool_member: bool,
    ):
        """Update Farmer's singleton-related data"""

    @abstractmethod
    async def get_pay_to_singleton_phs(self) -> Set[bytes32]:
        """Fetch all puzzle hashes of Farmers in this pool, to scan the blockchain in search of them"""

    @abstractmethod
    async def get_farmer_records_for_p2_singleton_phs(self, puzzle_hashes: Set[bytes32]) -> List[FarmerRecord]:
        """Fetch Farmers matching given puzzle hashes"""

    @abstractmethod
    async def get_farmer_points_and_payout_instructions(self) -> List[Tuple[uint64, bytes]]:
        """Fetch all farmers and their respective payout instructions"""

    @abstractmethod
    async def snapshot_farmer_points(self) -> None:
        """Snapshot all Farmers' non-zero points"""

    @abstractmethod
    async def clear_farmer_points(self) -> None:
        """Rest all Farmers' points to 0"""

    @abstractmethod
    async def add_partial(self, launcher_id: bytes32, harvester_id: bytes32, timestamp: uint64, difficulty: uint64):
        """Register new partial and update corresponding Farmer's points"""

    @abstractmethod
    async def get_recent_partials(self, launcher_id: bytes32, count: int) -> List[Tuple[uint64, uint64]]:
        """Fetch last ``count`` partials for Farmer identified by ``launcher_id``"""

    @abstractmethod
    async def add_payment(self, payment_record: PaymentRecord):
        """Add new payment record for farmer"""

    @abstractmethod
    async def add_reward_record(self, reward: RewardRecord):
        """Add reward claim record"""
