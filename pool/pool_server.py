import asyncio
import logging
import time
import traceback
from typing import Dict, Callable, Optional

import aiohttp
from blspy import AugSchemeMPL, PrivateKey
from aiohttp import web
from chia.pools.pool_wallet_info import POOL_PROTOCOL_VERSION
from chia.protocols.pool_protocol import SubmitPartial, PoolInfo
from chia.util.hash import std_hash
from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.consensus.constants import ConsensusConstants
from chia.util.json_util import obj_to_response
from chia.util.ints import uint64, uint32
from chia.util.default_root import DEFAULT_ROOT_PATH
from chia.util.config import load_config

from error_codes import PoolErr
from store import FarmerRecord
from pool import Pool


def allow_cors(response: web.Response) -> web.Response:
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


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
            try:
                res_object = await f(request)
                if res_object is None:
                    res_object = {}
            except Exception as e:
                tb = traceback.format_exc()
                self.log.warning(f"Error while handling message: {tb}")
                if len(e.args) > 0:
                    res_object = {"error_code": PoolErr.SERVER_EXCEPTION.value, "error_message": f"{e.args[0]}"}
                else:
                    res_object = {"error_code": PoolErr.SERVER_EXCEPTION.value, "error_message": f"{e}"}

                return allow_cors(obj_to_response(res_object))
            return allow_cors(res_object)

        return inner

    async def index(self, _) -> web.Response:
        return web.Response(text="Chia reference pool")

    async def get_pool_info(self, _) -> web.Response:
        res: PoolInfo = PoolInfo(
            self.pool.info_name,
            self.pool.info_logo_url,
            uint64(self.pool.min_difficulty),
            uint32(self.pool.relative_lock_height),
            str(POOL_PROTOCOL_VERSION),
            str(self.pool.pool_fee),
            self.pool.info_description,
            self.pool.default_target_puzzle_hash,
        )
        return obj_to_response(res)

    async def submit_partial(self, request_obj) -> web.Response:
        start_time = time.time()
        request = await request_obj.json()
        # TODO(pool): add rate limiting
        partial: SubmitPartial = SubmitPartial.from_json_dict(request)
        time_received_partial = uint64(int(time.time()))

        # It's important that on the first request from this farmer, the default difficulty is used. Changing the
        # difficulty requires a few minutes, otherwise farmers can abuse by setting the difficulty right under the
        # proof that they found.
        farmer_record: Optional[FarmerRecord] = await self.pool.store.get_farmer_record(partial.payload.launcher_id)
        if farmer_record is not None:
            current_difficulty: uint64 = farmer_record.difficulty
            balance = farmer_record.points
        else:
            current_difficulty = self.pool.default_difficulty
            balance = uint64(0)

        async def await_and_call(cor, *args):
            # 10 seconds gives our node some time to get the signage point, in case we are slightly slowed down
            await asyncio.sleep(10)
            res = await cor(args)
            self.pool.log.info(f"Delayed response: {res}")

        res_dict = await self.pool.process_partial(partial, time_received_partial, balance, current_difficulty, True)

        if "error_code" in res_dict and "error_code" == PoolErr.NOT_FOUND.value:
            asyncio.create_task(
                await_and_call(
                    self.pool.process_partial, partial, time_received_partial, balance, current_difficulty, False
                )
            )

        self.pool.log.info(
            f"Returning {res_dict}, time: {time.time() - start_time} " f"singleton: {request['payload']['launcher_id']}"
        )
        return obj_to_response(res_dict)


server: PoolServer = None
runner = None


async def start_pool_server():
    global server
    global runner
    private_key: PrivateKey = AugSchemeMPL.key_gen(std_hash(b"123"))
    config = load_config(DEFAULT_ROOT_PATH, "config.yaml")
    overrides = config["network_overrides"]["constants"][config["selected_network"]]
    constants: ConsensusConstants = DEFAULT_CONSTANTS.replace_str_to_bytes(**overrides)
    server = PoolServer(private_key, config, constants)
    await server.start()

    # TODO(pool): support TLS
    app = web.Application()
    app.add_routes(
        [
            web.get("/", server.wrap_http_handler(server.index)),
            web.get("/pool_info", server.wrap_http_handler(server.get_pool_info)),
            web.post("/partial", server.wrap_http_handler(server.submit_partial)),
        ]
    )
    runner = aiohttp.web.AppRunner(app, access_log=None)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "0.0.0.0", int(80))
    await site.start()
    await asyncio.sleep(10000000)


async def stop():
    await server.stop()
    await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(start_pool_server())
    except KeyboardInterrupt:
        asyncio.run(stop())
