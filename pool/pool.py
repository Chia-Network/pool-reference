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

        # This is the list of payments that we have not sent yet, to farmers
        self.pending_payments: Optional[asyncio.Queue] = None

        # Keeps track of the latest state of our node
        self.blockchain_state = {"peak": None}

        # Whether or not the wallet is synced (required to make payments)
        self.wallet_synced = False

        # We target these many partials for this number of seconds. We adjust after receiving this many partials.
        self.number_of_partials_target: int = pool_config["number_of_partials_target"]
        self.time_target: int = pool_config["time_target"]

        # Tasks (infinite While loops) for different purposes
        self.confirm_partials_loop_task: Optional[asyncio.Task] = None
        self.collect_pool_rewards_loop_task: Optional[asyncio.Task] = None
        self.create_payment_loop_task: Optional[asyncio.Task] = None
        self.submit_payment_loop_task: Optional[asyncio.Task] = None
        self.get_peak_loop_task: Optional[asyncio.Task] = None

        self.node_rpc_client: Optional[FullNodeRpcClient] = None
        self.node_rpc_port = pool_config["node_rpc_port"]
        self.wallet_rpc_client: Optional[WalletRpcClient] = None
        self.wallet_rpc_port = pool_config["wallet_rpc_port"]

    async def start(self):
        await self.store.connect()
        self.pending_point_partials = asyncio.Queue()

        self_hostname = self.config["self_hostname"]
        self.node_rpc_client = await FullNodeRpcClient.create(
            self_hostname, uint16(self.node_rpc_port), DEFAULT_ROOT_PATH, self.config
        )
        self.wallet_rpc_client = await WalletRpcClient.create(
            self.config["self_hostname"], uint16(self.wallet_rpc_port), DEFAULT_ROOT_PATH, self.config
        )
        self.blockchain_state = await self.node_rpc_client.get_blockchain_state()
        res = await self.wallet_rpc_client.log_in_and_skip(fingerprint=self.wallet_fingerprint)
        if not res["success"]:
            raise ValueError(f"Error logging in: {res['error']}. Make sure your config fingerprint is correct.")
        self.log.info(f"Logging in: {res}")
        res = await self.wallet_rpc_client.get_wallet_balance(self.wallet_id)
        self.log.info(f"Obtaining balance: {res}")

        self.scan_p2_singleton_puzzle_hashes = await self.store.get_pay_to_singleton_phs()

        self.confirm_partials_loop_task = asyncio.create_task(self.confirm_partials_loop())
        # self.collect_pool_rewards_loop_task = asyncio.create_task(self.collect_pool_rewards_loop())
        # self.create_payment_loop_task = asyncio.create_task(self.create_payment_loop())
        # self.submit_payment_loop_task = asyncio.create_task(self.submit_payment_loop())
        self.get_peak_loop_task = asyncio.create_task(self.get_peak_loop())

        self.pending_payments = asyncio.Queue()

    async def stop(self):
        if self.confirm_partials_loop_task is not None:
            self.confirm_partials_loop_task.cancel()
        if self.collect_pool_rewards_loop_task is not None:
            self.collect_pool_rewards_loop_task.cancel()
        if self.create_payment_loop_task is not None:
            self.create_payment_loop_task.cancel()
        if self.submit_payment_loop_task is not None:
            self.submit_payment_loop_task.cancel()
        if self.get_peak_loop_task is not None:
            self.get_peak_loop_task.cancel()

        self.wallet_rpc_client.close()
        await self.wallet_rpc_client.await_closed()
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
                self.wallet_synced = await self.wallet_rpc_client.get_synced()
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.log.info("Cancelled get_peak_loop, closing")
                return
            except Exception as e:
                self.log.error(f"Unexpected error in get_peak_loop: {e}")
                await asyncio.sleep(30)

    async def collect_pool_rewards_loop(self):
        """
        Iterates through the blockchain, looking for pool rewards, and claims them, creating a transaction to the
        pool's puzzle_hash.
        """

        while True:
            try:
                if not self.blockchain_state["sync"]["synced"]:
                    await asyncio.sleep(60)
                    continue

                scan_phs: List[bytes32] = list(self.scan_p2_singleton_puzzle_hashes)
                peak_height = self.blockchain_state["peak"].height

                # Only get puzzle hashes with a certain number of confirmations or more, to avoid reorg issues
                coin_records: List[CoinRecord] = await self.node_rpc_client.get_coin_records_by_puzzle_hashes(
                    scan_phs,
                    include_spent_coins=False,
                    start_height=self.scan_start_height,
                )
                self.log.info(
                    f"Scanning for block rewards from {self.scan_start_height} to {peak_height}. "
                    f"Found: {len(coin_records)}"
                )
                ph_to_amounts: Dict[bytes32, int] = {}
                ph_to_coins: Dict[bytes32, List[CoinRecord]] = {}
                not_buried_amounts = 0
                for cr in coin_records:
                    if not cr.coinbase:
                        self.log.info(f"Non coinbase coin: {cr.coin}, ignoring")
                        continue

                    if cr.confirmed_block_index > peak_height - self.confirmation_security_threshold:
                        not_buried_amounts += cr.coin.amount
                        continue
                    if cr.coin.puzzle_hash not in ph_to_amounts:
                        ph_to_amounts[cr.coin.puzzle_hash] = 0
                        ph_to_coins[cr.coin.puzzle_hash] = []
                    ph_to_amounts[cr.coin.puzzle_hash] += cr.coin.amount
                    ph_to_coins[cr.coin.puzzle_hash].append(cr)

                # For each p2sph, get the FarmerRecords
                farmer_records = await self.store.get_farmer_records_for_p2_singleton_phs(
                    set(ph for ph in ph_to_amounts.keys())
                )

                # For each singleton, create, submit, and save a claim transaction
                claimable_amounts = 0
                not_claimable_amounts = 0
                for rec in farmer_records:
                    if rec.is_pool_member:
                        claimable_amounts += ph_to_amounts[rec.p2_singleton_puzzle_hash]
                    else:
                        not_claimable_amounts += ph_to_amounts[rec.p2_singleton_puzzle_hash]

                if len(coin_records) > 0:
                    self.log.info(f"Claimable amount: {claimable_amounts / (10**12)}")
                    self.log.info(f"Not claimable amount: {not_claimable_amounts / (10**12)}")
                    self.log.info(f"Not buried amounts: {not_buried_amounts / (10**12)}")

                for rec in farmer_records:
                    if rec.is_pool_member:
                        singleton_tip: Optional[Coin] = get_most_recent_singleton_coin_from_coin_spend(
                            rec.singleton_tip
                        )
                        if singleton_tip is None:
                            continue

                        singleton_coin_record: Optional[
                            CoinRecord
                        ] = await self.node_rpc_client.get_coin_record_by_name(singleton_tip.name())
                        if singleton_coin_record is None:
                            continue
                        if singleton_coin_record.spent:
                            self.log.warning(
                                f"Singleton coin {singleton_coin_record.coin.name()} is spent, will not "
                                f"claim rewards"
                            )
                            continue

                        spend_bundle = await create_absorb_transaction(
                            self.node_rpc_client,
                            rec,
                            self.blockchain_state["peak"].height,
                            ph_to_coins[rec.p2_singleton_puzzle_hash],
                            self.constants.GENESIS_CHALLENGE,
                        )

                        if spend_bundle is None:
                            continue

                        push_tx_response: Dict = await self.node_rpc_client.push_tx(spend_bundle)
                        if push_tx_response["status"] == "SUCCESS":
                            # TODO(pool): save transaction in records
                            self.log.info(f"Submitted transaction successfully: {spend_bundle.name().hex()}")
                        else:
                            self.log.error(f"Error submitting transaction: {push_tx_response}")
                await asyncio.sleep(self.collect_pool_rewards_interval)
            except asyncio.CancelledError:
                self.log.info("Cancelled collect_pool_rewards_loop, closing")
                return
            except Exception as e:
                error_stack = traceback.format_exc()
                self.log.error(f"Unexpected error in collect_pool_rewards_loop: {e} {error_stack}")
                await asyncio.sleep(self.collect_pool_rewards_interval)

    async def create_payment_loop(self):
        """
        Calculates the points of each farmer, and splits the total funds received into coins for each farmer.
        Saves the transactions that we should make, to `amount_to_distribute`.
        """
        while True:
            try:
                if not self.blockchain_state["sync"]["synced"]:
                    self.log.warning("Not synced, waiting")
                    await asyncio.sleep(60)
                    continue

                if self.pending_payments.qsize() != 0:
                    self.log.warning(f"Pending payments ({self.pending_payments.qsize()}), waiting")
                    await asyncio.sleep(60)
                    continue

                self.log.info("Starting to create payment")

                coin_records: List[CoinRecord] = await self.node_rpc_client.get_coin_records_by_puzzle_hash(
                    self.default_target_puzzle_hash,
                    include_spent_coins=False,
                    start_height=self.scan_start_height,
                )

                if len(coin_records) == 0:
                    self.log.info("No funds to distribute.")
                    await asyncio.sleep(120)
                    continue

                total_amount_claimed = sum([c.coin.amount for c in coin_records])
                pool_coin_amount = int(total_amount_claimed * self.pool_fee)
                amount_to_distribute = total_amount_claimed - pool_coin_amount

                if total_amount_claimed < calculate_pool_reward(uint32(1)):  # 1.75 XCH
                    self.log.info(f"Do not have enough funds to distribute: {total_amount_claimed}, skipping payout")
                    continue

                self.log.info(f"Total amount claimed: {total_amount_claimed / (10 ** 12)}")
                self.log.info(f"Pool coin amount (includes blockchain fee) {pool_coin_amount  / (10 ** 12)}")
                self.log.info(f"Total amount to distribute: {amount_to_distribute  / (10 ** 12)}")

                async with self.store.lock:
                    # Get the points of each farmer, as well as payout instructions. Here a chia address is used,
                    # but other blockchain addresses can also be used.
                    points_and_ph: List[
                        Tuple[uint64, bytes]
                    ] = await self.store.get_farmer_points_and_payout_instructions()
                    total_points = sum([pt for (pt, ph) in points_and_ph])
                    if total_points > 0:
                        mojo_per_point = floor(amount_to_distribute / total_points)
                        self.log.info(f"Paying out {mojo_per_point} mojo / point")

                        additions_sub_list: List[Dict] = [
                            {"puzzle_hash": self.pool_fee_puzzle_hash, "amount": pool_coin_amount}
                        ]
                        for points, ph in points_and_ph:
                            if points > 0:
                                additions_sub_list.append({"puzzle_hash": ph, "amount": points * mojo_per_point})

                            if len(additions_sub_list) == self.max_additions_per_transaction:
                                await self.pending_payments.put(additions_sub_list.copy())
                                self.log.info(f"Will make payments: {additions_sub_list}")
                                additions_sub_list = []

                        if len(additions_sub_list) > 0:
                            self.log.info(f"Will make payments: {additions_sub_list}")
                            await self.pending_payments.put(additions_sub_list.copy())

                        # Subtract the points from each farmer
                        await self.store.clear_farmer_points()
                    else:
                        self.log.info(f"No points for any farmer. Waiting {self.payment_interval}")

                await asyncio.sleep(self.payment_interval)
            except asyncio.CancelledError:
                self.log.info("Cancelled create_payments_loop, closing")
                return
            except Exception as e:
                error_stack = traceback.format_exc()
                self.log.error(f"Unexpected error in create_payments_loop: {e} {error_stack}")
                await asyncio.sleep(self.payment_interval)

    async def submit_payment_loop(self):
        while True:
            try:
                peak_height = self.blockchain_state["peak"].height
                await self.wallet_rpc_client.log_in_and_skip(fingerprint=self.wallet_fingerprint)
                if not self.blockchain_state["sync"]["synced"] or not self.wallet_synced:
                    self.log.warning("Waiting for wallet sync")
                    await asyncio.sleep(60)
                    continue

                payment_targets = await self.pending_payments.get()
                assert len(payment_targets) > 0

                self.log.info(f"Submitting a payment: {payment_targets}")

                # TODO(pool): make sure you have enough to pay the blockchain fee, this will be taken out of the pool
                # fee itself. Alternatively you can set it to 0 and wait longer
                # blockchain_fee = 0.00001 * (10 ** 12) * len(payment_targets)
                blockchain_fee: uint64 = uint64(0)
                try:
                    transaction: TransactionRecord = await self.wallet_rpc_client.send_transaction_multi(
                        self.wallet_id, payment_targets, fee=blockchain_fee
                    )
                except ValueError as e:
                    self.log.error(f"Error making payment: {e}")
                    await asyncio.sleep(10)
                    await self.pending_payments.put(payment_targets)
                    continue

                self.log.info(f"Transaction: {transaction}")

                while (
                    not transaction.confirmed
                    or not (peak_height - transaction.confirmed_at_height) > self.confirmation_security_threshold
                ):
                    transaction = await self.wallet_rpc_client.get_transaction(self.wallet_id, transaction.name)
                    peak_height = self.blockchain_state["peak"].height
                    self.log.info(
                        f"Waiting for transaction to obtain {self.confirmation_security_threshold} confirmations"
                    )
                    if not transaction.confirmed:
                        self.log.info(f"Not confirmed. In mempool? {transaction.is_in_mempool()}")
                    else:
                        self.log.info(f"Confirmations: {peak_height - transaction.confirmed_at_height}")
                    await asyncio.sleep(10)

                # TODO(pool): persist in DB
                self.log.info(f"Successfully confirmed payments {payment_targets}")

            except asyncio.CancelledError:
                self.log.info("Cancelled submit_payment_loop, closing")
                return
            except Exception as e:
                # TODO(pool): retry transaction if failed
                self.log.error(f"Unexpected error in submit_payment_loop: {e}")
                await asyncio.sleep(60)

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
                    await self.store.add_partial(partial.payload.launcher_id, partial.payload.harvester_id, uint64(int(time.time())), points_received)
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
