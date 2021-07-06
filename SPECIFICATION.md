# Chia Pool Protocol 1.0

This is the initial version of the Chia Pool Protocol. It is designed to be simple, and to be extended later.
It relies on farmers having smart coins (referred to as Pool NFTs in GUI + CLI) which allow them to switch between pools
by making transactions on the blockchain. Furthermore, it decreases the reliance on pools for block production, since
the protocol only handles distribution of rewards, and it protects against pools or farmers acting maliciously.


## Security considerations
The pool must ensure that partials arrive quickly, faster than the 28 second time limit of inclusion into the
blockchain. This allows farmers that have slow setups to detect issues.

The Pool server must check that the `pool_contract_puzzle_hash` a.k.a. `p2_singleton_puzzle_hash` matches the
puzzle that they expect. Otherwise, the pool has no guarantee that users will not attempt to claim block rewards
for themselves, and immediately leave the pool, something that the provided smart contract prevents.

The Chia client must only connect to the pool configuration URL via HTTPS over TLS >= 1.2. This is to
prevent session hijacking, leading to user funds being stolen.


## Parties

The parties involved in the pool protocol are the pool operator and farmers. Each farmer is running
a farmer process, and any number of harvester processes connected to that farmer process. The full node can either be
run by the farmer (the default in the Chia GUI application), or run by the pool operator. If the farmer does not want
to run a full node, they can configure their node to connect to a remote full node.

A pool operator can support any number of farmers.

## Farmer identification
A farmer can be uniquely identified by the identifier of the farmer's singleton on the blockchain, this is what
`launcher_id` refers to. The `launcher_id` can be used as a primary key in a database. The pool must periodically check
the singleton's state on the blockchain to validate that it's farming to the pool, and not leaving or farming to another
pool.

## Farmer authentication
For the farmer to authenticate to the pool the following time based authentication token scheme must be added to the
signing messages of some endpoints.

```
authentication_token = current_utc_minutes / authentication_token_timeout
```

Where `authentication_token_timeout` is a configuration parameter of the pool which is also included in the
[GET /pool_info](#get-pool_info) response that must be respected by the farmer. Whereas `current_utc_minutes` is the
local UTC timestamp in **minutes** at the moment of signing. The local clock should ideally be in sync with a time
synchronization protocol e.g., NTP. The authentication token is usually included in a signed payload.

## HTTPS Endpoints Summary

The pool protocol consists of several HTTPS endpoints which return JSON responses. The HTTPS server can run on any port,
but must be running with TLS enabled (using a CA approved certificate), and with pipelining enabled.
All bytes values are encoded as hex with optional 0x in front. Clients are also expected to run with pipelining.

- [GET /pool_info](#get-pool_info)
- [GET /farmer](#get-farmer)
- [POST /farmer](#post-farmer)
- [PUT /farmer](#put-farmer)
- [POST /partial](#post-partial)
- [GET /login (Optional)](#get-login)

## Error codes

A failed endpoint will always return a JSON object with an error code and an
english error message as shown below:

```json
{"error_code": 0, "error_message": ""}
```

The following errors may occur:

|Error code|Description|
|---|---|
| 0x01 | The provided signage point has been reverted |
| 0x02 | Received partial too late |
| 0x03 | Not found |
| 0x04 | Proof of space invalid |
| 0x05 | Proof of space not good enough |
| 0x06 | Invalid difficulty |
| 0x07 | Invalid signature |
| 0x08 | Web-Server raised an exception|
| 0x09 | Invalid puzzle hash|
| 0x0A | Farmer not known |
| 0x0B | Farmer already known |
| 0x0C | Invalid authentication public key |
| 0x0D | Invalid payout instructions |
| 0x0E | Invalid singleton |
| 0x0F | Delay time too short |
| 0x10 | Request failed |

## Signature validation

Most of the endpoints require signature validation. The validation requires serialization of the endpoints payloads
to calculate the message hash which is done like:

```
message_hash = sha256(serialized_payload)
```

The serialized payload must follow the `Streamable` standard defined
[here](https://github.com/Chia-Network/chia-blockchain/blob/main/chia/util/streamable.py).

## Pool URL
The pool URL is the url that farmers use to connect to the pool. The subdomains, port, and path are optional. The client
will use 443 if there is no port. Note that the trailing slash must NOT be present. Everything must be lower case.
```
https://subdomain.domain.tld:port/path
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
    "protocol_version": 1,
    "relative_lock_height": 100,
    "target_puzzle_hash": "0x344587cf06a39db471d2cc027504e8688a0a67cce961253500c956c73603fd58",
    "authentication_token_timeout": 5
}
```

#### description
The description is a short paragraph that can be displayed in GUIs when the farmer enters a pool URL.

#### fee
The fee that the pool charges by default, a number between 0.0 (0.0%) and 1.0 (100.0%). This does not include blockchain
transaction fees.

#### logo_url
A URL for a pool logo that the client can display in the UI. This is optional for v1.0.

#### minimum_difficulty
The minimum difficulty that the pool supports. This will also be the default that farmers start sending proofs for.

#### name
Name of the pool, this is only for display purposes and does not go on the blockchain.

#### protocol_version
The pool protocol version supported by the pool.

#### relative_lock_height
The number of blocks (confirmations) that a user must wait between the point when they start escaping a pool, and the
point at which they can finalize their pool switch. Must be less than 4608 (approximately 24 hours).

#### target_puzzle_hash
This is the target of where rewards will be sent to from the singleton. Controlled by the pool.

#### authentication_token_timeout
The time in **minutes** for an `authentication_token` to be valid, see [Farmer authentication](#farmer-authentication).

## GET /farmer
Get the latest information for a farmer.

Request parameter:
```
- launcher_id
- authentication_token
- signature
```

Example request:
```
https://poolurl.com/farmer/launcher_id=:launcher_id&authentication_token=:token&signature=:signature
```

Successful response:
```json
{
    "authentication_public_key": "0x970e181ae45435ae696508a78012dc80548c334cf29676ea6ade7049eb9d2b9579cc30cb44c3fd68d35a250cfbc69e29",
    "payout_instructions": "0xc2b08e41d766da4116e388357ed957d04ad754623a915f3fd65188a8746cf3e8",
    "current_difficulty": 10,
    "current_points": 10
}
```

### Parameter
#### launcher_id
The unique identifier of the farmer's singleton, see [Farmer identification](#farmer-identification).

#### authentication_token
See [Farmer authentication](#farmer-authentication) for the specification of
`authentication_token`.

#### signature
This is a BLS signature of the hashed serialization of the following data in the given order:

|Element|Type|
|---|---|
|method_name| string|
|launcher_id | bytes32 |
|target_puzzle_hash | bytes32 |
|authentication_token | uint64 |

where `method_name` must be the serialized string `"get_farmer"`, the parameters must be serialized and hashed
according to [Signature validation](#signature-validation) and the signature must be signed by the private key of the
`authentication_public_key` using the Augmented Scheme in the BLS IETF spec.

where the parameter must be serialized and hashed according to [Signature validation](#signature-validation) and the
signature must be signed by the private key of the `authentication_public_key` using the Augmented Scheme in the BLS
IETF spec.

Note: The pool MUST return the current points balance, which is the total number of points found since the last 
payout for that user. 

## POST /farmer
Register a farmer with the pool. This is required once before submitting the first partial.

Request:
```json
{
    "payload": {
        "launcher_id": "0xae4ef3b9bfe68949691281a015a9c16630fc8f66d48c19ca548fb80768791afa",
        "authentication_token": 27062279,
        "authentication_public_key": "0x970e181ae45435ae696508a78012dc80548c334cf29676ea6ade7049eb9d2b9579cc30cb44c3fd68d35a250cfbc69e29",
        "payout_instructions": "0xc2b08e41d766da4116e388357ed957d04ad754623a915f3fd65188a8746cf3e8",
        "suggested_difficulty": 10
    },
    "signature": "0xa078dc1462bbcdec7cd651c5c3d7584ac6c6a142e049c7790f3b0ee8768ed6326e3a639f949b2293469be561adfa1c57130f64334994f53c1bd12e59579e27127fbabadc5e8793a2ef194a5a22ac832e92dcb6ad9a0d33bd264726f6e8df6aad"
}
```

Successful response:
```json
{"welcome_message" : "Welcome to the reference pool. Happy farming."}
```

A successful response must always contain a welcome message which must be defined by the pool.

#### payload

#### payload.launcher_id
The unique identifier of the farmer's singleton, see [Farmer identification](#farmer-identification).

#### payload.authentication_token
See [Farmer authentication](#farmer-authentication) for the specification of
`authentication_token`.

#### payload.authentication_public_key
The public key of the authentication key, which is a temporary key used by the farmer to sign requests
to the pool. It is authorized by the `owner_key`, so that the owner key can be kept more secure. The pool should reject
requests made with outdated `authentication_keys`. These key can be changed using `PUT /farmer`, which is signed with
the owner key.

#### payload.payout_instructions
These are the instructions for how the farmer wants to get paid. By default this will be an XCH address, but it can
be set to any string with a size of less than 1024 characters, so it can represent another blockchain or payment
system identifier.

#### payload.suggested_difficulty
A request from the farmer to update the difficulty. Can be ignored or respected by the pool. However, this should only
be respected if the authentication public key is the most recent one seen for this farmer.

See [Difficulty](#difficulty) for more details about the impact of the difficulty.

#### signature
This is a BLS signature of the hashed serialization of the payload:

```
sha256(PostFarmerPayload)
```

signed by the private key of the `owner_public_key` using the Augmented Scheme in the BLS IETF spec.

See the [streamable](#signature-validation) class `PostFarmerPayload` in the
[pool protocol](https://github.com/Chia-Network/chia-blockchain/blob/main/chia/protocols/pool_protocol.py)
and [Farmer authentication](#farmer-authentication) for the specification of `authentication_token`.

## PUT /farmer
Allows farmers to update their information on the pool.

Request:
```json
{
    "payload": {
        "launcher_id": "0xae4ef3b9bfe68949691281a015a9c16630fc8f66d48c19ca548fb80768791afa",
        "authentication_token": 27062279,
        "authentication_public_key": "0x970e181ae45435ae696508a78012dc80548c334cf29676ea6ade7049eb9d2b9579cc30cb44c3fd68d35a250cfbc69e29",
        "payout_instructions": "0xc2b08e41d766da4116e388357ed957d04ad754623a915f3fd65188a8746cf3e8",
        "suggested_difficulty": 10
    },
    "signature": "0xa078dc1462bbcdec7cd651c5c3d7584ac6c6a142e049c7790f3b0ee8768ed6326e3a639f949b2293469be561adfa1c57130f64334994f53c1bd12e59579e27127fbabadc5e8793a2ef194a5a22ac832e92dcb6ad9a0d33bd264726f6e8df6aad"
}
```

For a description of the request body entries see the corresponding keys in [POST /farmer](#post-farmer). The values
provided with the key/value pairs are used to update the existing values on the server. All entries, except
`launcher_id`, are optional but there must be at least one of them. 

See the [streamable](#signature-validation) class `PutFarmerPayload` in the
[pool protocol](https://github.com/Chia-Network/chia-blockchain/blob/main/chia/protocols/pool_protocol.py) for details
and [Farmer authentication](#farmer-authentication) for the specification of
`authentication_token`.

Successful response:
```json
{
  "authentication_public_key": true,
  "payout_instructions": true,
  "suggested_difficulty": true
}
```

A successful response must always contain one key/value pair for each entry provided in the request body. The value
must be `true` if the entry has been updated or `false` if the value was the same as the current value.

See below for an example body to only update the authentication key:

Example to update `authentication_public_key`:
```json
{
    "payload": {
        "launcher_id": "0xae4ef3b9bfe68949691281a015a9c16630fc8f66d48c19ca548fb80768791afa",
        "authentication_public_key": "0x970e181ae45435ae696508a78012dc80548c334cf29676ea6ade7049eb9d2b9579cc30cb44c3fd68d35a250cfbc69e29"
    },
    "signature": "0xa078dc1462bbcdec7cd651c5c3d7584ac6c6a142e049c7790f3b0ee8768ed6326e3a639f949b2293469be561adfa1c57130f64334994f53c1bd12e59579e27127fbabadc5e8793a2ef194a5a22ac832e92dcb6ad9a0d33bd264726f6e8df6aad"
}
```

## POST /partial
This is a partial submission from the farmer to the pool operator.

Request:
```json
{
  "payload": {
    "launcher_id": "0xae4ef3b9bfe68949691281a015a9c16630fc8f66d48c19ca548fb80768791afa",
    "authentication_token": 27062279,
    "proof_of_space": {
      "challenge": "0xe0e55d45eef8d53a6b68220abeec8f14f57baaa80dbd7b37430e42f9ac6e2c0e",
      "pool_contract_puzzle_hash": "0x9e3e9b37b54cf6c7467e277b6e4ca9ab6bdea53cdc1d79c000dc95b6a3908a3b",
      "plot_public_key": "0xa7ad70989cc8f18e555e9b698d197cdfc32465e0b99fd6cf5fdbac8aa2da04b0704ba04d2d50d852402f9dd6eec47a4d",
      "size": 32,
      "proof": "0xb2cd6374c8db249ad3b638199dbb6eb9eaefe55042cef66c43cf1e31161f4a1280455d8b53c2823c747fd4f8823c44de3a52cc85332431630857c359935660c3403ae3a92728d003dd66ef5966317cd49894d265a3e4c43f0530a1192874ed327e6f35862a25dfb67c5d0d573d078b4b8ba9bfb1cce52fd17939ae9d7033d3aa09d6c449e392ba2472a1fecf992abcc51c3bf5d56a72fef9900e79b8dba88a5afc39e04993325a0cd6b67757355b836f"
    },
    "sp_hash": "0x4c52796ca4ff775fbcdac90140c12270d26a37724ad77988535d58b376332533",
    "end_of_sub_slot": false,
    "harvester_id": "0xb9d8de98ec5c026f1167b0b587715d7137f43b6d1d40b81d9aac6dc8355fde28"
  },
  "aggregate_signature": "0xa078dc1462bbcdec7cd651c5c3d7584ac6c6a142e049c7790f3b0ee8768ed6326e3a639f949b2293469be561adfa1c57130f64334994f53c1bd12e59579e27127fbabadc5e8793a2ef194a5a22ac832e92dcb6ad9a0d33bd264726f6e8df6aad"
}
```

Successful response:
```json
{"new_difficulty": 10}
```

A successful response must always contain the new difficulty which must be respected by the farmer.

#### payload
This is the main payload of the partial, which is signed by two keys: `authentication_key` and `plot_key`.

#### payload.launcher_id
The unique identifier of the farmer's singleton, see [Farmer identification](#farmer-identification).

#### payload.authentication_token
See [Farmer authentication](#farmer-authentication) for the specification of `authentication_token`.

#### payload.proof_of_space
The proof of space in chia-blockchain format.

#### payload.proof_of_space.challenge
The challenge of the proof of space, computed from the signage point or end of subslot.

#### payload.proof_of_space.pool_contract_puzzle_hash
The puzzle hash that is encoded in the plots, equivalent to the `p2_singleton_puzzle_hash`. This is the first address
that the 7/8 rewards get paid out to in the blockchain, if this proof wins. This value can be derived from the
`launcher_id`, and must be valid for all partials.

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

#### aggregate_signature
This is a 2/2 BLS signature of the hashed serialization of the payload:

```
sha256(PostPartialPayload)
```

signed by the private keys of the following keys using the Augmented Scheme in the BLS IETF spec:

1. `plot_public_key`
2. `authentication_public_key`

See the [streamable](#signature-validation) class `PostPartialPayload` in the
[pool protocol](https://github.com/Chia-Network/chia-blockchain/blob/main/chia/protocols/pool_protocol.py) for details
and [Farmer authentication](#farmer-authentication) for the specification of
`authentication_token`.

A partial must be completely rejected if the BLS signature does not validate.

## GET /login
This allows the user to log in to a web interface if the pool supports it, see service flags in
[GET /pool_info](#get-pool_info). The farmer software must offer a way to generate and display a login link or provide 
a button which generates the link and then just opens it in the default browser. The link follows the specification
below.

Note that there is no explicit account creation. A farmer can log in after making their self known at the pool with
[POST /farmer](#post-farmer).

Request parameters:
```
- launcher_id
- authentication_token
- signature
```

Example request:
```
https://poolurl.com/login?launcher_id=:launcher_id&authentication_token=:token&signature=:signature
```

#### launcher_id
The unique identifier of the farmer's singleton, see [Farmer identification](#farmer-identification).

#### authentication_token
See [Farmer authentication](#farmer-authentication) for the specification of
`authentication_token`.

#### target_puzzle_hash
The pool's target puzzle hash, see [GET /pool_info](#get-pool_info)

#### signature
This is a BLS signature of the hashed serialization of the following data in the given order:

|Element|Type|
|---|---|
|method_name| string|
|launcher_id | bytes32 |
|target_puzzle_hash | bytes32 |
|authentication_token | uint64 |

where `method_name` must be the serialized string `"get_login"`, the parameters must be serialized and hashed
according to [Signature validation](#signature-validation) and the signature must be signed by the private key of the
`authentication_public_key` using the Augmented Scheme in the BLS IETF spec.

where the parameter must be serialized and hashed according to [Signature validation](#signature-validation) and the
signature must be signed by the private key of the `authentication_public_key` using the Augmented Scheme in the BLS
IETF spec.


## Difficulty
The difficulty allows the pool operator to control how many partials per day they are receiving from each farmer.
The difficulty can be adjusted separately for each farmer. A reasonable target would be 300 partials per day,
to ensure frequent feedback to the farmer, and low variability.
A difficulty of 1 results in approximately 10 partials per day per k32 plot. This is the minimum difficulty that
the V1 of the protocol supports is 1. However, a pool may set a higher minimum difficulty for efficiency. When
calculating whether a proof is high quality enough for being awarded points, the pool should use
`sub_slot_iters=37600000000`.
If the farmer submits a proof that is not good enough for the current difficulty, the pool should respond by setting
the `current_difficulty` in the response.

## Points
X points are awarded for submitting a partial with difficulty X, which means that points scale linearly with difficulty.
For example, 100 TiB of space should yield approximately 10,000 points per day, whether the difficulty is set to
100 or 200. It should not matter what difficulty is set for a farmer, as long as they are consistently submitting partials.
The specification does not require pools to pay out proportionally by points, but the payout scheme should be clear to
farmers, and points should be acknowledged and accumulated points returned in the response.
