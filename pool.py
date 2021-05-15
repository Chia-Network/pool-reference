import asyncio
import dataclasses
import logging
import time
from typing import Dict, Optional, Set

from blspy import AugSchemeMPL, PrivateKey, G1Element
from chia.protocols.pool_protocol import SubmitPartial
from chia.types.blockchain_format.program import Program
from chia.util.bech32m import decode_puzzle_hash
from chia.consensus.constants import ConsensusConstants
from chia.util.ints import uint64, uint16, uint32
from chia.util.default_root import DEFAULT_ROOT_PATH
from chia.rpc.full_node_rpc_client import FullNodeRpcClient
from chia.full_node.signage_point import SignagePoint
from chia.types.end_of_slot_bundle import EndOfSubSlotBundle
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.consensus.pot_iterations import calculate_iterations_quality, calculate_sp_interval_iters
from chia.util.lru_cache import LRUCache
from chia.wallet.puzzles.load_clvm import load_clvm
from error_codes import PoolErr
from store import FarmerRecord, PoolStore


SINGLETON_MOD = load_clvm("singleton_top_layer.clvm")
P2_SINGLETON_MOD = load_clvm("p2_singleton.clvm")
singleton_mod_hash = SINGLETON_MOD.get_tree_hash()


@dataclasses.dataclass
class SingletonState:
    pool_url: str
    pool_puzzle_hash: bytes32
    relative_lock_height: uint32
    minimum_difficulty: uint64
    maximum_difficulty: uint64
    escaping: bool
    blockchain_height: uint32
    singleton_coin_id: bytes32


class Pool:
    def __init__(self, private_key: PrivateKey, config: Dict, constants: ConsensusConstants):
        self.log = logging.getLogger(__name__)
        self.private_key = private_key
        self.public_key: G1Element = private_key.get_g1()
        self.config = config
        self.constants = constants
        self.node_rpc_client = None

        self.store: Optional[PoolStore] = None

        self.pool_fee = 0.01

        # This number should be held constant and be consistent for every pool in the network
        self.iters_limit = 734000000

        # This number should not be changed, since users will put this into their singletons
        self.relative_lock_height = uint32(100)

        # TODO: potentially tweak these numbers for security and performance
        self.pool_url = "https://myreferencepool.com"
        self.min_difficulty = uint64(100)  # 100 difficulty is about 1 proof a day per plot
        self.default_difficulty: uint64 = uint64(100)
        self.max_difficulty = uint64(1000)

        # TODO: store this information in a persistent DB
        self.account_points: Dict[bytes, int] = {}  # Points are added by submitting partials
        self.account_rewards_targets: Dict[bytes, bytes] = {}

        self.pending_point_partials: Optional[asyncio.Queue] = None
        self.recent_points_added: LRUCache = LRUCache(20000)

        # This is where the block rewards will get paid out to. The pool needs to support this address forever,
        # since the farmers will encode it into their singleton on the blockchain.
        self.default_pool_puzzle_hash: bytes32 = decode_puzzle_hash(
            "xch12ma5m7sezasgh95wkyr8470ngryec27jxcvxcmsmc4ghy7c4njssnn623q"
        )

        # We need to check for slow farmers. If farmers cannot submit proofs in time, they won't be able to win
        # any rewards either. This number can be tweaked to be more or less strict. More strict ensures everyone
        # gets high rewards, but it might cause some of the slower farmers to not be able to participate in the pool.
        self.partial_time_limit: int = 25

        # There is always a risk of a reorg, in which case we cannot reward farmers that submitted partials in that
        # reorg. That is why we have a time delay before changing any account points.
        self.partial_confirmation_delay: int = 300

        self.full_node_client: Optional[FullNodeRpcClient] = None
        self.confirm_partials_loop_task: Optional[asyncio.Task] = None
        self.difficulty_change_time: Dict[bytes32, uint64] = {}

        self.scan_p2_singleton_puzzle_hashes: Set[bytes32] = set()
        self.blockchain_state = {"peak": None}

    async def start(self):
        self.store = await PoolStore.create()
        self.pending_point_partials = asyncio.Queue()
        self.full_node_client = await FullNodeRpcClient.create(
            self.config["self_hostname"], uint16(8555), DEFAULT_ROOT_PATH, self.config
        )
        self.confirm_partials_loop_task = asyncio.create_task(self.confirm_partials_loop())
        self_hostname = self.config["self_hostname"]
        self.node_rpc_client = await FullNodeRpcClient.create(
            self_hostname, uint16(8555), DEFAULT_ROOT_PATH, self.config
        )
        self.scan_p2_singleton_puzzle_hashes = await self.store.get_pay_to_singleton_phs()

    async def stop(self):
        if self.confirm_partials_loop_task is not None:
            self.confirm_partials_loop_task.cancel()

    async def get_peak_loop(self):
        """
        Periodically contacts the full node to get the latest state of the blockchain
        """
        while True:
            try:
                self.blockchain_state = await self.full_node_client.get_blockchain_state()
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.log.info("Cancelled get_peak_loop, closing")
                return
            except Exception as e:
                self.log.error(f"Unexpected error: {e}")

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
            if self.recent_points_added.get((bytes(partial.payload.owner_public_key, partial.payload.sp_hash))):
                self.log.info(f"Double signage point submitted by: {partial.payload.owner_public_key}")
                return
            self.recent_points_added.put((bytes(partial.payload.owner_public_key, partial.payload.sp_hash)), uint64(1))

            # Now we need to check to see that the singleton in the blockchain is still assigned to this pool
            singleton_state: Optional[SingletonState] = await self.get_and_validate_singleton_state(partial)

            if singleton_state is None:
                # This singleton doesn't exist, or isn't assigned to our pool
                return
            if singleton_state.escaping:
                # Don't give rewards while escaping from the pool (is this necessary?)
                return

            # The farmers sets their own difficulty. We already validated that this is in the correct range.
            farmer_record: Optional[FarmerRecord] = await self.store.get_farmer_record(
                partial.payload.singleton_genesis
            )
            if farmer_record is None:
                self.log.info(f"New farmer: {partial.payload.singleton_genesis.hex()}")
                farmer_record = FarmerRecord(
                    partial.payload.singleton_genesis,
                    partial.payload.owner_public_key,
                    self.default_pool_puzzle_hash,
                    singleton_state.relative_lock_height,
                    partial.payload.proof_of_space.pool_contract_puzzle_hash,
                    singleton_state.blockchain_height,
                    singleton_state.singleton_coin_id,
                    points_received,
                    partial.payload.difficulty,
                )
                self.scan_p2_singleton_puzzle_hashes.add(partial.payload.proof_of_space.pool_contract_puzzle_hash)
            else:
                assert partial.payload.owner_public_key == farmer_record.owner_public_key
                assert (
                    partial.payload.proof_of_space.pool_contract_puzzle_hash == farmer_record.p2_singleton_puzzle_hash
                )
                new_difficulty: uint64 = farmer_record.difficulty
                if partial.payload.difficulty != farmer_record.difficulty:
                    if time.time() - self.difficulty_change_time.get(partial.payload.singleton_genesis, 0) > 3600:
                        # Only change the difficulty about once per hour, to better support multiple devices farming
                        # on the same pool group
                        new_difficulty = partial.payload.difficulty
                        self.difficulty_change_time[partial.payload.singleton_genesis] = uint64(int(time.time()))

                farmer_record = FarmerRecord(
                    partial.payload.singleton_genesis,
                    partial.payload.owner_public_key,
                    self.default_pool_puzzle_hash,
                    singleton_state.relative_lock_height,
                    partial.payload.proof_of_space.pool_contract_puzzle_hash,
                    singleton_state.blockchain_height,
                    singleton_state.singleton_coin_id,
                    uint64(farmer_record.points + points_received),
                    new_difficulty,
                )

            await self.store.add_farmer_record(farmer_record)

            self.log.info(
                f"Farmer {partial.payload.owner_public_key} updated points to: "
                f"{self.account_points[bytes(partial.payload.owner_public_key)]}"
            )

            # The farmer also sets their own reward. This has a time lag as well
            self.account_rewards_targets[bytes(partial.payload.owner_public_key)] = partial.payload.rewards_target
        except Exception as e:
            self.log.error(f"Error: {e}")

    async def get_and_validate_singleton_state(self, partial: SubmitPartial) -> Optional[SingletonState]:
        """
        :return: the state of the singleton, if it currently exists in the blockchain, and if it is assigned to
        our pool, with the correct parameters.
        """

        # TODO: check if tasks running, if not start one
        # wait for task to end
        return SingletonState(
            self.pool_url,
            self.default_pool_puzzle_hash,
            self.relative_lock_height,
            self.min_difficulty,
            self.max_difficulty,
            False,
        )

    @staticmethod
    async def calculate_p2_singleton_ph(partial: SubmitPartial) -> bytes32:
        p2_singleton_full = P2_SINGLETON_MOD.curry(
            singleton_mod_hash, Program.to(singleton_mod_hash).get_tree_hash(), partial.payload.singleton_genesis
        )
        return p2_singleton_full.get_tree_hash()

    async def process_partial(
        self,
        partial: SubmitPartial,
        time_received_partial: uint64,
        balance: uint64,
        curr_difficulty: uint64,
    ) -> Dict:
        if partial.payload.difficulty < self.min_difficulty or partial.payload.difficulty > self.max_difficulty:
            return {
                "error_code": PoolErr.INVALID_DIFFICULTY,
                "error_message": f"Invalid difficulty {partial.payload.difficulty}. minimum: {self.min_difficulty} "
                f"maximum {self.max_difficulty}",
                "points_balance": balance,
                "curr_difficulty": curr_difficulty,
            }

        # Validate signatures
        pk1: G1Element = partial.payload.owner_public_key
        m1: bytes = partial.payload.rewards_target
        pk2: G1Element = partial.payload.proof_of_space.plot_public_key
        m2: bytes = bytes(partial.payload)
        valid_sig = AugSchemeMPL.aggregate_verify([pk1, pk2], [m1, m2], partial.rewards_and_partial_aggregate_signature)

        if not valid_sig:
            return {
                "error_code": PoolErr.INVALID_SIGNATURE,
                "error_message": f"The aggregate signature is invalid {partial.rewards_and_partial_aggregate_signature}",
                "points_balance": balance,
                "difficulty": curr_difficulty,
            }

        if partial.payload.proof_of_space.pool_contract_puzzle_hash != self.calculate_p2_singleton_ph(partial):
            return {
                "error_code": PoolErr.INVALID_P2_SINGLETON_PUZZLE_HASH,
                "error_message": f"The puzzl h {partial.rewards_and_partial_aggregate_signature}",
                "points_balance": balance,
                "difficulty": curr_difficulty,
            }

        if partial.payload.end_of_sub_slot:
            response = await self.node_rpc_client.get_recent_signage_point_or_eos(None, partial.payload.sp_hash)
        else:
            response = await self.node_rpc_client.get_recent_signage_point_or_eos(partial.payload.sp_hash, None)

        if response is None or response["reverted"]:
            return {
                "error_code": PoolErr.NOT_FOUND,
                "error_message": f"Did not find signage point or EOS {partial.payload.sp_hash}",
                "points_balance": balance,
                "difficulty": curr_difficulty,
            }
        node_time_received_sp = response["time_received"]

        signage_point: Optional[SignagePoint] = response.get("signage_point", None)
        end_of_sub_slot: Optional[EndOfSubSlotBundle] = response.get("eos", None)

        if time_received_partial - node_time_received_sp > self.partial_time_limit:
            return {
                "error_code": PoolErr.TOO_LATE,
                "error_message": f"Received partial in {time_received_partial - node_time_received_sp}. "
                f"Make sure your proof of space lookups are fast, and network connectivity is good. Response "
                f"must happen in less than {self.partial_time_limit} seconds. NAS or networking farming can be an "
                f"issue",
                "points_balance": balance,
                "curr_difficulty": curr_difficulty,
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
                "error_code": PoolErr.INVALID_PROOF,
                "error_message": f"Invalid proof of space {partial.payload.sp_hash}",
                "points_balance": balance,
                "curr_difficulty": curr_difficulty,
            }

        required_iters: uint64 = calculate_iterations_quality(
            self.constants.DIFFICULTY_CONSTANT_FACTOR,
            quality_string,
            partial.payload.proof_of_space.size,
            curr_difficulty,
            partial.payload.sp_hash,
        )

        if required_iters >= self.iters_limit:
            return {
                "error_code": PoolErr.PROOF_NOT_GOOD_ENOUGH,
                "error_message": f"Proof of space has required iters {required_iters}, too high for difficulty "
                f"{curr_difficulty}",
                "points_balance": balance,
                "curr_difficulty": curr_difficulty,
            }

        await self.pending_point_partials.put((partial, time_received_partial, curr_difficulty))

        return {"points_balance": balance, "curr_difficulty": curr_difficulty}
