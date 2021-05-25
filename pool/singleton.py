from typing import List, Optional
import logging

from blspy import G2Element
from chia.consensus.coinbase import pool_parent_id
from chia.protocols.pool_protocol import SubmitPartial
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program, SerializedProgram, INFINITE_COST
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_record import CoinRecord
from chia.types.coin_solution import CoinSolution
from chia.types.spend_bundle import SpendBundle
from chia.util.ints import uint32
from chia.wallet.puzzles.load_clvm import load_clvm

from store import FarmerRecord

SINGLETON_MOD = load_clvm("singleton_top_layer.clvm")
P2_SINGLETON_MOD = load_clvm("p2_singleton.clvm")
POOL_COMMITED_MOD = load_clvm("pool_member_innerpuz.clvm")
POOL_ESCAPING_MOD = load_clvm("pool_escaping_innerpuz.clvm")
singleton_mod_hash = SINGLETON_MOD.get_tree_hash()
log = logging
log.basicConfig(level=logging.INFO)


async def calculate_p2_singleton_ph(partial: SubmitPartial) -> bytes32:
    p2_singleton_full = P2_SINGLETON_MOD.curry(
        singleton_mod_hash, Program.to(singleton_mod_hash).get_tree_hash(), partial.payload.singleton_genesis
    )
    return p2_singleton_full.get_tree_hash()


async def create_absorb_transaction(
    farmer_record: FarmerRecord,
    singleton_coin: Coin,
    reward_coin_records: List[CoinRecord],
    relative_lock_height: uint32,
    genesis_challenge: bytes32,
) -> SpendBundle:
    # We assume that the farmer record singleton state is updated to the latest
    escape_inner_puzzle: Program = POOL_ESCAPING_MOD.curry(
        farmer_record.pool_puzzle_hash,
        relative_lock_height,
        bytes(farmer_record.owner_public_key),
        farmer_record.p2_singleton_puzzle_hash,
    )
    committed_inner_puzzle: Program = POOL_COMMITED_MOD.curry(
        farmer_record.pool_puzzle_hash,
        escape_inner_puzzle.get_tree_hash(),
        farmer_record.p2_singleton_puzzle_hash,
        bytes(farmer_record.owner_public_key),
    )

    aggregate_spend_bundle: SpendBundle = SpendBundle([], G2Element())
    for reward_coin_record in reward_coin_records:
        found_block_index: Optional[uint32] = None
        for block_index in range(
            reward_coin_record.confirmed_block_index, reward_coin_record.confirmed_block_index - 100, -1
        ):
            if block_index < 0:
                break
            pool_parent = pool_parent_id(uint32(block_index), genesis_challenge)
            if pool_parent == reward_coin_record.coin.parent_coin_info:
                found_block_index = uint32(block_index)
        if not found_block_index:
            # The puzzle does not allow spending coins that are not a coinbase reward
            log.info(f"Received reward {reward_coin_record.coin} that is not a pool reward.")
            continue

        singleton_full = SINGLETON_MOD.curry(
            singleton_mod_hash, farmer_record.singleton_genesis, committed_inner_puzzle
        )

        inner_sol = Program.to(
            [0, singleton_full.get_tree_hash(), singleton_coin.amount, reward_coin_record.amount, found_block_index]
        )
        full_sol = Program.to([farmer_record.singleton_genesis, singleton_coin.amount, inner_sol])

        new_spend = SpendBundle(
            [CoinSolution(singleton_coin, SerializedProgram.from_bytes(bytes(singleton_full)), full_sol)],
            G2Element(),
        )
        # TODO(pool): handle the case where the cost exceeds the size of the block
        aggregate_spend_bundle = SpendBundle.aggregate([aggregate_spend_bundle, new_spend])

        # The new singleton will have the same puzzle hash and amount, but just have a different parent (old singleton)
        singleton_coin = Coin(singleton_coin.name(), singleton_coin.puzzle_hash(), singleton_coin.amount)

        cost, result = singleton_full.run_with_cost(INFINITE_COST, full_sol)
        log.info(f"Cost: {cost}, result {result}")

    return aggregate_spend_bundle
