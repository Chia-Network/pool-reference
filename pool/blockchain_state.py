import asyncio

from typing import Optional
from chia.rpc.wallet_rpc_client import WalletRpcClient
from chia.rpc.full_node_rpc_client import FullNodeRpcClient


class StateKeeper:
    """
    Manager for a task periodically syncing the state of the blockchain and the wallet
    """
    def __init__(self, logger, wallet_fingerprint):
        self._logger = logger
        self._node_rpc_client: Optional[FullNodeRpcClient] = None
        self._wallet_rpc_client: Optional[WalletRpcClient] = None
        self._get_peak_loop_task: Optional[asyncio.Task] = None

        # Keeps track of the latest state of our node
        self.blockchain_state = {"peak": None}

        # Whether or not the wallet is synced (required to make payments)
        self.wallet_synced = False

        # configuration
        self.wallet_fingerprint = wallet_fingerprint

    async def start(self, node_rpc_client, wallet_rpc_client):
        """
        Start the state keeping loop
        """
        self._node_rpc_client = node_rpc_client
        self._wallet_rpc_client = wallet_rpc_client
        self.blockchain_state = await self._node_rpc_client.get_blockchain_state()
        self.wallet_synced = await self._wallet_rpc_client.get_synced()

        self._get_peak_loop_task = asyncio.create_task(self.get_peak_loop())

    async def stop(self):
        """
        Terminate the state keeping loop
        """
        if self._get_peak_loop_task is not None:
            self._get_peak_loop_task.cancel()

    async def get_peak_loop(self):
        """
        Periodically contacts the full node to get the latest state of the blockchain
        """
        while True:
            try:
                self.blockchain_state = await self._node_rpc_client.get_blockchain_state()
                self.wallet_synced = await self._wallet_rpc_client.get_synced()
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self._logger.info("Cancelled get_peak_loop, closing")
                return
            except Exception as e:
                self._logger.error(f"Unexpected error in get_peak_loop: {e}")
                await asyncio.sleep(30)
