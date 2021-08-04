from dataclasses import dataclass

from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.ints import uint64
from chia.util.streamable import streamable, Streamable

@dataclass(frozen=True)
@streamable
class PaymentRecord(Streamable):
    launcher_id: bytes32   # This uniquely identifies the singleton on the blockchain (ID for this farmer)
    payment_amount: uint64 # The amount of token paid to the farmer
    points: uint64         # The points of the farmer during the payment
    timestamp: uint64      # The timestamp of the payment
    payment_type: str      # The type of the payment coin:  XCH, MAXI, WXCH etc
    txid: str              # The payment transaction id/hash
    note: str              # Additional payment note
