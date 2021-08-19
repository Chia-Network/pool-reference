import asyncio
import logging
from typing import Dict, Optional

from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.consensus.constants import ConsensusConstants
from chia.util.default_root import DEFAULT_ROOT_PATH
from chia.util.config import load_config

from .payment import Payment
from pool.store.abstract import AbstractPoolStore


class PaymentServer:
    def __init__(self, config: Dict, constants: ConsensusConstants, pool_store: Optional[AbstractPoolStore] = None):

        self.log = logging.getLogger(__name__)
        self.payment = Payment(config, constants, pool_store)

    async def start(self):
        await self.payment.start()

    async def stop(self):
        await self.payment.stop()


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
