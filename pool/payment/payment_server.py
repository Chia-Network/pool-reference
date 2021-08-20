import asyncio
import logging
import os
import pathlib

import yaml
from typing import Dict, Optional

from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.consensus.constants import ConsensusConstants
from chia.util.chia_logging import initialize_logging
from chia.util.default_root import DEFAULT_ROOT_PATH
from chia.util.config import load_config

from .payment import Payment
from pool.store.abstract import AbstractPoolStore
from pool.store.sqlite_store import SqlitePoolStore


class PaymentServer:
    def __init__(self, config: Dict, constants: ConsensusConstants, pool_store: Optional[AbstractPoolStore] = None):
        # We load our configurations from here
        with open(os.getcwd() + "/config.yaml") as f:
            pool_config: Dict = yaml.safe_load(f)

        store: AbstractPoolStore = pool_store or SqlitePoolStore()

        self.store = store
        self.log = logging.getLogger(__name__)

        pool_config["logging"]["log_filename"] = pool_config["logging"].get("snapshot_log_filename", "snapshot.log")

        initialize_logging("pool", pool_config["logging"], pathlib.Path(pool_config["logging"]["log_path"]))

        self.payments = Payment(config, constants, pool_config, store,)

    async def start(self):
        await self.store.connect()
        await self.payments.start()

    async def stop(self):
        await self.payments.stop()
        await self.store.connection.close()


server: Optional[PaymentServer] = None


async def start_payment_server(pool_store: Optional[AbstractPoolStore] = None):
    global server
    config = load_config(DEFAULT_ROOT_PATH, "config.yaml")
    overrides = config["network_overrides"]["constants"][config["selected_network"]]
    constants: ConsensusConstants = DEFAULT_CONSTANTS.replace_str_to_bytes(**overrides)
    server = PaymentServer(config, constants, pool_store)
    await server.start()

    await asyncio.sleep(10000000)


async def stop():
    await server.stop()


def main():
    try:
        asyncio.run(start_payment_server())
    except KeyboardInterrupt:
        asyncio.run(stop())


if __name__ == "__main__":
    main()
