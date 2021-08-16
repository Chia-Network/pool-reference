import asyncio
import logging
import os
import ssl
import time
import traceback
from typing import Dict, Callable, Optional

import aiohttp
import yaml
from blspy import AugSchemeMPL, G2Element
from aiohttp import web
from chia.protocols.pool_protocol import (
    PoolErrorCode,
    GetFarmerResponse,
    GetPoolInfoResponse,
    PostPartialRequest,
    PostFarmerRequest,
    PutFarmerRequest,
    validate_authentication_token,
    POOL_PROTOCOL_VERSION,
    AuthenticationPayload,
)
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.byte_types import hexstr_to_bytes
from chia.util.hash import std_hash
from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.consensus.constants import ConsensusConstants
from chia.util.json_util import obj_to_response
from chia.util.ints import uint8, uint64, uint32
from chia.util.default_root import DEFAULT_ROOT_PATH
from chia.util.config import load_config

from .record import FarmerRecord
from .pool import Pool
from .store.abstract import AbstractPoolStore
from .util import error_response, RequestMetadata


def allow_cors(response: web.Response) -> web.Response:
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


def check_authentication_token(launcher_id: bytes32, token: uint64, timeout: uint8) -> Optional[web.Response]:
    if not validate_authentication_token(token, timeout):
        return error_response(
            PoolErrorCode.INVALID_AUTHENTICATION_TOKEN,
            f"authentication_token {token} invalid for farmer {launcher_id.hex()}.",
        )
    return None


def get_ssl_context(config):
    if config["server"]["server_use_ssl"] is False:
        return None
    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ssl_context.load_cert_chain(config["server"]["server_ssl_crt"], config["server"]["server_ssl_key"])
    return ssl_context


class PoolServer:
    def __init__(self, config: Dict, constants: ConsensusConstants, pool_store: Optional[AbstractPoolStore] = None):

        # We load our configurations from here
        with open(os.getcwd() + "/config.yaml") as f:
            pool_config: Dict = yaml.safe_load(f)

        self.log = logging.getLogger(__name__)
        self.pool = Pool(config, pool_config, constants, pool_store)

        self.pool_config = pool_config
        self.host = pool_config["server"]["server_host"]
        self.port = int(pool_config["server"]["server_port"])

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
                    res_error = error_response(PoolErrorCode.SERVER_EXCEPTION, f"{e.args[0]}")
                else:
                    res_error = error_response(PoolErrorCode.SERVER_EXCEPTION, f"{e}")
                return allow_cors(res_error)

            return allow_cors(res_object)

        return inner

    async def index(self, _) -> web.Response:
        return web.Response(text="Chia reference pool")

    async def get_pool_info(self, _) -> web.Response:
        res: GetPoolInfoResponse = GetPoolInfoResponse(
            self.pool.info_name,
            self.pool.info_logo_url,
            uint64(self.pool.min_difficulty),
            uint32(self.pool.relative_lock_height),
            POOL_PROTOCOL_VERSION,
            str(self.pool.pool_fee),
            self.pool.info_description,
            self.pool.default_target_puzzle_hash,
            self.pool.authentication_token_timeout,
        )
        return obj_to_response(res)

    async def get_farmer(self, request_obj) -> web.Response:
        # TODO(pool): add rate limiting
        launcher_id: bytes32 = hexstr_to_bytes(request_obj.rel_url.query["launcher_id"])
        authentication_token = uint64(request_obj.rel_url.query["authentication_token"])

        authentication_token_error: Optional[web.Response] = check_authentication_token(
            launcher_id, authentication_token, self.pool.authentication_token_timeout
        )
        if authentication_token_error is not None:
            return authentication_token_error

        farmer_record: Optional[FarmerRecord] = await self.pool.store.get_farmer_record(launcher_id)
        if farmer_record is None:
            return error_response(
                PoolErrorCode.FARMER_NOT_KNOWN, f"Farmer with launcher_id {launcher_id.hex()} unknown."
            )

        # Validate provided signature
        signature: G2Element = G2Element.from_bytes(hexstr_to_bytes(request_obj.rel_url.query["signature"]))
        message: bytes32 = std_hash(
            AuthenticationPayload("get_farmer", launcher_id, self.pool.default_target_puzzle_hash, authentication_token)
        )
        if not AugSchemeMPL.verify(farmer_record.authentication_public_key, message, signature):
            return error_response(
                PoolErrorCode.INVALID_SIGNATURE,
                f"Failed to verify signature {signature} for launcher_id {launcher_id.hex()}.",
            )

        response: GetFarmerResponse = GetFarmerResponse(
            farmer_record.authentication_public_key,
            farmer_record.payout_instructions,
            farmer_record.difficulty,
            farmer_record.points,
        )

        self.pool.log.info(f"get_farmer response {response.to_json_dict()}, " f"launcher_id: {launcher_id.hex()}")
        return obj_to_response(response)

    def post_metadata_from_request(self, request_obj):
        return RequestMetadata(
            url=str(request_obj.url),
            scheme=request_obj.scheme,
            headers=request_obj.headers,
            cookies=dict(request_obj.cookies),
            query=dict(request_obj.query),
            remote=request_obj.remote,
        )

    async def post_farmer(self, request_obj) -> web.Response:
        # TODO(pool): add rate limiting
        post_farmer_request: PostFarmerRequest = PostFarmerRequest.from_json_dict(await request_obj.json())

        authentication_token_error = check_authentication_token(
            post_farmer_request.payload.launcher_id,
            post_farmer_request.payload.authentication_token,
            self.pool.authentication_token_timeout,
        )
        if authentication_token_error is not None:
            return authentication_token_error

        post_farmer_response = await self.pool.add_farmer(
            post_farmer_request, self.post_metadata_from_request(request_obj))

        self.pool.log.info(
            f"post_farmer response {post_farmer_response}, "
            f"launcher_id: {post_farmer_request.payload.launcher_id.hex()}",
        )
        return obj_to_response(post_farmer_response)

    async def put_farmer(self, request_obj) -> web.Response:
        # TODO(pool): add rate limiting
        put_farmer_request: PutFarmerRequest = PutFarmerRequest.from_json_dict(await request_obj.json())

        authentication_token_error = check_authentication_token(
            put_farmer_request.payload.launcher_id,
            put_farmer_request.payload.authentication_token,
            self.pool.authentication_token_timeout,
        )
        if authentication_token_error is not None:
            return authentication_token_error

        # Process the request
        put_farmer_response = await self.pool.update_farmer(put_farmer_request,
                                                            self.post_metadata_from_request(request_obj))

        self.pool.log.info(
            f"put_farmer response {put_farmer_response}, "
            f"launcher_id: {put_farmer_request.payload.launcher_id.hex()}",
        )
        return obj_to_response(put_farmer_response)

    async def post_partial(self, request_obj) -> web.Response:
        # TODO(pool): add rate limiting
        start_time = time.time()
        request = await request_obj.json()
        partial: PostPartialRequest = PostPartialRequest.from_json_dict(request)

        authentication_token_error = check_authentication_token(
            partial.payload.launcher_id,
            partial.payload.authentication_token,
            self.pool.authentication_token_timeout,
        )
        if authentication_token_error is not None:
            return authentication_token_error

        farmer_record: Optional[FarmerRecord] = await self.pool.store.get_farmer_record(partial.payload.launcher_id)
        if farmer_record is None:
            return error_response(
                PoolErrorCode.FARMER_NOT_KNOWN,
                f"Farmer with launcher_id {partial.payload.launcher_id.hex()} not known.",
            )

        post_partial_response = await self.pool.process_partial(partial, farmer_record, uint64(int(start_time)))

        self.pool.log.info(
            f"post_partial response {post_partial_response}, time: {time.time() - start_time} "
            f"launcher_id: {request['payload']['launcher_id']}"
        )
        return obj_to_response(post_partial_response)

    async def get_login(self, request_obj) -> web.Response:
        # TODO(pool): add rate limiting
        launcher_id: bytes32 = hexstr_to_bytes(request_obj.rel_url.query["launcher_id"])
        authentication_token: uint64 = uint64(request_obj.rel_url.query["authentication_token"])
        authentication_token_error = check_authentication_token(
            launcher_id, authentication_token, self.pool.authentication_token_timeout
        )
        if authentication_token_error is not None:
            return authentication_token_error

        farmer_record: Optional[FarmerRecord] = await self.pool.store.get_farmer_record(launcher_id)
        if farmer_record is None:
            return error_response(
                PoolErrorCode.FARMER_NOT_KNOWN, f"Farmer with launcher_id {launcher_id.hex()} unknown."
            )

        # Validate provided signature
        signature: G2Element = G2Element.from_bytes(hexstr_to_bytes(request_obj.rel_url.query["signature"]))
        message: bytes32 = std_hash(
            AuthenticationPayload("get_login", launcher_id, self.pool.default_target_puzzle_hash, authentication_token)
        )
        if not AugSchemeMPL.verify(farmer_record.authentication_public_key, message, signature):
            return error_response(
                PoolErrorCode.INVALID_SIGNATURE,
                f"Failed to verify signature {signature} for launcher_id {launcher_id.hex()}.",
            )

        self.pool.log.info(f"Login successful for launcher_id: {launcher_id.hex()}")

        return await self.login_response(launcher_id)

    async def login_response(self, launcher_id):
        record: Optional[FarmerRecord] = await self.pool.store.get_farmer_record(launcher_id)
        response = {}
        if record is not None:
            response["farmer_record"] = record
            recent_partials = await self.pool.store.get_recent_partials(launcher_id, 20)
            response["recent_partials"] = recent_partials

        return obj_to_response(response)


server: Optional[PoolServer] = None
runner: Optional[aiohttp.web.BaseRunner] = None


async def start_pool_server(pool_store: Optional[AbstractPoolStore] = None):
    global server
    global runner
    config = load_config(DEFAULT_ROOT_PATH, "config.yaml")
    overrides = config["network_overrides"]["constants"][config["selected_network"]]
    constants: ConsensusConstants = DEFAULT_CONSTANTS.replace_str_to_bytes(**overrides)
    server = PoolServer(config, constants, pool_store)
    await server.start()

    app = web.Application()
    app.add_routes(
        [
            web.get("/", server.wrap_http_handler(server.index)),
            web.get("/pool_info", server.wrap_http_handler(server.get_pool_info)),
            web.get("/farmer", server.wrap_http_handler(server.get_farmer)),
            web.post("/farmer", server.wrap_http_handler(server.post_farmer)),
            web.put("/farmer", server.wrap_http_handler(server.put_farmer)),
            web.post("/partial", server.wrap_http_handler(server.post_partial)),
            web.get("/login", server.wrap_http_handler(server.get_login)),
        ]
    )
    runner = aiohttp.web.AppRunner(app, access_log=None)
    await runner.setup()
    ssl_context = get_ssl_context(server.pool_config)
    site = aiohttp.web.TCPSite(
        runner,
        host=server.host,
        port=server.port,
        ssl_context=ssl_context,
    )
    await site.start()

    while True:
        await asyncio.sleep(3600)


async def stop():
    await server.stop()
    await runner.cleanup()


def main():
    try:
        asyncio.run(start_pool_server())
    except KeyboardInterrupt:
        asyncio.run(stop())


if __name__ == "__main__":
    main()
