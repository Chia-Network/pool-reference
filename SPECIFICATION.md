# Chia Pool Protocol 1.0

This is the initial version of the Chia Pool Protocol. It has been designed to be simple, and extensible in the future.
It relies on farmers having smart coins on the blockchain which allow them to dynamically set their pool to a different
pool by making a blockchain transaction. Furthermore, it decreases the reliance on pools for block production, since
the protocol only handles distribution of rewards, and it protects against pools or farmers acting maliciously.

## Parties

The parties involved in the pool protocol involve the pool operator and farmers. Each farmer is running
a farmer process, and any number of harvester processes connected to that farmer process. The full node can either be
run by the farmer (the default in the Chia GUI application), or run by the pool operator. If the farmer does not want
to run a full node, they can configure their node to connect to a remote full node.

A pool operator can support any number of farmers. 

## HTTP Endpoints Summary

The pool protocol consists of two HTTP endpoints which return JSON responses. The HTTP server can run on any port,
but must be running with TLS enabled. All bytes values are encoded as hex with optional 0x in front.

```
GET /pool_info
POST /partials
```

## GET /pool_info
This takes no arguments, and allows clients to fetch information about a pool. It is called right before joining a pool,
when the farmer enters the pool URL into the client. This allows the farmer to see information about the pool, and
decide whether or not to join. It also allows the farmer to set the correct parameters in their singleton on the
blockchain. Warning to client implementers: if displaying any of this information, make sure to account for malicious 
scripts and JS injections. It returns a JSON response with the following data:

```json
{
    "description": "(example) The Reference Pool allows you to pool with low fees, paying out daily using Chia.",
    "fee": 0.01,
    "logo_url": "https://www.chia.net/img/chia_logo.svg",
    "minimum_difficulty": 10,
    "name": "The Reference Pool",
    "protocol_version": "1.0",
    "relative_lock_height": 100,
    "target_puzzle_hash": "0x344587cf06a39db471d2cc027504e8688a0a67cce961253500c956c73603fd58"
}

```

#### description
The description is a short paragraph that can be displayed in GUIs when the farmer enters a pool URL.

#### fee
The fee that the pool charges by default, a number between 0 and 1. This assume

#### logo_url
A URL for a pool logo that the client can display in the UI.

#### minimum_difficulty
The minimum difficulty that the pool supports. This will also be the default that farmers start sending proofs for.

#### name
Name of the pool, this is only for display purposes and does not go on the blockchain.

#### protocol_version
The version of this pool protocol. Must be set to 1.0.

#### relative_lock_height
The number of blocks (confirmations) that a user must wait between the point when they start escaping a pool, and the
point at which they can finalize their pool switch.

#### target_puzzle_hash
This is the target of where rewards will be sent to from the singleton. Controlled by the pool.

## POST /partials
This is a partial submittion from the farmer to the pool operator.

Request:
```json
{
    "payload": {
        "proof_of_space": {
            "challenge": "0xe0e55d45eef8d53a6b68220abeec8f14f57baaa80dbd7b37430e42f9ac6e2c0e",
            "pool_contract_puzzle_hash": "0x9e3e9b37b54cf6c7467e277b6e4ca9ab6bdea53cdc1d79c000dc95b6a3908a3b",
            "plot_public_key": "0xa7ad70989cc8f18e555e9b698d197cdfc32465e0b99fd6cf5fdbac8aa2da04b0704ba04d2d50d852402f9dd6eec47a4d",
            "size": 32,
            "proof": "0xb2cd6374c8db249ad3b638199dbb6eb9eaefe55042cef66c43cf1e31161f4a1280455d8b53c2823c747fd4f8823c44de3a52cc85332431630857c359935660c3403ae3a92728d003dd66ef5966317cd49894d265a3e4c43f0530a1192874ed327e6f35862a25dfb67c5d0d573d078b4b8ba9bfb1cce52fd17939ae9d7033d3aa09d6c449e392ba2472a1fecf992abcc51c3bf5d56a72fef9900e79b8dba88a5afc39e04993325a0cd6b67757355b836f"
        },
        "sp_hash": "0x4c52796ca4ff775fbcdac90140c12270d26a37724ad77988535d58b376332533",
        "end_of_sub_slot": false,
        "suggested_difficulty": 10,
        "singleton_genesis": "0xae4ef3b9bfe68949691281a015a9c16630fc8f66d48c19ca548fb80768791afa",
        "owner_public_key": "0x84c3fcf9d5581c1ddc702cb0f3b4a06043303b334dd993ab42b2c320ebfa98e5ce558448615b3f69638ba92cf7f43da5",
        "pool_payout_instructions": "0xc2b08e41d766da4116e388357ed957d04ad754623a915f3fd65188a8746cf3e8",
        "authentication_key_info": {
            "authentication_public_key": "0x970e181ae45435ae696508a78012dc80548c334cf29676ea6ade7049eb9d2b9579cc30cb44c3fd68d35a250cfbc69e29",
            "authentication_public_key_timestamp": 1621854388
        }
    },
    "auth_key_and_partial_aggregate_signature": "0xa078dc1462bbcdec7cd651c5c3d7584ac6c6a142e049c7790f3b0ee8768ed6326e3a639f949b2293469be561adfa1c57130f64334994f53c1bd12e59579e27127fbabadc5e8793a2ef194a5a22ac832e92dcb6ad9a0d33bd264726f6e8df6aad"
}
```

Successful response:
```json
{"points_balance": 1130, "current_difficulty": 10}
```

The successful response must always contain the points balance since the last payout, as well as the current difficulty
that this farmer is being given credit for.

Failed response:
```json
{"error_code": 4, "error_message": "Invalid proof of space"}
```
Failed responses must include an error code as well as an english error message.

#### payload
This is the main payload of the partial, which is signed by two keys: `authentication_key` and `plot_key`.

#### payload.proof_of_space
The proof of space in chia-blockchain format.

#### payload.proof_of_space.challenge
The challenge of the proof of space, computed from the signage point or end of subslot.

#### payload.proof_of_space.pool_contract_puzzle_hash
The puzzle hash that is encoded in the plots, equivalent to the `p2_singleton_puzzle_hash`. This is the first place
that the 7/8 rewards get paid out to in the blockchain, if this proof wins. This value can be derived from the 
`singleton_genesis`, and must be valid for all partials.

#### payload.proof_of_space.plot_public_key
Public key associated with the plot. (Can be a 2/2 BLS between plot local key and farmer, but not necessarily).

#### payload.proof_of_space.size
K size, must be at least 32.

#### payload.proof_of_space.proof
64 x values encoding the actual proof of space, must be valid corresponding to the `sp_hash`.

#### payload.sp_hash
This is either the hash of the output for the signage point, or the challenge_hash for the sub slot, if it's an end 
of sub slot challenge. This must be a valid signage point on the blockchain that has not been reverted. The pool must
check a few minutes after processing the partial, that it has not been reverted on the blockchain.

#### payload.end_of_sub_slot
If true, the sp_hash encodes the challenge_hash of the sub slot.

#### payload.suggested_difficulty
A request from the farmer to update the difficulty. Can be ignored or respected by the pool. However, this should only 
be respected if the authentication public key is the most recent one seen for this farmer.

#### payload.singleton_genesis
The singleton_genesis, or the identifier of the farmer's singleton on the blockchain. This uniquely identifies this
farmer, and can be used as a primary key. The pool must periodically check the singleton on the blockchain to 
validate that it's farming to the pool, and not escaping or farming to another pool.

#### payload.owner_public_key
The current owner public key of the farmer's singleton. This can change on the blockchain, but the initial
implementation does not change it. It is used to transfer ownership of plots or singletons. The pool must check that
the current incarnation of the singleton matches this value.

#### payload.pool_payout_instructions
This is the instructions for how the farmer wants to get paid. By default this will be an XCH address, but it can
be set to any string with a size of less than 1024 characters, so it can represent another blockchain or payment
system identifier. If the farmer sends new instructions here, and the partial is fully valid, the pool should
update the instructions for this farmer. However, this should only be done if the authentication public key is the 
most recent one seen for this farmer.

#### payload.authentication_key_info
An authentication key, is a temporary key which the farmer uses to sign all of their requests to the pool. It is an
authorization given by the `owner_key`, so that the owner key can be potentially kept more secure. The timestamp is
the time at which the `owner_key` approved this key, and therefore the `owner_key` can revoke older keys by issuing
an authentication key with a new timestamp. The pool can accept partials with outdated `authentication_keys`, but the
pool must not update the difficulty or the payout instructions.

#### payload.authentication_key_info.authentication_public_key
Public key in BLS G1 format.

#### payload.authentication_key_info.authentication_public_key_timestamp
Timestamp of approval in unix time.

#### auth_key_and_partial_aggregate_signature
This is a 3/3 BLS aggregate signature using the Augmented Scheme in the BLS IETF spec.
1. Message: `payload` in streamable format, public key: `plot_public_key`
2. Message: `payload` in streamable format, public key: `authentication_public_key`
3. Message: `authentication_key_info` in streamable format, public key: `owner_public_key`

A partial must be completely rejected if the BLS signature does not validate.


## 1/8 vs 7/8
Note that the coinbase rewards in Chia are divided into two coins: the farmer coin and the pool coin. The farmer coin
(1/8) only goes to the puzzle hash signed by the farmer private key, while the pool coin (7/8) goes to the pool.
The user transaction fees on the blockchain are included in the farmer coin as well. This split of 7/8 1/8 exists
to prevent attacks where one pool tries to destroy another by farming partials, but never submitting winning blocks.


## Difficulty
The difficulty allows the pool operator to control how many partials per day they are receiving from each farmer.
A difficulty of 1 results in approximately 10 partials per day per k32 plot. This is the minimum difficulty that 
the V1 of the protocol supports is 1. However, a pool may set a higher minimum difficulty for efficiency. When 
calculating whether a proof is high quality enough for being awarded points, the pool should use 
`sub_slot_iters=37600000000`.

## Error codes
```
REVERTED_SIGNAGE_POINT = 1
TOO_LATE = 2
NOT_FOUND = 3
INVALID_PROOF = 4
PROOF_NOT_GOOD_ENOUGH = 5
INVALID_DIFFICULTY = 6
INVALID_SIGNATURE = 7
SERVER_EXCEPTION = 8
INVALID_P2_SINGLETON_PUZZLE_HASH = 9
```

## Security considerations