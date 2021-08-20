import asyncio
import logging
import pathlib
import time
import traceback
from asyncio import Task
from math import floor
from typing import Dict, Optional, Set, List, Tuple, Callable

from blspy import AugSchemeMPL, G1Element
from chia.consensus.block_rewards import calculate_pool_reward
from chia.pools.pool_wallet_info import PoolState, PoolSingletonState
from chia.protocols.pool_protocol import (
    PoolErrorCode,
    PostPartialRequest,
    PostPartialResponse,
    PostFarmerRequest,
    PostFarmerResponse,
    PutFarmerRequest,
    PutFarmerResponse,
    POOL_PROTOCOL_VERSION,
)
from chia.rpc.wallet_rpc_client import WalletRpcClient
from chia.types.blockchain_format.coin import Coin
from chia.types.coin_record import CoinRecord
from chia.types.coin_spend import CoinSpend
from chia.util.bech32m import decode_puzzle_hash
from chia.consensus.constants import ConsensusConstants
from chia.util.ints import uint8, uint16, uint32, uint64
from chia.util.byte_types import hexstr_to_bytes
from chia.util.default_root import DEFAULT_ROOT_PATH
from chia.rpc.full_node_rpc_client import FullNodeRpcClient
from chia.full_node.signage_point import SignagePoint
from chia.types.end_of_slot_bundle import EndOfSubSlotBundle
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.consensus.pot_iterations import calculate_iterations_quality
from chia.util.lru_cache import LRUCache
from chia.util.chia_logging import initialize_logging
from chia.wallet.transaction_record import TransactionRecord
from chia.pools.pool_puzzles import (
    get_most_recent_singleton_coin_from_coin_spend,
    get_delayed_puz_info_from_launcher_spend,
    launcher_id_to_p2_puzzle_hash,
)

from .difficulty_adjustment import get_new_difficulty
from .singleton import create_absorb_transaction, get_singleton_state, get_coin_spend, get_farmed_height
from .store.abstract import AbstractPoolStore
from .store.sqlite_store import SqlitePoolStore
from .record import FarmerRecord
from .util import error_dict, RequestMetadata
from .payment.payment import Payment


class Pool:
    def __init__(
        self,
        config: Dict,
        pool_config: Dict,
        constants: ConsensusConstants,
        pool_store: Optional[AbstractPoolStore] = None,
        difficulty_function: Callable = get_new_difficulty,
    ):
        self.follow_singleton_tasks: Dict[bytes32, asyncio.Task] = {}
        self.log = logging
        # If you want to log to a file: use filename='example.log', encoding='utf-8'
        self.log.basicConfig(level=logging.INFO)

        initialize_logging("pool", pool_config["logging"], pathlib.Path(pool_config["logging"]["log_path"]))

        # Set our pool info here
        self.info_default_res = pool_config["pool_info"]["default_res"]
        self.info_name = pool_config["pool_info"]["name"]
        self.info_logo_url = pool_config["pool_info"]["logo_url"]
        self.info_description = pool_config["pool_info"]["description"]
        self.welcome_message = pool_config["welcome_message"]

        self.config = config
        self.constants = constants

        self.store: AbstractPoolStore = pool_store or SqlitePoolStore()

        self.pool_fee = pool_config["pool_fee"]

        # This number should be held constant and be consistent for every pool in the network. DO NOT CHANGE
        self.iters_limit = self.constants.POOL_SUB_SLOT_ITERS // 64

        # This number should not be changed, since users will put this into their singletons
        self.relative_lock_height = uint32(pool_config["relative_lock_height"])

        # TODO(pool): potentially tweak these numbers for security and performance
        # This is what the user enters into the input field. This exact value will be stored on the blockchain
        self.pool_url = pool_config["pool_url"]
        self.min_difficulty = uint64(pool_config["min_difficulty"])  # 10 difficulty is about 1 proof a day per plot
        self.default_difficulty: uint64 = uint64(pool_config["default_difficulty"])
        self.difficulty_function: Callable = difficulty_function

        self.pending_point_partials: Optional[asyncio.Queue] = None
        self.recent_points_added: LRUCache = LRUCache(20000)

        # The time in minutes for an authentication token to be valid. See "Farmer authentication" in SPECIFICATION.md
        self.authentication_token_timeout: uint8 = pool_config["authentication_token_timeout"]

        # This is where the block rewards will get paid out to. The pool needs to support this address forever,
        # since the farmers will encode it into their singleton on the blockchain. WARNING: the default pool code
        # completely spends this wallet and distributes it to users, do don't put any additional funds in here
        # that you do not want to distribute. Even if the funds are in a different address than this one, they WILL
        # be spent by this code! So only put funds that you want to distribute to pool members here.

        # Using 2164248527
        self.default_target_puzzle_hash: bytes32 = bytes32(decode_puzzle_hash(pool_config["default_target_address"]))

        # The pool fees will be sent to this address. This MUST be on a different key than the target_puzzle_hash,
        # otherwise, the fees will be sent to the users. Using 690783650
        self.pool_fee_puzzle_hash: bytes32 = bytes32(decode_puzzle_hash(pool_config["pool_fee_address"]))

        # This is the wallet fingerprint and ID for the wallet spending the funds from `self.default_target_puzzle_hash`
        self.wallet_fingerprint = pool_config["wallet_fingerprint"]
        self.wallet_id = pool_config["wallet_id"]

        # We need to check for slow farmers. If farmers cannot submit proofs in time, they won't be able to win
        # any rewards either. This number can be tweaked to be more or less strict. More strict ensures everyone
        # gets high rewards, but it might cause some of the slower farmers to not be able to participate in the pool.
        self.partial_time_limit: int = pool_config["partial_time_limit"]

        # There is always a risk of a reorg, in which case we cannot reward farmers that submitted partials in that
        # reorg. That is why we have a time delay before changing any account points.
        self.partial_confirmation_delay: int = pool_config["partial_confirmation_delay"]

        # Only allow PUT /farmer per launcher_id every n seconds to prevent difficulty change attacks.
        self.farmer_update_blocked: set = set()
        self.farmer_update_cooldown_seconds: int = 600

        # These are the phs that we want to look for on chain, that we can claim to our pool
        self.scan_p2_singleton_puzzle_hashes: Set[bytes32] = set()

        # Don't scan anything before this height, for efficiency (for example pool start date)
        self.scan_start_height: uint32 = uint32(pool_config["scan_start_height"])

        # Interval for scanning and collecting the pool rewards
        self.collect_pool_rewards_interval = pool_config["collect_pool_rewards_interval"]

        # After this many confirmations, a transaction is considered final and irreversible
        self.confirmation_security_threshold = pool_config["confirmation_security_threshold"]

        # Interval for making payout transactions to farmers
        self.payment_interval = pool_config["payment_interval"]

        # We will not make transactions with more targets than this, to ensure our transaction gets into the blockchain
        # faster.
        self.max_additions_per_transaction = pool_config["max_additions_per_transaction"]

        # Keeps track of the latest state of our node
        self.blockchain_state = {"peak": None}

        # Whether or not the wallet is synced (required to make payments)
        self.wallet_synced = False

        # We target these many partials for this number of seconds. We adjust after receiving this many partials.
        self.number_of_partials_target: int = pool_config["number_of_partials_target"]
        self.time_target: int = pool_config["time_target"]

        # Hostname for RPC calls.
        self.rpc_hostname = pool_config.get("self_hostname") or self.config["self_hostname"]

        # Whether or not to start the payment submodule.
        self.payments_enabled = pool_config.get("payments_enabled")

        # import payment submodule and give it a variable if self.payments_enabled is False
        if self.payments_enabled is True or None:
            from .payment.payment import Payment
            self.payments = Payment(self.config, self.constants, pool_config, self.store)

        # Tasks (infinite While loops) for different purposes
        self.confirm_partials_loop_task: Optional[asyncio.Task] = None
        self.get_peak_loop_task: Optional[asyncio.Task] = None

        self.node_rpc_client: Optional[FullNodeRpcClient] = None
        self.node_rpc_port = pool_config["node_rpc_port"]
        self.wallet_rpc_client: Optional[WalletRpcClient] = None
        self.wallet_rpc_port = pool_config["wallet_rpc_port"]
        self.rpc_hostname = pool_config.get("self_hostname") or self.config["self_hostname"]
        self.payments_enabled = pool_config.get("payments_enabled")

    async def start(self):
        await self.store.connect()
        self.pending_point_partials = asyncio.Queue()
        if self.payments_enabled is True or None:
            await self.payments.start()
            self.node_rpc_client = self.payments.node_rpc_client

        else:
            self.node_rpc_client = await FullNodeRpcClient.create(
                self.rpc_hostname, uint16(self.node_rpc_port), DEFAULT_ROOT_PATH, self.config
            )
            self.blockchain_state = await self.node_rpc_client.get_blockchain_state()
            self.get_peak_loop_task = asyncio.create_task(self.get_peak_loop())
            if self.blockchain_state['sync']['synced']:
                self.log.info("Connected to node. Node is Synced.")
            else:
                self.log.info("Connected to node. Node is Syncing.")

        self.scan_p2_singleton_puzzle_hashes = await self.store.get_pay_to_singleton_phs()

        self.confirm_partials_loop_task = asyncio.create_task(self.confirm_partials_loop())

    async def stop(self):
        if self.confirm_partials_loop_task is not None:
            self.confirm_partials_loop_task.cancel()
        if self.get_peak_loop_task is not None:
            self.get_peak_loop_task.cancel()
        if self.payments_enabled is True or None:
            await self.payments.stop()
        else:
            self.node_rpc_client.close()
            await self.node_rpc_client.await_closed()
        await self.store.connection.close()

    async def get_peak_loop(self):
        """
        Periodically contacts the full node to get the latest state of the blockchain
        """
        while True:
            try:
                self.blockchain_state = await self.node_rpc_client.get_blockchain_state()
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.log.info("Cancelled get_peak_loop, closing")
                return
            except Exception as e:
                self.log.error(f"Unexpected error in get_peak_loop: {e}")
                await asyncio.sleep(30)

    async def confirm_partials_loop(self):
        """
        Pulls things from the queue of partials one at a time, and adjusts balances.
        """

        while True:
            try:
                # The points are based on the difficulty at the time of partial submission, not at the time of
                # confirmation
                partial, time_received, points_received = await self.pending_point_partials.get()

                # Wait a few minutes to check if partial is still valid in the blockchain (no reorgs)
                await asyncio.sleep((max(0, time_received + self.partial_confirmation_delay - time.time() - 5)))

                # Starts a task to check the remaining things for this partial and optionally update points
                asyncio.create_task(self.check_and_confirm_partial(partial, points_received))
            except asyncio.CancelledError:
                self.log.info("Cancelled confirm partials loop, closing")
                return
            except Exception as e:
                self.log.error(f"Unexpected error: {e}")

    async def check_and_confirm_partial(self, partial: PostPartialRequest, points_received: uint64) -> None:
        try:
            # TODO(pool): these lookups to the full node are not efficient and can be cached, especially for
            #  scaling to many users
            if partial.payload.end_of_sub_slot:
                response = await self.node_rpc_client.get_recent_signage_point_or_eos(None, partial.payload.sp_hash)
                if response is None or response["reverted"]:
                    self.log.info(f"Partial EOS reverted: {partial.payload.sp_hash}")
                    return
            else:
                response = await self.node_rpc_client.get_recent_signage_point_or_eos(partial.payload.sp_hash, None)
                if response is None or response["reverted"]:
                    self.log.info(f"Partial SP reverted: {partial.payload.sp_hash}")
                    return

            # Now we know that the partial came on time, but also that the signage point / EOS is still in the
            # blockchain. We need to check for double submissions.
            pos_hash = partial.payload.proof_of_space.get_hash()
            if self.recent_points_added.get(pos_hash):
                self.log.info(f"Double signage point submitted for proof: {partial.payload}")
                return
            self.recent_points_added.put(pos_hash, uint64(1))

            # Now we need to check to see that the singleton in the blockchain is still assigned to this pool
            singleton_state_tuple: Optional[
                Tuple[CoinSpend, PoolState, bool]
            ] = await self.get_and_validate_singleton_state(partial.payload.launcher_id)

            if singleton_state_tuple is None:
                self.log.info(f"Invalid singleton {partial.payload.launcher_id}")
                return

            _, _, is_member = singleton_state_tuple
            if not is_member:
                self.log.info(f"Singleton is not assigned to this pool")
                return

            async with self.store.lock:
                farmer_record: Optional[FarmerRecord] = await self.store.get_farmer_record(partial.payload.launcher_id)

                assert (
                    partial.payload.proof_of_space.pool_contract_puzzle_hash == farmer_record.p2_singleton_puzzle_hash
                )

                if farmer_record.is_pool_member:
                    await self.store.add_partial(partial.payload.launcher_id, partial.payload.harvester_id,
                                                 uint64(int(time.time())), points_received)
                    self.log.info(
                        f"Farmer {farmer_record.launcher_id}/{partial.payload.harvester_id} updated points to: "
                        f"{farmer_record.points + points_received}"
                    )
        except Exception as e:
            error_stack = traceback.format_exc()
            self.log.error(f"Exception in confirming partial: {e} {error_stack}")

    async def add_farmer(self, request: PostFarmerRequest, metadata: RequestMetadata) -> Dict:
        async with self.store.lock:
            farmer_record: Optional[FarmerRecord] = await self.store.get_farmer_record(request.payload.launcher_id)
            if farmer_record is not None:
                return error_dict(
                    PoolErrorCode.FARMER_ALREADY_KNOWN,
                    f"Farmer with launcher_id {request.payload.launcher_id} already known.",
                )

            singleton_state_tuple: Optional[
                Tuple[CoinSpend, PoolState, bool]
            ] = await self.get_and_validate_singleton_state(request.payload.launcher_id)

            if singleton_state_tuple is None:
                return error_dict(PoolErrorCode.INVALID_SINGLETON, f"Invalid singleton {request.payload.launcher_id}")

            last_spend, last_state, is_member = singleton_state_tuple
            if is_member is None:
                return error_dict(PoolErrorCode.INVALID_SINGLETON, f"Singleton is not assigned to this pool")

            if (
                request.payload.suggested_difficulty is None
                or request.payload.suggested_difficulty < self.min_difficulty
            ):
                difficulty: uint64 = self.default_difficulty
            else:
                difficulty = request.payload.suggested_difficulty

            if len(hexstr_to_bytes(request.payload.payout_instructions)) != 32:
                return error_dict(
                    PoolErrorCode.INVALID_PAYOUT_INSTRUCTIONS,
                    f"Payout instructions must be an xch address for this pool.",
                )

            if not AugSchemeMPL.verify(last_state.owner_pubkey, request.payload.get_hash(), request.signature):
                return error_dict(PoolErrorCode.INVALID_SIGNATURE, f"Invalid signature")

            launcher_coin: Optional[CoinRecord] = await self.node_rpc_client.get_coin_record_by_name(
                request.payload.launcher_id
            )
            assert launcher_coin is not None and launcher_coin.spent

            launcher_solution: Optional[CoinSpend] = await get_coin_spend(self.node_rpc_client, launcher_coin)
            delay_time, delay_puzzle_hash = get_delayed_puz_info_from_launcher_spend(launcher_solution)

            if delay_time < 3600:
                return error_dict(PoolErrorCode.DELAY_TIME_TOO_SHORT, f"Delay time too short, must be at least 1 hour")

            p2_singleton_puzzle_hash = launcher_id_to_p2_puzzle_hash(
                request.payload.launcher_id, delay_time, delay_puzzle_hash
            )

            farmer_record = FarmerRecord(
                request.payload.launcher_id,
                p2_singleton_puzzle_hash,
                delay_time,
                delay_puzzle_hash,
                request.payload.authentication_public_key,
                last_spend,
                last_state,
                uint64(0),
                difficulty,
                request.payload.payout_instructions,
                True,
                uint64(time.time()),
            )
            self.scan_p2_singleton_puzzle_hashes.add(p2_singleton_puzzle_hash)
            await self.store.add_farmer_record(farmer_record, metadata)

            return PostFarmerResponse(self.welcome_message).to_json_dict()

    async def update_farmer(self, request: PutFarmerRequest, metadata: RequestMetadata) -> Dict:
        launcher_id = request.payload.launcher_id
        # First check if this launcher_id is currently blocked for farmer updates, if so there is no reason to validate
        # all the stuff below
        if launcher_id in self.farmer_update_blocked:
            return error_dict(PoolErrorCode.REQUEST_FAILED, f"Cannot update farmer yet.")
        farmer_record: Optional[FarmerRecord] = await self.store.get_farmer_record(launcher_id)
        if farmer_record is None:
            return error_dict(PoolErrorCode.FARMER_NOT_KNOWN, f"Farmer with launcher_id {launcher_id} not known.")

        singleton_state_tuple: Optional[
            Tuple[CoinSpend, PoolState, bool]
        ] = await self.get_and_validate_singleton_state(launcher_id)

        if singleton_state_tuple is None:
            return error_dict(PoolErrorCode.INVALID_SINGLETON, f"Invalid singleton {request.payload.launcher_id}")

        last_spend, last_state, is_member = singleton_state_tuple
        if is_member is None:
            return error_dict(PoolErrorCode.INVALID_SINGLETON, f"Singleton is not assigned to this pool")

        if not AugSchemeMPL.verify(last_state.owner_pubkey, request.payload.get_hash(), request.signature):
            return error_dict(PoolErrorCode.INVALID_SIGNATURE, f"Invalid signature")

        farmer_dict = farmer_record.to_json_dict()
        response_dict = {}
        if request.payload.authentication_public_key is not None:
            is_new_value = farmer_record.authentication_public_key != request.payload.authentication_public_key
            response_dict["authentication_public_key"] = is_new_value
            if is_new_value:
                farmer_dict["authentication_public_key"] = request.payload.authentication_public_key

        if request.payload.payout_instructions is not None:
            is_new_value = (
                farmer_record.payout_instructions != request.payload.payout_instructions
                and request.payload.payout_instructions is not None
                and len(hexstr_to_bytes(request.payload.payout_instructions)) == 32
            )
            response_dict["payout_instructions"] = is_new_value
            if is_new_value:
                farmer_dict["payout_instructions"] = request.payload.payout_instructions

        if request.payload.suggested_difficulty is not None:
            is_new_value = (
                farmer_record.difficulty != request.payload.suggested_difficulty
                and request.payload.suggested_difficulty is not None
                and request.payload.suggested_difficulty >= self.min_difficulty
            )
            response_dict["suggested_difficulty"] = is_new_value
            if is_new_value:
                farmer_dict["difficulty"] = request.payload.suggested_difficulty

        async def update_farmer_later():
            await asyncio.sleep(self.farmer_update_cooldown_seconds)
            await self.store.add_farmer_record(FarmerRecord.from_json_dict(farmer_dict), metadata)
            self.farmer_update_blocked.remove(launcher_id)
            self.log.info(f"Updated farmer: {response_dict}")

        self.farmer_update_blocked.add(launcher_id)
        asyncio.create_task(update_farmer_later())

        # TODO Fix chia-blockchain's Streamable implementation to support Optional in `from_json_dict`, then use
        # PutFarmerResponse here and in the trace up.
        return response_dict

    async def get_and_validate_singleton_state(
        self, launcher_id: bytes32
    ) -> Optional[Tuple[CoinSpend, PoolState, bool]]:
        """
        :return: the state of the singleton, if it currently exists in the blockchain, and if it is assigned to
        our pool, with the correct parameters. Otherwise, None. Note that this state must be buried (recent state
        changes are not returned)
        """
        singleton_task: Optional[Task] = self.follow_singleton_tasks.get(launcher_id, None)
        remove_after = False
        farmer_rec = None
        if singleton_task is None or singleton_task.done():
            farmer_rec: Optional[FarmerRecord] = await self.store.get_farmer_record(launcher_id)
            singleton_task = asyncio.create_task(
                get_singleton_state(
                    self.node_rpc_client,
                    launcher_id,
                    farmer_rec,
                    self.blockchain_state["peak"].height,
                    self.confirmation_security_threshold,
                    self.constants.GENESIS_CHALLENGE,
                )
            )
            self.follow_singleton_tasks[launcher_id] = singleton_task
            remove_after = True

        optional_result: Optional[Tuple[CoinSpend, PoolState, PoolState]] = await singleton_task
        if remove_after and launcher_id in self.follow_singleton_tasks:
            await self.follow_singleton_tasks.pop(launcher_id)

        if optional_result is None:
            return None

        buried_singleton_tip, buried_singleton_tip_state, singleton_tip_state = optional_result

        # Validate state of the singleton
        is_pool_member = True
        if singleton_tip_state.target_puzzle_hash != self.default_target_puzzle_hash:
            self.log.info(
                f"Wrong target puzzle hash: {singleton_tip_state.target_puzzle_hash} for launcher_id {launcher_id}"
            )
            is_pool_member = False
        elif singleton_tip_state.relative_lock_height != self.relative_lock_height:
            self.log.info(
                f"Wrong relative lock height: {singleton_tip_state.relative_lock_height} for launcher_id {launcher_id}"
            )
            is_pool_member = False
        elif singleton_tip_state.version != POOL_PROTOCOL_VERSION:
            self.log.info(f"Wrong version {singleton_tip_state.version} for launcher_id {launcher_id}")
            is_pool_member = False
        elif singleton_tip_state.state == PoolSingletonState.SELF_POOLING.value:
            self.log.info(f"Invalid singleton state {singleton_tip_state.state} for launcher_id {launcher_id}")
            is_pool_member = False
        elif singleton_tip_state.state == PoolSingletonState.LEAVING_POOL.value:
            coin_record: Optional[CoinRecord] = await self.node_rpc_client.get_coin_record_by_name(
                buried_singleton_tip.coin.name()
            )
            assert coin_record is not None
            if self.blockchain_state["peak"].height - coin_record.confirmed_block_index > self.relative_lock_height:
                self.log.info(f"launcher_id {launcher_id} got enough confirmations to leave the pool")
                is_pool_member = False

        self.log.info(f"Is {launcher_id} pool member: {is_pool_member}")

        if farmer_rec is not None and (
            farmer_rec.singleton_tip != buried_singleton_tip
            or farmer_rec.singleton_tip_state != buried_singleton_tip_state
        ):
            # This means the singleton has been changed in the blockchain (either by us or someone else). We
            # still keep track of this singleton if the farmer has changed to a different pool, in case they
            # switch back.
            self.log.info(f"Updating singleton state for {launcher_id}")
            await self.store.update_singleton(
                launcher_id, buried_singleton_tip, buried_singleton_tip_state, is_pool_member
            )

        return buried_singleton_tip, buried_singleton_tip_state, is_pool_member

    async def process_partial(
        self,
        partial: PostPartialRequest,
        farmer_record: FarmerRecord,
        time_received_partial: uint64,
    ) -> Dict:
        # Validate signatures
        message: bytes32 = partial.payload.get_hash()
        pk1: G1Element = partial.payload.proof_of_space.plot_public_key
        pk2: G1Element = farmer_record.authentication_public_key
        valid_sig = AugSchemeMPL.aggregate_verify([pk1, pk2], [message, message], partial.aggregate_signature)
        if not valid_sig:
            return error_dict(
                PoolErrorCode.INVALID_SIGNATURE,
                f"The aggregate signature is invalid {partial.aggregate_signature}",
            )

        if partial.payload.proof_of_space.pool_contract_puzzle_hash != farmer_record.p2_singleton_puzzle_hash:
            return error_dict(
                PoolErrorCode.INVALID_P2_SINGLETON_PUZZLE_HASH,
                f"Invalid pool contract puzzle hash {partial.payload.proof_of_space.pool_contract_puzzle_hash}",
            )

        async def get_signage_point_or_eos():
            if partial.payload.end_of_sub_slot:
                return await self.node_rpc_client.get_recent_signage_point_or_eos(None, partial.payload.sp_hash)
            else:
                return await self.node_rpc_client.get_recent_signage_point_or_eos(partial.payload.sp_hash, None)

        response = await get_signage_point_or_eos()
        if response is None:
            # Try again after 10 seconds in case we just didn't yet receive the signage point
            await asyncio.sleep(10)
            response = await get_signage_point_or_eos()

        if response is None or response["reverted"]:
            return error_dict(
                PoolErrorCode.NOT_FOUND, f"Did not find signage point or EOS {partial.payload.sp_hash}, {response}"
            )
        node_time_received_sp = response["time_received"]

        signage_point: Optional[SignagePoint] = response.get("signage_point", None)
        end_of_sub_slot: Optional[EndOfSubSlotBundle] = response.get("eos", None)

        if time_received_partial - node_time_received_sp > self.partial_time_limit:
            return error_dict(
                PoolErrorCode.TOO_LATE,
                f"Received partial in {time_received_partial - node_time_received_sp}. "
                f"Make sure your proof of space lookups are fast, and network connectivity is good."
                f"Response must happen in less than {self.partial_time_limit} seconds. NAS or network"
                f" farming can be an issue",
            )

        # Validate the proof
        if signage_point is not None:
            challenge_hash: bytes32 = signage_point.cc_vdf.challenge
        else:
            challenge_hash = end_of_sub_slot.challenge_chain.get_hash()

        quality_string: Optional[bytes32] = partial.payload.proof_of_space.verify_and_get_quality_string(
            self.constants, challenge_hash, partial.payload.sp_hash
        )
        if quality_string is None:
            return error_dict(PoolErrorCode.INVALID_PROOF, f"Invalid proof of space {partial.payload.sp_hash}")

        current_difficulty = farmer_record.difficulty
        required_iters: uint64 = calculate_iterations_quality(
            self.constants.DIFFICULTY_CONSTANT_FACTOR,
            quality_string,
            partial.payload.proof_of_space.size,
            current_difficulty,
            partial.payload.sp_hash,
        )

        if required_iters >= self.iters_limit:
            return error_dict(
                PoolErrorCode.PROOF_NOT_GOOD_ENOUGH,
                f"Proof of space has required iters {required_iters}, too high for difficulty " f"{current_difficulty}",
            )

        await self.pending_point_partials.put((partial, time_received_partial, current_difficulty))

        async with self.store.lock:
            # Obtains the new record in case we just updated difficulty
            farmer_record: Optional[FarmerRecord] = await self.store.get_farmer_record(partial.payload.launcher_id)
            if farmer_record is not None:
                current_difficulty = farmer_record.difficulty
                # Decide whether to update the difficulty
                recent_partials = await self.store.get_recent_partials(
                    partial.payload.launcher_id, self.number_of_partials_target
                )
                # Only update the difficulty if we meet certain conditions
                new_difficulty: uint64 = self.difficulty_function(
                    recent_partials,
                    int(self.number_of_partials_target),
                    int(self.time_target),
                    current_difficulty,
                    time_received_partial,
                    self.min_difficulty,
                )

                if current_difficulty != new_difficulty:
                    await self.store.update_difficulty(partial.payload.launcher_id, new_difficulty)
                    current_difficulty = new_difficulty

        return PostPartialResponse(current_difficulty).to_json_dict()
