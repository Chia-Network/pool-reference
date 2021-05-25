import asyncio
import dataclasses
import logging
import time
import traceback
from secrets import token_bytes
from typing import Dict, Optional, Set, List, Tuple

from blspy import AugSchemeMPL, PrivateKey, G1Element
from chia.protocols.pool_protocol import SubmitPartial
from chia.rpc.wallet_rpc_client import WalletRpcClient
from chia.types.blockchain_format.coin import Coin
from chia.types.coin_record import CoinRecord
from chia.util.bech32m import decode_puzzle_hash
from chia.consensus.constants import ConsensusConstants
from chia.util.ints import uint64, uint16, uint32
from chia.util.default_root import DEFAULT_ROOT_PATH
from chia.rpc.full_node_rpc_client import FullNodeRpcClient
from chia.full_node.signage_point import SignagePoint
from chia.types.end_of_slot_bundle import EndOfSubSlotBundle
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.consensus.pot_iterations import calculate_iterations_quality
from chia.util.lru_cache import LRUCache
from chia.wallet.transaction_record import TransactionRecord

from difficulty_adjustment import get_new_difficulty
from error_codes import PoolErr
from store import FarmerRecord, PoolStore
from singleton import create_absorb_transaction, calculate_p2_singleton_ph


@dataclasses.dataclass
class SingletonState:
    pool_url: str
    pool_puzzle_hash: bytes32
    relative_lock_height: uint32
    minimum_difficulty: uint64
    escaping: bool
    blockchain_height: uint32
    singleton_coin_id: bytes32


class Pool:
    def __init__(self, private_key: PrivateKey, config: Dict, constants: ConsensusConstants):
        self.log = logging
        # If you want to log to a file: use filename='example.log', encoding='utf-8'
        self.log.basicConfig(level=logging.INFO)

        self.private_key = private_key
        self.public_key: G1Element = private_key.get_g1()
        self.config = config
        self.constants = constants
        self.node_rpc_client = None
        self.wallet_rpc_client = None

        self.store: Optional[PoolStore] = None

        self.pool_fee = 0.01

        # This number should be held constant and be consistent for every pool in the network. DO NOT CHANGE
        self.iters_limit = self.constants.POOL_SUB_SLOT_ITERS // 64

        # This number should not be changed, since users will put this into their singletons
        self.relative_lock_height = uint32(100)

        # TODO(pool): potentially tweak these numbers for security and performance
        self.pool_url = "https://myreferencepool.com"
        self.min_difficulty = uint64(10)  # 10 difficulty is about 1 proof a day per plot
        self.default_difficulty: uint64 = uint64(10)

        self.pending_point_partials: Optional[asyncio.Queue] = None
        self.recent_points_added: LRUCache = LRUCache(20000)

        # This is where the block rewards will get paid out to. The pool needs to support this address forever,
        # since the farmers will encode it into their singleton on the blockchain. WARNING: the default pool code
        # completely spends this wallet and distributes it to users, do don't put any additional funds in here
        # that you do not want to distribute. Even if the funds are in a different address than this one, they WILL
        # be spent by this code! So only put funds that you want to distribute to pool members here.

        # Using 1254429856
        self.default_target_puzzle_hash: bytes32 = bytes32(
            decode_puzzle_hash("xch1x3zc0ncx5wwmguwjesp82p8gdz9q5e7va9sj2dgqe9tvwdsrl4vqnaquh3")
        )

        # The pool fees will be sent to this address. This MUST be on a different key than the target_puzzle_hash,
        # otherwise, the fees will be sent to the users. Using 2269911172
        self.pool_fee_puzzle_hash: bytes32 = bytes32(
            decode_puzzle_hash("xch1jhydnk47xl40jn3pgwjd708uk2wqzqexagy9xxjcwt2qeaumv3fq4yfvce")
        )

        # This is the wallet fingerprint and ID for the wallet spending the funds from `self.default_target_puzzle_hash`
        self.wallet_fingerprint = 2938470744
        self.wallet_id = "1"

        # We need to check for slow farmers. If farmers cannot submit proofs in time, they won't be able to win
        # any rewards either. This number can be tweaked to be more or less strict. More strict ensures everyone
        # gets high rewards, but it might cause some of the slower farmers to not be able to participate in the pool.
        self.partial_time_limit: int = 25

        # There is always a risk of a reorg, in which case we cannot reward farmers that submitted partials in that
        # reorg. That is why we have a time delay before changing any account points.
        self.partial_confirmation_delay: int = 30

        # These are the phs that we want to look for on chain, that we can claim to our pool
        self.scan_p2_singleton_puzzle_hashes: Set[bytes32] = set()

        # Don't scan anything before this height, for efficiency (for example pool start date)
        self.scan_start_height: uint32 = uint32(1000)

        # Interval for scanning and collecting the pool rewards
        self.collect_pool_rewards_interval = 600

        # After this many confirmations, a transaction is considered final and irreversible
        self.confirmation_security_threshold = 6

        # Interval for making payout transactions to farmers
        self.payment_interval = 600

        # We will not make transactions with more targets than this, to ensure our transaction gets into the blockchain
        # faster.
        self.max_additions_per_transaction = 400

        # This is the list of payments that we have not sent yet, to farmers
        self.pending_payments: Optional[asyncio.Queue] = None

        # Keeps track of the latest state of our node
        self.blockchain_state = {"peak": None}

        # Whether or not the wallet is synced (required to make payments)
        self.wallet_synced = False

        # We target these many partials for this number of seconds. We adjust after receiving this many partials.
        self.number_of_partials_target: int = 30
        self.time_target: int = 24 * 360

        # Tasks (infinite While loops) for different purposes
        self.confirm_partials_loop_task: Optional[asyncio.Task] = None
        self.collect_pool_rewards_loop_task: Optional[asyncio.Task] = None
        self.create_payment_loop_task: Optional[asyncio.Task] = None
        self.submit_payment_loop_task: Optional[asyncio.Task] = None
        self.get_peak_loop_task: Optional[asyncio.Task] = None

        self.node_rpc_client: Optional[FullNodeRpcClient] = None
        self.wallet_rpc_client: Optional[WalletRpcClient] = None

    async def start(self):
        self.store = await PoolStore.create()
        self.pending_point_partials = asyncio.Queue()

        self_hostname = self.config["self_hostname"]
        self.node_rpc_client = await FullNodeRpcClient.create(
            self_hostname, uint16(8555), DEFAULT_ROOT_PATH, self.config
        )
        self.wallet_rpc_client = await WalletRpcClient.create(
            self.config["self_hostname"], uint16(9256), DEFAULT_ROOT_PATH, self.config
        )
        self.blockchain_state = await self.node_rpc_client.get_blockchain_state()
        res = await self.wallet_rpc_client.log_in_and_skip(fingerprint=self.wallet_fingerprint)
        self.log.info(f"Logging in: {res}")
        res = await self.wallet_rpc_client.get_wallet_balance(self.wallet_id)
        self.log.info(f"Obtaining balance: {res}")

        self.scan_p2_singleton_puzzle_hashes = await self.store.get_pay_to_singleton_phs()

        self.confirm_partials_loop_task = asyncio.create_task(self.confirm_partials_loop())
        self.collect_pool_rewards_loop_task = asyncio.create_task(self.collect_pool_rewards_loop())
        self.create_payment_loop_task = asyncio.create_task(self.create_payment_loop())
        self.submit_payment_loop_task = asyncio.create_task(self.submit_payment_loop())
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
                    set([ph for ph in ph_to_amounts.keys()])
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

                        singleton_coin_record: Optional[
                            CoinRecord
                        ] = await self.node_rpc_client.get_coin_record_by_name(rec.singleton_coin_id)
                        if singleton_coin_record is None:
                            self.log.error(f"Could not find singleton coin {rec.singleton_coin_id}")
                            continue
                        if singleton_coin_record.spent:
                            self.log.warning(f"Singleton coin {rec.singleton_coin_id} is spent")
                            continue

                        spend_bundle = await create_absorb_transaction(
                            rec,
                            singleton_coin_record.coin,
                            ph_to_coins[rec.p2_singleton_puzzle_hash],
                            self.relative_lock_height,
                            self.constants.GENESIS_CHALLENGE,
                        )

                        push_tx_response: Dict = await self.node_rpc_client.push_tx(spend_bundle)
                        if push_tx_response["status"] == "SUCCESS":
                            # TODO(pool): save transaction in records
                            self.log.info(f"Submitted transaction successfully: {spend_bundle.name().hex()}")
                        else:
                            self.log.error(f"Error submitting transaction: {push_tx_response}")
                self.scan_start_height = uint32(
                    max(self.scan_start_height, peak_height - self.confirmation_security_threshold)
                )
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
                    self.default_target_puzzle_hash, include_spent_coins=False
                )

                coins_to_distribute: List[Coin] = []
                for coin_record in coin_records:
                    assert not coin_record.spent
                    if (
                        coin_record.spent_block_index
                        > self.blockchain_state["peak"].height - self.confirmation_security_threshold
                    ):
                        continue
                    coins_to_distribute.append(coin_record.coin)

                if len(coins_to_distribute) == 0:
                    self.log.info("No funds to distribute.")
                    await asyncio.sleep(120)
                    continue

                total_amount_claimed = sum([c.amount for c in coins_to_distribute])
                pool_coin_amount = int(total_amount_claimed * self.pool_fee)
                amount_to_distribute = total_amount_claimed - pool_coin_amount

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
                    mojo_per_point = amount_to_distribute / total_points
                    self.log.info(f"Paying out {mojo_per_point} mojo / point")

                    additions_sub_list: List[Dict] = [
                        {"puzzle_hash": self.pool_fee_puzzle_hash, "amount": pool_coin_amount}
                    ]
                    for points, ph in points_and_ph:
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
                if not self.blockchain_state["sync"]["synced"] or not self.wallet_synced:
                    self.log.warning("Waiting for wallet sync")
                    await asyncio.sleep(60)
                    continue

                payment_targets = await self.pending_payments.get()
                assert len(payment_targets) > 0

                self.log.info(f"Submitting a payment: {payment_targets}")

                # TODO(pool): make sure you have enough to pay the blockchain fee, this will be taken out of the pool
                # fee itself. Alternatively you can set it to 0 and wait longer
                blockchain_fee = 0.00001 * (10 ** 12) * len(payment_targets)
                try:
                    transaction: TransactionRecord = await self.wallet_rpc_client.send_transaction_multi(
                        self.wallet_id, payment_targets, fee=blockchain_fee
                    )
                except ValueError as e:
                    self.log.error(f"Error making payment: {e}")
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
                    await asyncio.sleep(30)

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

    async def check_and_confirm_partial(self, partial: SubmitPartial, points_received: uint64) -> None:
        try:
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
            singleton_state: Optional[SingletonState] = await self.get_and_validate_singleton_state(partial)

            if singleton_state is None:
                # This singleton doesn't exist, or isn't assigned to our pool
                return
            if singleton_state.escaping:
                # Don't give rewards while escaping from the pool (is this necessary?)
                return

            async with self.store.lock:
                farmer_record: Optional[FarmerRecord] = await self.store.get_farmer_record(
                    partial.payload.singleton_genesis
                )
                if farmer_record is None:
                    self.log.info(f"New farmer: {partial.payload.singleton_genesis.hex()}")
                    farmer_record = FarmerRecord(
                        partial.payload.singleton_genesis,
                        partial.payload.authentication_key_info.authentication_public_key,
                        partial.payload.authentication_key_info.authentication_public_key_timestamp,
                        partial.payload.owner_public_key,
                        self.default_target_puzzle_hash,
                        singleton_state.relative_lock_height,
                        partial.payload.proof_of_space.pool_contract_puzzle_hash,
                        singleton_state.blockchain_height,
                        singleton_state.singleton_coin_id,
                        points_received,
                        partial.payload.suggested_difficulty,
                        partial.payload.pool_payout_instructions,
                        True,
                    )
                    self.scan_p2_singleton_puzzle_hashes.add(partial.payload.proof_of_space.pool_contract_puzzle_hash)
                else:
                    assert partial.payload.owner_public_key == farmer_record.owner_public_key
                    assert (
                        partial.payload.proof_of_space.pool_contract_puzzle_hash
                        == farmer_record.p2_singleton_puzzle_hash
                    )

                    new_payout_instructions: bytes = farmer_record.pool_payout_instructions
                    new_authentication_pk: G1Element = farmer_record.authentication_public_key
                    new_authentication_pk_timestamp: uint64 = farmer_record.authentication_public_key_timestamp
                    if farmer_record.pool_payout_instructions != partial.payload.pool_payout_instructions:
                        # Only allow changing payout instructions if we have the latest authentication public key
                        if (
                            farmer_record.authentication_public_key_timestamp
                            <= partial.payload.authentication_key_info.authentication_public_key_timestamp
                        ):
                            # This means the authentication key being used is at least as new as the one in the DB
                            self.log.info(
                                f"Farmer changing rewards target to {partial.payload.pool_payout_instructions.hex()}"
                            )
                            new_payout_instructions = partial.payload.pool_payout_instructions
                            new_authentication_pk = partial.payload.authentication_key_info.authentication_public_key
                            new_authentication_pk_timestamp = (
                                partial.payload.authentication_key_info.authentication_public_key_timestamp
                            )
                        else:
                            # This means the timestamp in DB is new
                            self.log.info("Not changing pool payout instructions, don't have newest authentication key")
                    farmer_record = FarmerRecord(
                        partial.payload.singleton_genesis,
                        new_authentication_pk,
                        new_authentication_pk_timestamp,
                        partial.payload.owner_public_key,
                        self.default_target_puzzle_hash,
                        singleton_state.relative_lock_height,
                        partial.payload.proof_of_space.pool_contract_puzzle_hash,
                        singleton_state.blockchain_height,
                        singleton_state.singleton_coin_id,
                        uint64(farmer_record.points + points_received),
                        farmer_record.difficulty,
                        new_payout_instructions,
                        True,
                    )

                await self.store.add_farmer_record(farmer_record)
                await self.store.add_partial(
                    partial.payload.singleton_genesis, uint64(int(time.time())), points_received
                )

            self.log.info(f"Farmer {partial.payload.owner_public_key} updated points to: " f"{farmer_record.points}")
        except Exception as e:
            error_stack = traceback.format_exc()
            self.log.error(f"Exception in confirming partial: {e} {error_stack}")

    async def get_and_validate_singleton_state(self, partial: SubmitPartial) -> Optional[SingletonState]:
        """
        :return: the state of the singleton, if it currently exists in the blockchain, and if it is assigned to
        our pool, with the correct parameters.
        """

        # TODO(chia-dev): check if tasks running, if not start one
        # wait for task to end
        # update farmer records?
        return SingletonState(
            self.pool_url,
            self.default_target_puzzle_hash,
            self.relative_lock_height,
            self.min_difficulty,
            False,
            0,
            token_bytes(32),
        )

    async def process_partial(
        self,
        partial: SubmitPartial,
        time_received_partial: uint64,
        balance: uint64,
        current_difficulty: uint64,
        can_update_difficulty: bool,
    ) -> Dict:
        if partial.payload.suggested_difficulty < self.min_difficulty:
            return {
                "error_code": PoolErr.INVALID_DIFFICULTY.value,
                "error_message": f"Invalid difficulty {partial.payload.suggested_difficulty}. minimum: {self.min_difficulty} ",
            }

        # Validate signatures
        pk1: G1Element = partial.payload.owner_public_key
        m1: bytes = bytes(partial.payload.authentication_key_info)
        pk2: G1Element = partial.payload.proof_of_space.plot_public_key
        m2: bytes = partial.payload.get_hash()
        pk3: G1Element = partial.payload.authentication_key_info.authentication_public_key
        valid_sig = AugSchemeMPL.aggregate_verify(
            [pk1, pk2, pk3], [m1, m2, m2], partial.auth_key_and_partial_aggregate_signature
        )

        if not valid_sig:
            return {
                "error_code": PoolErr.INVALID_SIGNATURE.value,
                "error_message": f"The aggregate signature is invalid {partial.rewards_and_partial_aggregate_signature}",
            }

        if partial.payload.proof_of_space.pool_contract_puzzle_hash != await calculate_p2_singleton_ph(partial):
            return {
                "error_code": PoolErr.INVALID_P2_SINGLETON_PUZZLE_HASH.value,
                "error_message": f"The puzzl h {partial.rewards_and_partial_aggregate_signature}",
            }

        if partial.payload.end_of_sub_slot:
            response = await self.node_rpc_client.get_recent_signage_point_or_eos(None, partial.payload.sp_hash)
        else:
            response = await self.node_rpc_client.get_recent_signage_point_or_eos(partial.payload.sp_hash, None)

        if response is None or response["reverted"]:
            return {
                "error_code": PoolErr.NOT_FOUND.value,
                "error_message": f"Did not find signage point or EOS {partial.payload.sp_hash}, {response}",
            }
        node_time_received_sp = response["time_received"]

        signage_point: Optional[SignagePoint] = response.get("signage_point", None)
        end_of_sub_slot: Optional[EndOfSubSlotBundle] = response.get("eos", None)

        if time_received_partial - node_time_received_sp > self.partial_time_limit:
            return {
                "error_code": PoolErr.TOO_LATE.value,
                "error_message": f"Received partial in {time_received_partial - node_time_received_sp}. "
                f"Make sure your proof of space lookups are fast, and network connectivity is good. Response "
                f"must happen in less than {self.partial_time_limit} seconds. NAS or networking farming can be an "
                f"issue",
            }

        # Validate the proof
        if signage_point is not None:
            challenge_hash: bytes32 = signage_point.cc_vdf.challenge
        else:
            challenge_hash = end_of_sub_slot.challenge_chain.challenge_chain_end_of_slot_vdf.get_hash()

        quality_string: Optional[bytes32] = partial.payload.proof_of_space.verify_and_get_quality_string(
            self.constants, challenge_hash, partial.payload.sp_hash
        )
        if quality_string is None:
            return {
                "error_code": PoolErr.INVALID_PROOF.value,
                "error_message": f"Invalid proof of space {partial.payload.sp_hash}",
            }

        required_iters: uint64 = calculate_iterations_quality(
            self.constants.DIFFICULTY_CONSTANT_FACTOR,
            quality_string,
            partial.payload.proof_of_space.size,
            current_difficulty,
            partial.payload.sp_hash,
        )

        if required_iters >= self.iters_limit:
            return {
                "error_code": PoolErr.PROOF_NOT_GOOD_ENOUGH.value,
                "error_message": f"Proof of space has required iters {required_iters}, too high for difficulty "
                f"{current_difficulty}",
            }

        await self.pending_point_partials.put((partial, time_received_partial, current_difficulty))

        if can_update_difficulty:
            async with self.store.lock:
                # Obtains the new record in case we just updated difficulty
                farmer_record: Optional[FarmerRecord] = await self.store.get_farmer_record(
                    partial.payload.singleton_genesis
                )
                if farmer_record is not None:
                    current_difficulty = farmer_record.difficulty
                    # Decide whether to update the difficulty
                    recent_partials = await self.store.get_recent_partials(
                        partial.payload.singleton_genesis, self.number_of_partials_target
                    )
                    # Only update the difficulty if we meet certain conditions
                    new_difficulty: uint64 = get_new_difficulty(
                        recent_partials,
                        self.number_of_partials_target,
                        self.time_target,
                        current_difficulty,
                        time_received_partial,
                        self.min_difficulty,
                    )

                    # Only allow changing difficulty if we have the latest authentication public key
                    if (
                        current_difficulty != new_difficulty
                        and farmer_record.authentication_public_key_timestamp
                        <= partial.payload.authentication_key_info.authentication_public_key_timestamp
                    ):
                        await self.store.update_difficulty(partial.payload.singleton_genesis, new_difficulty)
                        return {"points_balance": balance, "current_difficulty": new_difficulty}

        return {"points_balance": balance, "current_difficulty": current_difficulty}
