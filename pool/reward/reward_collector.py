import asyncio
import logging
import pathlib
import traceback
from typing import Dict, Optional, Set, List

import os
import yaml
import time

from chia.rpc.wallet_rpc_client import WalletRpcClient
from chia.types.blockchain_format.coin import Coin
from chia.types.coin_record import CoinRecord
from chia.consensus.constants import ConsensusConstants
from chia.util.ints import uint8, uint16, uint32, uint64
from chia.util.default_root import DEFAULT_ROOT_PATH
from chia.rpc.full_node_rpc_client import FullNodeRpcClient
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.chia_logging import initialize_logging
from chia.pools.pool_puzzles import (
    get_most_recent_singleton_coin_from_coin_spend,
)

from pool.singleton import create_absorb_transaction, get_singleton_state, get_coin_spend
from pool.store.abstract import AbstractPoolStore
from pool.store.sqlite_store import SqlitePoolStore

from ..reward_record import RewardRecord

class RewardCollector:
    def __init__(self, config: Dict, constants: ConsensusConstants, pool_store: Optional[AbstractPoolStore] = None):
        self.log = logging
        # If you want to log to a file: use filename='example.log', encoding='utf-8'
        self.log.basicConfig(level=logging.INFO)

        # We load our configurations from here
        with open(os.getcwd() + "/config.yaml") as f:
            pool_config: Dict = yaml.safe_load(f)

        pool_config["logging"]["log_filename"] = pool_config["logging"].get("reward_log_filename", "reward.log")
        # pool_config["logging"]["log_path"] = os.getcwd()

        initialize_logging("reward_collector", pool_config["logging"], pathlib.Path(pool_config["logging"]["log_path"]))

        self.config = config
        self.constants = constants

        self.store: AbstractPoolStore = pool_store or SqlitePoolStore()

        # This is the wallet fingerprint and ID for the wallet spending the funds from `self.default_target_puzzle_hash`
        self.wallet_fingerprint = pool_config["wallet_fingerprint"]
        self.wallet_id = pool_config["wallet_id"]

        # These are the phs that we want to look for on chain, that we can claim to our pool
        self.scan_p2_singleton_puzzle_hashes: Set[bytes32] = set()

        # Don't scan anything before this height, for efficiency (for example pool start date)
        self.scan_start_height: uint32 = uint32(pool_config["scan_start_height"])

        # Interval for scanning and collecting the pool rewards
        self.collect_pool_rewards_interval = pool_config["collect_pool_rewards_interval"]

        # After this many confirmations, a transaction is considered final and irreversible
        self.confirmation_security_threshold = pool_config["confirmation_security_threshold"]

        # Keeps track of the latest state of our node
        self.blockchain_state = {"peak": None}

        # Whether or not the wallet is synced (required to make payments)
        self.wallet_synced = False

        self.collect_pool_rewards_loop_task: Optional[asyncio.Task] = None
        self.get_peak_loop_task: Optional[asyncio.Task] = None

        self.node_rpc_client: Optional[FullNodeRpcClient] = None
        self.node_rpc_port = pool_config["node_rpc_port"]
        self.wallet_rpc_client: Optional[WalletRpcClient] = None
        self.wallet_rpc_port = pool_config["wallet_rpc_port"]

    async def start(self):
        await self.store.connect()

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

        self.collect_pool_rewards_loop_task = asyncio.create_task(self.collect_pool_rewards_loop())
        self.get_peak_loop_task = asyncio.create_task(self.get_peak_loop())

    async def stop(self):
        if self.collect_pool_rewards_loop_task is not None:
            self.collect_pool_rewards_loop_task.cancel()
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

                self.scan_p2_singleton_puzzle_hashes = await self.store.get_pay_to_singleton_phs()

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
                    self.log.info(f"coin_record: {cr}")
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
                            self.log.info(f"spend_bundle is None. {spend_bundle}")
                            continue

                        push_tx_response: Dict = await self.node_rpc_client.push_tx(spend_bundle)
                        if push_tx_response["status"] == "SUCCESS":
                            block_index: List[bytes32] = []
                            # TODO(pool): save transaction in records
                            for cr in ph_to_coins[rec.p2_singleton_puzzle_hash]:
                                if cr.confirmed_block_index not in block_index:
                                    block_index.append(cr.confirmed_block_index)
                                    reward = RewardRecord(
                                        rec.launcher_id,
                                        cr.coin.amount,
                                        cr.confirmed_block_index,
                                        cr.coin.puzzle_hash,
                                        cr.timestamp
                                    )
                                    self.log.info(f"add reward record: {reward}")
                                    await self.store.add_reward_record(reward)
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
