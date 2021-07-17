import asyncio
import logging
from typing import Dict, Optional

from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.consensus.constants import ConsensusConstants
from chia.util.default_root import DEFAULT_ROOT_PATH
from chia.util.config import load_config

from .reward_collector import RewardCollector
from pool.store.abstract import AbstractPoolStore
from pool.store.pg_store import PGStore


class RewardCollectorServer:
    def __init__(self, config: Dict, constants: ConsensusConstants, pool_store: Optional[AbstractPoolStore] = None):

        self.log = logging.getLogger(__name__)
        self.reward_collector = RewardCollector(config, constants, pool_store)

    async def start(self):
        await self.reward_collector.start()

    async def stop(self):
        await self.reward_collector.stop()


server: Optional[RewardCollectorServer] = None


async def start_reward_collector_server(pool_store: Optional[AbstractPoolStore] = None):
    global server
    config = load_config(DEFAULT_ROOT_PATH, "config.yaml")
    overrides = config["network_overrides"]["constants"][config["selected_network"]]
    constants: ConsensusConstants = DEFAULT_CONSTANTS.replace_str_to_bytes(**overrides)
    server = RewardCollectorServer(config, constants, pool_store)
    await server.start()

    await asyncio.sleep(10000000)


async def stop():
    await server.stop()


def main():
    try:
        asyncio.run(start_reward_collector_server(pool_store=PGStore()))
    except KeyboardInterrupt:
        asyncio.run(stop())


if __name__ == "__main__":
    main()
