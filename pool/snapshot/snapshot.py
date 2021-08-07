import asyncio
import logging
import pathlib
import traceback
from math import floor
from typing import Dict, Optional, Set, List, Tuple, Callable

import os
import yaml
import time

from chia.consensus.constants import ConsensusConstants
from chia.util.ints import uint8, uint16, uint32, uint64
from chia.util.default_root import DEFAULT_ROOT_PATH
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.chia_logging import initialize_logging

from pool.store.abstract import AbstractPoolStore
from pool.store.sqlite_store import SqlitePoolStore


class Snapshot:
    def __init__(self, config: Dict, constants: ConsensusConstants, pool_store: Optional[AbstractPoolStore] = None):
        self.log = logging
        # If you want to log to a file: use filename='example.log', encoding='utf-8'
        self.log.basicConfig(level=logging.INFO)

        # We load our configurations from here
        with open(os.getcwd() + "/config.yaml") as f:
            pool_config: Dict = yaml.safe_load(f)

        pool_config["logging"]["log_filename"] = pool_config["logging"].get("snapshot_log_filename", "snapshot.log")
        initialize_logging("pool", pool_config["logging"], pathlib.Path(pool_config["logging"]["log_path"]))

        self.config = config
        self.constants = constants

        self.store: AbstractPoolStore = pool_store or SqlitePoolStore()

        # Interval for taking snapshot of farmer's points
        self.snapshot_interval = pool_config["snapshot_interval"]

        self.create_payment_loop_task: Optional[asyncio.Task] = None

    async def start(self):
        await self.store.connect()

        self.create_snapshot_loop_task = asyncio.create_task(self.create_snapshot_loop())

    async def stop(self):
        if self.create_snapstho_loop_task is not None:
            self.create_snapshot_loop_task.cancel()

        await self.store.connection.close()

    async def create_snapshot_loop(self):
        """
        Calculates the points of each farmer, and splits the total funds received into coins for each farmer.
        Saves the transactions that we should make, to `amount_to_distribute`.
        """
        while True:
            try:
                self.log.info(f"Create snapshot of the farmers")
                # keep a snapshot of the points collected by the farmer
                await self.store.snapshot_farmer_points()
                await asyncio.sleep(self.snapshot_interval)

            except asyncio.CancelledError:
                self.log.info("Cancelled create_snapshot_loop, closing")
                return
            except Exception as e:
                error_stack = traceback.format_exc()
                self.log.error(f"Unexpected error in create_snapshot_loop: {e} {error_stack}")
                await asyncio.sleep(self.snapshot_interval)
