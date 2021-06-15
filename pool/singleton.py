from typing import List, Optional, Tuple
import logging

from blspy import G2Element
from chia.consensus.coinbase import pool_parent_id
from chia.pools.pool_puzzles import (
    create_absorb_spend,
    solution_to_extra_data,
    get_most_recent_singleton_coin_from_coin_solution,
)
from chia.pools.pool_wallet_info import PoolState
from chia.rpc.full_node_rpc_client import FullNodeRpcClient
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_record import CoinRecord
from chia.types.coin_solution import CoinSolution
from chia.types.spend_bundle import SpendBundle
from chia.util.ints import uint32

from store import FarmerRecord

log = logging
log.basicConfig(level=logging.INFO)


async def get_coin_spend(node_rpc_client: FullNodeRpcClient, coin_record: CoinRecord) -> Optional[CoinSolution]:
    if not coin_record.spent:
        return None
    return await node_rpc_client.get_puzzle_and_solution(coin_record.coin.name(), coin_record.spent_block_index)


async def get_and_validate_singleton_state_inner(
    node_rpc_client: FullNodeRpcClient,
    launcher_id: bytes32,
    farmer_record: Optional[FarmerRecord],
    peak_height: uint32,
    confirmation_security_threshold: int,
    desired_state: Optional[PoolState],
) -> Optional[Tuple[CoinSolution, PoolState, bool, bool]]:
    try:
        if farmer_record is None:
            launcher_coin: Optional[CoinRecord] = await node_rpc_client.get_coin_record_by_name(launcher_id)
            if launcher_coin is None:
                log.warning(f"Can not find genesis coin {launcher_id}")
                return None
            if not launcher_coin.spent:
                log.warning(f"Genesis coin {launcher_id} not spent")
                return None

            last_solution: Optional[CoinSolution] = await get_coin_spend(node_rpc_client, launcher_coin)
            saved_state = solution_to_extra_data(last_solution)
            assert last_solution is not None and saved_state is not None
            updated = True
        else:
            last_solution = farmer_record.singleton_tip
            saved_state = farmer_record.singleton_tip_state
            updated = False
        saved_solution = last_solution
        last_not_none_state: PoolState = saved_state
        assert last_solution is not None

        last_coin_record: Optional[CoinRecord] = await node_rpc_client.get_coin_record_by_name(
            last_solution.coin.name()
        )
        assert last_coin_record is not None

        while True:
            # Get next coin solution
            next_coin: Optional[Coin] = get_most_recent_singleton_coin_from_coin_solution(last_solution)
            if next_coin is None:
                # This means the singleton is invalid
                return None
            next_coin_record: Optional[CoinRecord] = await node_rpc_client.get_coin_record_by_name(next_coin.name())
            assert next_coin_record is not None

            if not next_coin_record.spent:
                break

            last_solution: Optional[CoinSolution] = await get_coin_spend(node_rpc_client, next_coin_record)
            assert last_solution is not None

            pool_state: Optional[PoolState] = solution_to_extra_data(last_solution)

            if pool_state is not None:
                last_not_none_state = pool_state
            if peak_height - confirmation_security_threshold > next_coin_record.spent_block_index:
                # There is a state transition, and it is sufficiently buried
                saved_solution = last_solution
                saved_state = last_not_none_state
                updated = True

        # Validate state of the singleton
        is_pool_member = True
        if desired_state is not None:
            if last_not_none_state.target_puzzle_hash != desired_state.target_puzzle_hash:
                log.info(f"Wrong target puzzle hash: {last_not_none_state.target_puzzle_hash}")
                is_pool_member = False
            elif last_not_none_state.relative_lock_height != desired_state.relative_lock_height:
                log.info(f"Wrong relative lock height: {last_not_none_state.relative_lock_height}")
                is_pool_member = False
            elif last_not_none_state.version != desired_state.version:
                log.info(f"Wrong version {last_not_none_state.version}")
                is_pool_member = False
            elif last_not_none_state.state != desired_state.state:
                log.info(f"Invalid singleton state {last_not_none_state.state}")
                is_pool_member = False

        log.info(f"Is pool member? {is_pool_member}")
        return saved_solution, saved_state, updated, is_pool_member
    except Exception as e:
        log.error(f"Error getting singleton: {e}")
        return None


async def create_absorb_transaction(
    node_rpc_client: FullNodeRpcClient,
    farmer_record: FarmerRecord,
    peak_height: uint32,
    reward_coin_records: List[CoinRecord],
    genesis_challenge: bytes32,
) -> SpendBundle:
    last_solution, last_state, _, _ = await get_and_validate_singleton_state_inner(
        node_rpc_client, farmer_record.launcher_id, farmer_record, peak_height, 0, None
    )
    launcher_coin_record: Optional[CoinRecord] = await node_rpc_client.get_coin_record_by_name(
        farmer_record.launcher_id
    )
    assert launcher_coin_record is not None

    all_spends: List[CoinSolution] = []
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
        absorb_spend: List[CoinSolution] = create_absorb_spend(
            last_solution,
            last_state,
            launcher_coin_record.coin,
            found_block_index,
            genesis_challenge,
            farmer_record.delay_time,
            farmer_record.delay_puzzle_hash,
        )
        last_solution = absorb_spend[0]
        all_spends += absorb_spend
        # TODO(pool): handle the case where the cost exceeds the size of the block

    return SpendBundle(all_spends, G2Element())
