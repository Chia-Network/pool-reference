import asyncio
import logging
import time
import traceback
from typing import Dict, Callable, Optional

import aiohttp
from blspy import AugSchemeMPL, PrivateKey
from aiohttp import web
from chia.protocols.pool_protocol import SubmitPartial, PoolInfo
from chia.util.hash import std_hash
from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.consensus.constants import ConsensusConstants
from chia.util.json_util import obj_to_response
from chia.util.ints import uint64
from chia.util.default_root import DEFAULT_ROOT_PATH
from chia.util.config import load_config

from error_codes import PoolErr
from store import FarmerRecord
from pool import Pool


class PoolServer:
    def __init__(self, private_key: PrivateKey, config: Dict, constants: ConsensusConstants):

        self.log = logging.getLogger(__name__)
        self.pool = Pool(private_key, config, constants)

    async def start(self):
        await self.pool.start()

    async def stop(self):
        await self.pool.stop()

    def wrap_http_handler(self, f) -> Callable:
        async def inner(request) -> aiohttp.web.Response:
            request_data = await request.json()
            try:
                res_object = await f(request_data)
                if res_object is None:
                    res_object = {}
            except Exception as e:
                tb = traceback.format_exc()
                self.log.warning(f"Error while handling message: {tb}")
                if len(e.args) > 0:
                    res_object = {"error_code": PoolErr.SERVER_EXCEPTION, "error_message": f"{e.args[0]}"}
                else:
                    res_object = {"error_code": PoolErr.SERVER_EXCEPTION, "error_message": f"{e}"}

            return obj_to_response(res_object)

        return inner

    async def index(_):
        return web.Response(text="Chia reference pool")

    async def get_pool_info(self, _):
        res: PoolInfo = PoolInfo(
            "The Reference Pool",
            "https://www.chia.net/img/chia_logo.svg",
            uint64(self.pool.min_difficulty),
            uint64(self.pool.max_difficulty),
            uint64(self.pool.relative_lock_height),
            "1.0.0",
            str(self.pool.pool_fee),
            "(example) The Reference Pool allows you to pool with low fees, paying out daily using Chia.",
        )
        return res

    async def submit_partial(self, request) -> Dict:
        # TODO: add rate limiting
        partial: SubmitPartial = SubmitPartial.from_json_dict(request.json())
        time_received_partial = uint64(int(time.time()))

        # It's important that on the first request from this farmer, the default difficulty is used. Changing the
        # difficulty requires a few minutes, otherwise farmers can abuse by setting the difficulty right under the
        # proof that they found.
        farmer_record: Optional[FarmerRecord] = await self.store.get_farmer_record(partial.payload.singleton_genesis)
        if farmer_record is not None:
            curr_difficulty: uint64 = farmer_record.difficulty
            balance = farmer_record.points
        else:
            curr_difficulty = self.pool.default_difficulty
            balance = uint64(0)

        async def await_and_call(cor, *args):
            # 10 seconds gives our node some time to get the signage point, in case we are slightly slowed down
            await asyncio.sleep(10)
            await cor(args)

        res_dict = await self.pool.process_partial(
            partial,
            time_received_partial,
            balance,
            curr_difficulty,
        )

        if "error_code" in res_dict and "error_code" == PoolErr.NOT_FOUND:
            asyncio.create_task(
                await_and_call(self.pool.process_partial, partial, time_received_partial, balance, curr_difficulty)
            )
        return res_dict


async def start_pool_server():
    private_key: PrivateKey = AugSchemeMPL.key_gen(std_hash(b"123"))
    config = load_config(DEFAULT_ROOT_PATH, "config.yaml", "full_node")
    overrides = config["network_overrides"]["constants"][config["selected_network"]]
    constants: ConsensusConstants = DEFAULT_CONSTANTS.replace_str_to_bytes(**overrides)
    server = PoolServer(private_key, config, constants)

    # TODO: support TLS
    app = web.Application()
    app.add_routes(
        [
            web.get("/", server.wrap_http_handler(server.index)),
            web.get("/get_pool_info", server.wrap_http_handler(server.get_pool_info)),
            web.post("/submit_partial", server.wrap_http_handler(server.submit_partial)),
        ]
    )
    web.run_app(app)


if __name__ == "__main__":
    asyncio.run(start_pool_server())
