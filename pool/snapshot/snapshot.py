import asyncio
import logging
import traceback
from typing import Dict, Optional, Set, List, Tuple, Callable

from pool.store.abstract import AbstractPoolStore


class Snapshot:
    def __init__(self, pool_config: Dict, store: Optional[AbstractPoolStore] = None):
        self.log = logging
        # If you want to log to a file: use filename='example.log', encoding='utf-8'
        self.log.basicConfig(level=logging.INFO)

        self.store = store
        # Interval for taking snapshot of farmer's points
        self.snapshot_interval = pool_config["snapshot_interval"]

        self.create_snapshot_loop_task: Optional[asyncio.Task] = None

    async def start(self):
        await self.store.connect()

        self.create_snapshot_loop_task = asyncio.create_task(self.create_snapshot_loop())

    async def stop(self):
        if self.create_snapshot_loop_task is not None:
            self.create_snapshot_loop_task.cancel()

    async def create_snapshot_loop(self):
        """
        Calculates the points of each farmer, and splits the total funds received into coins for each farmer.
        Saves the transactions that we should make, to `amount_to_distribute`.
        """
        while True:
            try:
                self.log.info(f"Creating snapshot of the farmers")
                # keep a snapshot of the points collected by the farmer
                await self.store.snapshot_farmer_points()
                self.log.info(f"Snapshot Completed")
                await asyncio.sleep(self.snapshot_interval)

            except asyncio.CancelledError:
                self.log.info("Cancelled create_snapshot_loop, closing")
                return
            except Exception as e:
                error_stack = traceback.format_exc()
                self.log.error(f"Unexpected error in create_snapshot_loop: {e} {error_stack}")
                await asyncio.sleep(self.snapshot_interval)
