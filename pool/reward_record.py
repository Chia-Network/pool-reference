from dataclasses import dataclass

from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.ints import uint64, uint32
from chia.util.streamable import streamable, Streamable


@dataclass(frozen=True)
@streamable
class RewardRecord(Streamable):
    launcher_id: bytes32  # This uniquely identifies the singleton on the blockchain (ID for this farmer)
    claimable: uint64  # The amount of token claimable
    block_height: uint32  # The height of the block to claim
    coins_hash: bytes32  # The coin's identifier
    timestamp: uint64  # The timestamp of the reward
