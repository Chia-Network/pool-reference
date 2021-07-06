import asyncio
import traceback
from math import floor
from typing import Dict, Optional, Set, List, Tuple

from chia.types.blockchain_format.coin import Coin
from chia.types.coin_record import CoinRecord
from chia.util.bech32m import decode_puzzle_hash
from chia.util.ints import uint32, uint64
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.wallet.transaction_record import TransactionRecord
from chia.pools.pool_puzzles import get_most_recent_singleton_coin_from_coin_solution

from ..singleton import create_absorb_transaction
from ..record import FarmerRecord

from .abstract import AbstractPaymentManager


class DefaultPaymentManager(AbstractPaymentManager):
    """
    Default payment manager
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Don't scan anything before this height, for efficiency (for example pool start date)
        self.scan_start_height: uint32 = uint32(self._pool_config["scan_start_height"])

        # Interval for scanning and collecting the pool rewards
        self.collect_pool_rewards_interval = self._pool_config["collect_pool_rewards_interval"]

        # After this many confirmations, a transaction is considered final and irreversible
        self.confirmation_security_threshold = self._pool_config["confirmation_security_threshold"]

        # Interval for making payout transactions to farmers
        self.payment_interval = self._pool_config["payment_interval"]

        # We will not make transactions with more targets than this, to ensure our transaction gets into the blockchain
        # faster.
        self.max_additions_per_transaction = self._pool_config["max_additions_per_transaction"]

        self.default_target_puzzle_hash: bytes32 = bytes32(decode_puzzle_hash(self._pool_config["default_target_address"]))

        self.pool_fee = self._pool_config["pool_fee"]

        # The pool fees will be sent to this address. This MUST be on a different key than the target_puzzle_hash,
        # otherwise, the fees will be sent to the users.
        self.pool_fee_puzzle_hash: bytes32 = bytes32(decode_puzzle_hash(self._pool_config["pool_fee_address"]))

        # This is the wallet fingerprint and ID for the wallet spending the funds from `self.default_target_puzzle_hash`
        self.wallet_fingerprint = self._pool_config["wallet_fingerprint"]
        self.wallet_id = self._pool_config["wallet_id"]

        # This is the list of payments that we have not sent yet, to farmers
        self.pending_payments: Optional[asyncio.Queue] = None

        self.scan_p2_singleton_puzzle_hashes: Set[bytes32] = set()

        self.collect_pool_rewards_loop_task: Optional[asyncio.Task] = None
        self.create_payment_loop_task: Optional[asyncio.Task] = None
        self.submit_payment_loop_task: Optional[asyncio.Task] = None
    
    async def start(self, *args, **kwargs):
        await super().start(*args, **kwargs)
        self.pending_payments = asyncio.Queue()
        self.scan_p2_singleton_puzzle_hashes = await self._store.get_pay_to_singleton_phs()

        self.collect_pool_rewards_loop_task = asyncio.create_task(self.collect_pool_rewards_loop())
        self.create_payment_loop_task = asyncio.create_task(self.create_payment_loop())
        self.submit_payment_loop_task = asyncio.create_task(self.submit_payment_loop())

    async def stop(self):
        if self.collect_pool_rewards_loop_task is not None:
            self.collect_pool_rewards_loop_task.cancel()
        if self.create_payment_loop_task is not None:
            self.create_payment_loop_task.cancel()
        if self.submit_payment_loop_task is not None:
            self.submit_payment_loop_task.cancel()
        
    def register_new_farmer(self, farmer_record: FarmerRecord):
        self.scan_p2_singleton_puzzle_hashes.add(farmer_record.p2_singleton_puzzle_hash)
        
    async def collect_pool_rewards_loop(self):
        """
        Iterates through the blockchain, looking for pool rewards, and claims them, creating a transaction to the
        pool's puzzle_hash.
        """

        while True:
            try:
                if not self._state_keeper.blockchain_state["sync"]["synced"]:
                    await asyncio.sleep(60)
                    continue

                scan_phs: List[bytes32] = list(self.scan_p2_singleton_puzzle_hashes)
                peak_height = self._state_keeper.blockchain_state["peak"].height

                # Only get puzzle hashes with a certain number of confirmations or more, to avoid reorg issues
                coin_records: List[CoinRecord] = await self._node_rpc_client.get_coin_records_by_puzzle_hashes(
                    scan_phs,
                    include_spent_coins=False,
                    start_height=self.scan_start_height,
                )
                self._logger.info(
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
                farmer_records = await self._store.get_farmer_records_for_p2_singleton_phs(
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
                    self._logger.info(f"Claimable amount: {claimable_amounts / (10**12)}")
                    self._logger.info(f"Not claimable amount: {not_claimable_amounts / (10**12)}")
                    self._logger.info(f"Not buried amounts: {not_buried_amounts / (10**12)}")

                for rec in farmer_records:
                    if rec.is_pool_member:
                        singleton_tip: Optional[Coin] = get_most_recent_singleton_coin_from_coin_solution(
                            rec.singleton_tip
                        )
                        if singleton_tip is None:
                            continue

                        singleton_coin_record: Optional[
                            CoinRecord
                        ] = await self._node_rpc_client.get_coin_record_by_name(singleton_tip.name())
                        if singleton_coin_record is None:
                            continue
                        if singleton_coin_record.spent:
                            self._logger.warning(
                                f"Singleton coin {singleton_coin_record.coin.name()} is spent, will not "
                                f"claim rewards"
                            )
                            continue

                        spend_bundle = await create_absorb_transaction(
                            self._node_rpc_client,
                            rec,
                            self._state_keeper.blockchain_state["peak"].height,
                            ph_to_coins[rec.p2_singleton_puzzle_hash],
                            self._constants.GENESIS_CHALLENGE,
                        )

                        if spend_bundle is None:
                            continue

                        push_tx_response: Dict = await self._node_rpc_client.push_tx(spend_bundle)
                        if push_tx_response["status"] == "SUCCESS":
                            # TODO(pool): save transaction in records
                            self._logger.info(f"Submitted transaction successfully: {spend_bundle.name().hex()}")
                        else:
                            self._logger.error(f"Error submitting transaction: {push_tx_response}")
                await asyncio.sleep(self.collect_pool_rewards_interval)
            except asyncio.CancelledError:
                self._logger.info("Cancelled collect_pool_rewards_loop, closing")
                return
            except Exception as e:
                error_stack = traceback.format_exc()
                self._logger.error(f"Unexpected error in collect_pool_rewards_loop: {e} {error_stack}")
                await asyncio.sleep(self.collect_pool_rewards_interval)

    async def create_payment_loop(self):
        """
        Calculates the points of each farmer, and splits the total funds received into coins for each farmer.
        Saves the transactions that we should make, to `amount_to_distribute`.
        """
        while True:
            try:
                if not self._state_keeper.blockchain_state["sync"]["synced"]:
                    self._logger.warning("Not synced, waiting")
                    await asyncio.sleep(60)
                    continue

                if self.pending_payments.qsize() != 0:
                    self._logger.warning(f"Pending payments ({self.pending_payments.qsize()}), waiting")
                    await asyncio.sleep(60)
                    continue

                self._logger.info("Starting to create payment")

                coin_records: List[CoinRecord] = await self._node_rpc_client.get_coin_records_by_puzzle_hash(
                    self.default_target_puzzle_hash, include_spent_coins=False
                )

                if len(coin_records) == 0:
                    self._logger.info("No funds to distribute.")
                    await asyncio.sleep(120)
                    continue

                total_amount_claimed = sum([c.coin.amount for c in coin_records])
                pool_coin_amount = int(total_amount_claimed * self.pool_fee)
                amount_to_distribute = total_amount_claimed - pool_coin_amount

                self._logger.info(f"Total amount claimed: {total_amount_claimed / (10 ** 12)}")
                self._logger.info(f"Pool coin amount (includes blockchain fee) {pool_coin_amount  / (10 ** 12)}")
                self._logger.info(f"Total amount to distribute: {amount_to_distribute  / (10 ** 12)}")

                async with self._store.lock:
                    # Get the points of each farmer, as well as payout instructions. Here a chia address is used,
                    # but other blockchain addresses can also be used.
                    points_and_ph: List[
                        Tuple[uint64, bytes]
                    ] = await self._store.get_farmer_points_and_payout_instructions()
                    total_points = sum([pt for (pt, ph) in points_and_ph])
                    if total_points > 0:
                        mojo_per_point = floor(amount_to_distribute / total_points)
                        self._logger.info(f"Paying out {mojo_per_point} mojo / point")

                        additions_sub_list: List[Dict] = [
                            {"puzzle_hash": self.pool_fee_puzzle_hash, "amount": pool_coin_amount}
                        ]
                        for points, ph in points_and_ph:
                            if points > 0:
                                additions_sub_list.append({"puzzle_hash": ph, "amount": points * mojo_per_point})

                            if len(additions_sub_list) == self.max_additions_per_transaction:
                                await self.pending_payments.put(additions_sub_list.copy())
                                self._logger.info(f"Will make payments: {additions_sub_list}")
                                additions_sub_list = []

                        if len(additions_sub_list) > 0:
                            self._logger.info(f"Will make payments: {additions_sub_list}")
                            await self.pending_payments.put(additions_sub_list.copy())

                        # Subtract the points from each farmer
                        await self._store.clear_farmer_points()
                    else:
                        self._logger.info(f"No points for any farmer. Waiting {self.payment_interval}")

                await asyncio.sleep(self.payment_interval)
            except asyncio.CancelledError:
                self._logger.info("Cancelled create_payments_loop, closing")
                return
            except Exception as e:
                error_stack = traceback.format_exc()
                self._logger.error(f"Unexpected error in create_payments_loop: {e} {error_stack}")
                await asyncio.sleep(self.payment_interval)

    async def submit_payment_loop(self):
        while True:
            try:
                peak_height = self._state_keeper.blockchain_state["peak"].height
                await self._wallet_rpc_client.log_in_and_skip(fingerprint=self.wallet_fingerprint)
                if not self._state_keeper.blockchain_state["sync"]["synced"] or not self._state_keeper.wallet_synced:
                    self._logger.warning("Waiting for wallet sync")
                    await asyncio.sleep(60)
                    continue

                payment_targets = await self.pending_payments.get()
                assert len(payment_targets) > 0

                self._logger.info(f"Submitting a payment: {payment_targets}")

                # TODO(pool): make sure you have enough to pay the blockchain fee, this will be taken out of the pool
                # fee itself. Alternatively you can set it to 0 and wait longer
                # blockchain_fee = 0.00001 * (10 ** 12) * len(payment_targets)
                blockchain_fee = 0
                try:
                    transaction: TransactionRecord = await self._wallet_rpc_client.send_transaction_multi(
                        self.wallet_id, payment_targets, fee=blockchain_fee
                    )
                except ValueError as e:
                    self._logger.error(f"Error making payment: {e}")
                    await asyncio.sleep(10)
                    await self.pending_payments.put(payment_targets)
                    continue

                self._logger.info(f"Transaction: {transaction}")

                while (
                    not transaction.confirmed
                    or not (peak_height - transaction.confirmed_at_height) > self.confirmation_security_threshold
                ):
                    transaction = await self._wallet_rpc_client.get_transaction(self.wallet_id, transaction.name)
                    peak_height = self._state_keeper.blockchain_state["peak"].height
                    self._logger.info(
                        f"Waiting for transaction to obtain {self.confirmation_security_threshold} confirmations"
                    )
                    if not transaction.confirmed:
                        self._logger.info(f"Not confirmed. In mempool? {transaction.is_in_mempool()}")
                    else:
                        self._logger.info(f"Confirmations: {peak_height - transaction.confirmed_at_height}")
                    await asyncio.sleep(10)

                # TODO(pool): persist in DB
                self._logger.info(f"Successfully confirmed payments {payment_targets}")

            except asyncio.CancelledError:
                self._logger.info("Cancelled submit_payment_loop, closing")
                return
            except Exception as e:
                # TODO(pool): retry transaction if failed
                self._logger.error(f"Unexpected error in submit_payment_loop: {e}")
                await asyncio.sleep(60)
