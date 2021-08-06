from dataclasses import dataclass

from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.ints import uint64
from chia.util.streamable import streamable, Streamable

@dataclass(frozen=True)
@streamable
class RewardRecord(Streamable):
    launcher_id: bytes32   # This uniquely identifies the singleton on the blockchain (ID for this farmer)
    claimable: uint64      # The amount of token claimable
    height: uint64         # The height of the block to claim
    coins: bytes32         # The coin identifier
    timestamp: uint64      # The timestamp of the reward
