from typing import List, Optional, Tuple
import logging

from blspy import G2Element
from chia.consensus.coinbase import pool_parent_id
from chia.pools.pool_puzzles import (
    create_absorb_spend,
    solution_to_pool_state,
    get_most_recent_singleton_coin_from_coin_spend,
    pool_state_to_inner_puzzle,
    create_full_puzzle,
    get_delayed_puz_info_from_launcher_spend,
)
from chia.pools.pool_wallet import PoolSingletonState
from chia.pools.pool_wallet_info import PoolState
from chia.rpc.full_node_rpc_client import FullNodeRpcClient
from chia.rpc.wallet_rpc_client import WalletRpcClient
from chia.types.announcement import Announcement
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program, SerializedProgram
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_record import CoinRecord
from chia.types.coin_spend import CoinSpend
from chia.types.spend_bundle import SpendBundle
from chia.util.ints import uint32, uint64
from chia.wallet.transaction_record import TransactionRecord


from .record import FarmerRecord

log = logging
log.basicConfig(level=logging.INFO)


async def get_coin_spend(node_rpc_client: FullNodeRpcClient, coin_record: CoinRecord) -> Optional[CoinSpend]:
    if not coin_record.spent:
        return None
    return await node_rpc_client.get_puzzle_and_solution(coin_record.coin.name(), coin_record.spent_block_index)


def validate_puzzle_hash(
    launcher_id: bytes32,
    delay_ph: bytes32,
    delay_time: uint64,
    pool_state: PoolState,
    outer_puzzle_hash: bytes32,
    genesis_challenge: bytes32,
) -> bool:
    inner_puzzle: Program = pool_state_to_inner_puzzle(pool_state, launcher_id, genesis_challenge, delay_time, delay_ph)
    new_full_puzzle: Program = create_full_puzzle(inner_puzzle, launcher_id)
    return new_full_puzzle.get_tree_hash() == outer_puzzle_hash


async def get_singleton_state(
    node_rpc_client: FullNodeRpcClient,
    launcher_id: bytes32,
    farmer_record: Optional[FarmerRecord],
    peak_height: uint32,
    confirmation_security_threshold: int,
    genesis_challenge: bytes32,
) -> Optional[Tuple[CoinSpend, PoolState, PoolState]]:
    try:
        if farmer_record is None:
            launcher_coin: Optional[CoinRecord] = await node_rpc_client.get_coin_record_by_name(launcher_id)
            if launcher_coin is None:
                log.warning(f"Can not find genesis coin {launcher_id}")
                return None
            if not launcher_coin.spent:
                log.warning(f"Genesis coin {launcher_id} not spent")
                return None

            last_spend: Optional[CoinSpend] = await get_coin_spend(node_rpc_client, launcher_coin)
            delay_time, delay_puzzle_hash = get_delayed_puz_info_from_launcher_spend(last_spend)
            saved_state = solution_to_pool_state(last_spend)
            assert last_spend is not None and saved_state is not None
        else:
            last_spend = farmer_record.singleton_tip
            saved_state = farmer_record.singleton_tip_state
            delay_time = farmer_record.delay_time
            delay_puzzle_hash = farmer_record.delay_puzzle_hash

        saved_spend = last_spend
        last_not_none_state: PoolState = saved_state
        assert last_spend is not None

        last_coin_record: Optional[CoinRecord] = await node_rpc_client.get_coin_record_by_name(last_spend.coin.name())
        assert last_coin_record is not None

        while True:
            # Get next coin solution
            next_coin: Optional[Coin] = get_most_recent_singleton_coin_from_coin_spend(last_spend)
            if next_coin is None:
                # This means the singleton is invalid
                return None
            next_coin_record: Optional[CoinRecord] = await node_rpc_client.get_coin_record_by_name(next_coin.name())
            assert next_coin_record is not None

            if not next_coin_record.spent:
                if not validate_puzzle_hash(
                    launcher_id,
                    delay_puzzle_hash,
                    delay_time,
                    last_not_none_state,
                    next_coin_record.coin.puzzle_hash,
                    genesis_challenge,
                ):
                    log.warning(f"Invalid singleton puzzle_hash for {launcher_id}")
                    return None
                break

            last_spend: Optional[CoinSpend] = await get_coin_spend(node_rpc_client, next_coin_record)
            assert last_spend is not None

            pool_state: Optional[PoolState] = solution_to_pool_state(last_spend)

            if pool_state is not None:
                last_not_none_state = pool_state
            if peak_height - confirmation_security_threshold >= next_coin_record.spent_block_index:
                # There is a state transition, and it is sufficiently buried
                saved_spend = last_spend
                saved_state = last_not_none_state

        return saved_spend, saved_state, last_not_none_state
    except Exception as e:
        log.error(f"Error getting singleton: {e}")
        return None


def get_farmed_height(reward_coin_record: CoinRecord, genesis_challenge: bytes32) -> Optional[uint32]:
    # Returns the height farmed if it's a coinbase reward, otherwise None
    for block_index in range(
        reward_coin_record.confirmed_block_index, reward_coin_record.confirmed_block_index - 128, -1
    ):
        if block_index < 0:
            break
        pool_parent = pool_parent_id(uint32(block_index), genesis_challenge)
        if pool_parent == reward_coin_record.coin.parent_coin_info:
            return uint32(block_index)
    return None


async def create_absorb_transaction(
    node_rpc_client: FullNodeRpcClient,
    farmer_record: FarmerRecord,
    peak_height: uint32,
    reward_coin_records: List[CoinRecord],
    genesis_challenge: bytes32,
    fee_amount: Optional[uint64] = None,
    wallet_rpc_client: Optional[WalletRpcClient] = None,
    fee_target_puzzle_hash: Optional[bytes32] = None,
) -> Optional[SpendBundle]:
    singleton_state_tuple: Optional[Tuple[CoinSpend, PoolState, PoolState]] = await get_singleton_state(
        node_rpc_client, farmer_record.launcher_id, farmer_record, peak_height, 0, genesis_challenge
    )
    if singleton_state_tuple is None:
        log.info(f"Invalid singleton {farmer_record.launcher_id}.")
        return None
    last_spend, last_state, last_state_2 = singleton_state_tuple
    # Here the buried state is equivalent to the latest state, because we use 0 as the security_threshold
    assert last_state == last_state_2

    if last_state.state == PoolSingletonState.SELF_POOLING:
        log.info(f"Don't try to absorb from former farmer {farmer_record.launcher_id}.")
        return None

    launcher_coin_record: Optional[CoinRecord] = await node_rpc_client.get_coin_record_by_name(
        farmer_record.launcher_id
    )
    assert launcher_coin_record is not None
    coin_announcements: List[Announcement] = []

    all_spends: List[CoinSpend] = []
    for reward_coin_record in reward_coin_records:
        found_block_index: Optional[uint32] = get_farmed_height(reward_coin_record, genesis_challenge)
        if not found_block_index:
            # The puzzle does not allow spending coins that are not a coinbase reward
            log.info(f"Received reward {reward_coin_record.coin} that is not a pool reward.")
            continue
        absorb_spend: List[CoinSpend] = create_absorb_spend(
            last_spend,
            last_state,
            launcher_coin_record.coin,
            found_block_index,
            genesis_challenge,
            farmer_record.delay_time,
            farmer_record.delay_puzzle_hash,
        )
        if fee_amount > 0:
            coin_announcements.append(Announcement(reward_coin_record.coin.name(), b"$"))
        last_spend = absorb_spend[0]
        all_spends += absorb_spend
        # TODO(pool): handle the case where the cost exceeds the size of the block

    if len(coin_announcements) > 0:
        # address can be anything
        signed_transaction: TransactionRecord = await wallet_rpc_client.create_signed_transaction(
            additions=[{"amount": uint64(1), "puzzle_hash": fee_target_puzzle_hash}],
            fee=uint64(fee_amount * len(coin_announcements)),
            coin_announcements=coin_announcements,
        )
        fee_spend_bundle: Optional[SpendBundle] = signed_transaction.spend_bundle
    else:
        fee_spend_bundle = None

    if len(all_spends) == 0:
        return None
    spend_bundle: SpendBundle = SpendBundle(all_spends, G2Element())
    if fee_spend_bundle is not None:
        spend_bundle = SpendBundle.aggregate([spend_bundle, fee_spend_bundle])

    return spend_bundle
