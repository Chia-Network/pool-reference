from dataclasses import dataclass

from blspy import G1Element
from chia.pools.pool_wallet_info import PoolState
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_spend import CoinSpend
from chia.util.ints import uint64
from chia.util.streamable import streamable, Streamable


@dataclass(frozen=True)
@streamable
class FarmerRecord(Streamable):
    launcher_id: bytes32  # This uniquely identifies the singleton on the blockchain (ID for this farmer)
    p2_singleton_puzzle_hash: bytes32  # Derived from the launcher id, delay_time and delay_puzzle_hash
    delay_time: uint64  # Backup time after which farmer can claim rewards directly, if pool unresponsive
    delay_puzzle_hash: bytes32  # Backup puzzlehash to claim rewards
    authentication_public_key: G1Element  # This is the latest public key of the farmer (signs all partials)
    singleton_tip: CoinSpend  # Last coin spend that is buried in the blockchain, for this singleton
    singleton_tip_state: PoolState  # Current state of the singleton
    points: uint64  # Total points accumulated since last rest (or payout)
    difficulty: uint64  # Current difficulty for this farmer
    payout_instructions: str  # This is where the pool will pay out rewards to the farmer
    is_pool_member: bool  # If the farmer leaves the pool, this gets set to False

@streamable
class PaymentRecord(Streamable):
    launcher_id: bytes32   # This uniquely identifies the singleton on the blockchain (ID for this farmer)
    payment_amount: uint64 # The amount of token paid to the farmer
    points: uint64         # The points of the farmer during the payment
    timestamp: uint64      # The timestamp of the payment
    payment_type: str      # The type of the payment coin:  XCH, MAXI, WXCH etc
    txid: str              # The payment transaction id/hash
    note: str              # Additional payment note
