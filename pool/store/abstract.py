from abc import ABC, abstractmethod
import asyncio
from typing import Optional, Set, List, Tuple

from chia.pools.pool_wallet_info import PoolState
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_spend import CoinSpend
from chia.util.ints import uint64

from ..record import FarmerRecord
from ..util import RequestMetadata


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
    async def tx(self):
        """Performing Transactions for async with statement"""

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
    async def get_farmer_launcher_id_and_points_and_payout_instructions(self) -> List[Tuple[bytes32, uint64, bytes32]]:
        """Fetch all farmers and their respective payout instructions"""

    @abstractmethod
    async def clear_farmer_points(self, auto_commit: bool = True) -> None:
        """
        Rest all Farmers' points to 0
        auto_commit decides whether to commit the transaction.
        """

    @abstractmethod
    async def add_partial(self, launcher_id: bytes32, timestamp: uint64, difficulty: uint64):
        """Register new partial and update corresponding Farmer's points"""

    @abstractmethod
    async def get_recent_partials(self, launcher_id: bytes32, count: int) -> List[Tuple[uint64, uint64]]:
        """Fetch last ``count`` partials for Farmer identified by ``launcher_id``"""

    @abstractmethod
    async def add_payment(
        self,
        launcher_id: bytes32,
        puzzle_hash: bytes32,
        amount: uint64,
        points: int,
        timestamp: uint64,
        is_payment: bool,
        auto_commit: bool = True,
    ):
        """
        Persist a new payment record in the store
        auto_commit decides whether to commit the transaction.
        """

    @abstractmethod
    async def update_is_payment(
        self,
        launcher_id_and_timestamp: List[Tuple[bytes32, uint64]],
        auto_commit: bool = True,
    ):
        """
        Update is_payment is True for payment records identified by ``launcher_id`` and ``timestamp``.
        auto_commit decides whether to commit the transaction.
        """

    @abstractmethod
    async def get_pending_payment_records(self, count: int) -> List[
        Tuple[bytes32, bytes32, uint64, uint64, uint64, bool, bool]
    ]:
        """Fetch ``count`` pending payment records"""

    @abstractmethod
    async def get_pending_payment_count(self) -> int:
        """Fetch pending payment records count"""

    @abstractmethod
    async def get_confirming_payment_records(self) -> List[bytes32]:
        """Fetch confirming payment records"""

    @abstractmethod
    async def update_transaction_id(
        self,
        launcher_id_and_timestamp: List[Tuple[bytes32, uint64]],
        transaction_id: bytes32,
    ):
        """Update transaction_id for payment records identified by ``launcher_id`` and ``timestamp``."""

    @abstractmethod
    async def update_is_confirmed(self, transaction_id: bytes32):
        """Update is_confirmed is True for payment records identified by ``transaction_id``"""
