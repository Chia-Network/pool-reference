from abc import ABC, abstractmethod
from typing import Optional

from chia.rpc.wallet_rpc_client import WalletRpcClient
from chia.rpc.full_node_rpc_client import FullNodeRpcClient

from ..record import FarmerRecord
from ..store.abstract import AbstractPoolStore
from ..blockchain_state import StateKeeper


class AbstractPaymentManager(ABC):
    """
    Manager for tasks:

    * scanning the blockchain for rewards to claim for the pool
    * scanning the blockchain for payment events
    * paying pool members
    """

    def __init__(self, logger, pool_config: dict, constants):
        self._logger = logger
        self._constants = constants
        self._pool_config = pool_config

        # The objects below are passed in by Pool initializing this class when `AbstractPaymentManager.start` is
        # called. The reasons for this are: Pool instantiates them in it's `start` and we want to avoid circular
        # dependencies (like passing a pool object to this class)
        self._node_rpc_client: Optional[FullNodeRpcClient] = None
        self._wallet_rpc_client: Optional[WalletRpcClient] = None
        self._store: Optional[AbstractPoolStore] = None
        self._state_keeper: Optional[StateKeeper] = None

    @abstractmethod
    async def start(self, node_rpc_client: FullNodeRpcClient, wallet_rpc_client: WalletRpcClient,
                    store: AbstractPoolStore, state_keeper: StateKeeper):
        """
        Save connection objects and start the periodic tasks
        """
        self._node_rpc_client = node_rpc_client
        self._wallet_rpc_client = wallet_rpc_client
        self._store = store
        self._state_keeper = state_keeper

    @abstractmethod
    async def stop(self):
        """
        Terminate the periodic tasks
        """

    @abstractmethod
    def register_new_farmer(self, farmer_record: FarmerRecord):
        """
        Hook method to be called when a new farmer is added to the pool
        """
