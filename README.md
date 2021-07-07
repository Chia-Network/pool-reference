## Pool Reference V1
This code is provided under the Apache 2.0 license.
Note: the draft specification is in the SPECIFICATION.md file.

### Summary
This repository provides a sample server written in python, which is meant to serve as a basis for a Chia Pool.
While this is a fully functional implementation, it requires some work in scalability and security to run in production.
An FAQ is provided here: https://github.com/Chia-Network/chia-blockchain/wiki/Pooling-FAQ


### Customizing
Several things are customizable in this pool reference. This includes:
* How long the timeout is for leaving the pool
* How difficulty adjustment happens
* Fees to take, and how much to pay in blockchain fees  
* How farmers' points are counted when paying (PPS, PPLNS, etc)
* How farmers receive payouts (XCH, BTC, ETH, etc), and how often
* What store (DB) is used - by default it's an SQLite db. Users can use their own store implementations, based on 
  `AbstractPoolStore`, by supplying them to `pool_server.start_pool_server`
* What happens (in terms of response) after a successful login

However, some things cannot be changed. These are described in SPECIFICATION.md, and mostly relate to validation,
protocol, and the singleton format for smart coins. 

### Pool Protocol Benefits
The Chia pool protocol has been designed for security, and decentralization, not relying on any 3rd party, closed code,
or trusted behaviour. 

* The farmer can never steal from the pool by double farming
* The farmer does not need collateral to join a pool, they only need a few cents to create a singleton
* The farmer can easily and securely change pools if they want to
* The farmer can run a full node (increasing decentralization)
* The farmer can log into another computer with only the 24 words, and the pooling configuration is detected, without
requiring a central server

### Pool Protocol Summary
When not pooling, farmers receive signage points from full nodes every 9 seconds, and send these signage points to the
harvester. Each signage point is sent along with the `sub_slot_iters` and `difficulty`, two network-wide parameters
which are adjusted every day (4608 blocks). The `sub_slot_iters` is the number of VDF iterations performed in 10
minutes for the fastest VDF in the network. This increases if the fastest timelord's speed increases. The difficulty
is similiarly affected by timelord speed (it goes up when timelord speed increases, since blocks come faster), but 
it's also affected by the total amount of space in the network. These two parameters determine how difficult it is
to "win" a block and find a proof.

Since only about 1 farmer wordwide finds a proof every 18.75 seconds, this means the chances of finding one are 
extremely small, with the default `difficulty` and `sub_slot_iters`. For pooling, what we do is we increase the 
`sub_slot_iters` to a constant, but very high number: 37600000000, and then we decrease the difficulty to an
artificially lower one, so that proofs can be found more frequently.

The farmer communicates with one or several pools through an HTTPS protocol, and sets their own local difficulty for
each pool. Then, when sending signage points to the harvester, the pool `difficulty` and `sub_slot_iters` are used. 
This means that the farmer can find proofs very often, perhaps every 10 minutes, even for small farmers. These proofs,
however, are not sent to the full node to create a block. They are instead only sent to the pool. This means that the 
other full nodes in the network do not have to see and validate everyone else's proofs, and the network can scale to
millions of farmers with no issue, as long as the pool scales properly. Since many farmers are part of the pool,
only 1 farmer needs to win a block, for the entire pool to be rewarded proportionally to their space.

The pool then keeps track of how many proofs (partials) each farmer sends, weighing them by difficulty. Occasionally 
(for example every 3 days), the pool can perform a payout to farmers based on how many partials they submitted. Farmers
with more space, and thus more points, will get linearly more rewards. 

Instead of farmers using a `pool_public_key` when plotting, they now use a puzzle hash, referred to as the 
`p2_singleton_puzzle_hash`, also known as the `pool_contract_address`. These values go into the plot itself, and 
cannot be changed after creating the plot, since they are hashed into the `plot_id`. The pool contract address is the
address of a chialisp contract called a singleton. The farmer must first create a singleton on the blockchain, which
stores the pool information of the pool that that singleton is assigned to. When making a plot, the address of that
singleton is used, and therefore that plot is tied to that singleton forever. When a block is found by the farmer, 
the pool portion of the block rewards (7/8, or 1.75XCH) go into the singleton, and when claimed, 
go directly to the pool's target address. 

The farmer can also configure their payout instructions, so that the pool knows where to send the occasional rewards
to.


### Receiving partials
A partial is a proof of space with some additional metadata and authentication info from the farmer, which
meets certain minimum difficulty requirements. Partials must be real proofs of space responding to blockchain signage
points, and they must be submitted within the blockchain time window (28 seconds after the signage point).

The pool server works by receiving partials from the users, validating that they are correct and correspond to a valid
signage point on the blockchain, and then adding them to a queue. A few minutes later, the pool pulls from the
queue, and checks that the signage point for that partial is still in the blockchain. If everything is good, the
partial is counted as valid, and the points are added for that farmer.


### Collecting pool rewards
![Pool absorbing rewards image](images/absorb.png?raw=true "Absorbing rewards")

The pool periodically searches the blockchain for new pool rewards (1.75 XCH) that go to the various
`p2_singleton_puzzle_hashes` of each of the farmers. These coins are locked, and can only be spent if they are spent
along with the singleton that they correspond to. The singleton is also locked to a `target_puzzle_hash`, which in
this diagram is the red pool address. Anyone can spend the singleton and the `p2_singleton_puzzle_hash` coin, as 
long as it's a block reward, and all the conditions are met. Some of these conditions require that the singleton
always create exactly 1 new child singleton with the same launcher id, and that the coinbase funds are sent to the 
`target_puzzle_hash`.

### Calculating farmer rewards

Periodically (for example once a day), the pool executes the code in `create_payment_loop`. This first sums up all the 
confirmed funds in the pool that have a certain number of confirmations.

Then, the pool divides the total amount by the points of all pool members, to obtain the `mojo_per_point` (minus the pool fee
and the blockchain fee). A new coin gets created for each pool member (and for the pool), and the payments are added
to the pending_payments list. Note that since blocks have a maximum size, we have to limit the size of each transaction.
There is a configurable parameter: `max_additions_per_transaction`. After adding the payments to the pending list,
the pool members' points are all reset to zero. This logic can be customized.


### 1/8 vs 7/8
Note that the coinbase rewards in Chia are divided into two coins: the farmer coin and the pool coin. The farmer coin
(1/8) only goes to the puzzle hash signed by the farmer private key, while the pool coin (7/8) goes to the pool.
The user transaction fees on the blockchain are included in the farmer coin as well. This split of 7/8 1/8 exists
to prevent attacks where one pool tries to destroy another by farming partials, but never submitting winning blocks.

### Difficulty
The difficulty allows the pool operator to control how many partials per day they are receiving from each farmer.
The difficulty can be adjusted separately for each farmer. A reasonable target would be 300 partials per day,
to ensure frequent feedback to the farmer, and low variability.
A difficulty of 1 results in approximately 10 partials per day per k32 plot. This is the minimum difficulty that
the V1 of the protocol supports is 1. However, a pool may set a higher minimum difficulty for efficiency. When
calculating whether a proof is high quality enough for being awarded points, the pool should use
`sub_slot_iters=37600000000`.
If the farmer submits a proof that is not good enough for the current difficulty, the pool should respond by setting
the `current_difficulty` in the response.

### Points
X points are awarded for submitting a partial with difficulty X, which means that points scale linearly with difficulty.
For example, 100 TiB of space should yield approximately 10,000 points per day, whether the difficulty is set to
100 or 200. It should not matter what difficulty is set for a farmer, as long as they are consistently submitting partials.
The specification does not require pools to pay out proportionally by points, but the payout scheme should be clear to
farmers, and points should be acknowledged and accumulated points returned in the response.


### Difficulty adjustment algorithm
This is a simple difficulty adjustment algorithm executed by the pool. The pool can also improve this or change it 
however they wish. The farmer can provide their own `suggested_difficulty`, and the pool can decide whether or not
to update that farmer's difficulty. Be careful to only accept the latest authentication_public_key when setting
difficulty or pool payout info. The initial reference client and pool do not use the `suggested_difficulty`.

- Obtain the last successful partial for this launcher id
- If > 3 hours, divide difficulty by 5
- If > 45 minutes < 6 hours, divide difficulty by 1.5
- If < 45 minutes:
   - If have < 300 partials at this difficulty, maintain same difficulty
   - Else, multiply the difficulty by (24 * 3600 / (time taken for 300 partials))
  
The 6 hours is used to handle rare cases where a farmer's storage drops dramatically. The 45 minutes is similar, but
for less extreme cases. Finally, the last case of < 45 minutes should properly handle users with increasing space,
or slightly decreasing space. This targets 300 partials per day, but different numbers can be used based on
performance and user preference.

### Making payments
Note that the payout info is provided with each partial. The user can choose where rewards are paid out to, and this
does not have to be an XCH address. The pool should ONLY update the payout info for successful partials with the
latest seen authentication key for that launcher_id.


### Install and run (Testnet)
To run a pool, you must use this along with the main branch of `chia-blockchain`.

1. Checkout the `main` branch of `chia-blockchain`, and install it. Checkout this repo in another directory next to (not inside) `chia-blockchain`.  
If you want to connect to testnet, use `export CHIA_ROOT=~/.chia/testnet9`, or whichever testnet you want to join, and 
   run `chia configure -t true`. You can also directly use the `pools.testnet9` branch, although this branch will
   be removed in the future (or past).

2. Create three keys, one which will be used for the block rewards from the blockchain, one to receive the pool fee that is kept by the pool, and the third to be a wallet that acts as a test user.

3. Change the `wallet_fingerprint` and `wallet_id` in the `config.yaml` config file, using the information from the first
key you created in step 2. These can be obtained by doing `chia wallet show`.

4. Do `chia keys show` and get the first address for each of the keys created in step 2. Put these into the `config.yaml` 
config file in `default_target_address` and `pool_fee_address` respectively.
   
5. Change the `pool_url` in `config.yaml` to point to your external ip or hostname. 
   This must match exactly with what the user enters into their UI or CLI, and must start with https://.
   
6. Start the node using `chia start farmer`, and log in to a different key (not the two keys created for the pool). 
This will be referred to as the farmer's key here. Sync up your wallet on testnet for the farmer key. 
You can log in to a key by running `chia wallet show` and then choosing each wallet in turn, to make them start syncing.

7. Create a venv (different from chia-blockchain) and start the pool server using the following commands:

```
cd pool-reference
python3 -m venv ./venv
source ./venv/bin/activate
pip install ../chia-blockchain/ 
sudo CHIA_ROOT="/your/home/dir/.chia/testnet9" ./venv/bin/python -m pool
```

You should see something like this when starting, but no errors:
```
INFO:root:Logging in: {'fingerprint': 2164248527, 'success': True}
INFO:root:Obtaining balance: {'confirmed_wallet_balance': 0, 'max_send_amount': 0, 'pending_change': 0, 'pending_coin_removal_count': 0, 'spendable_balance': 0, 'unconfirmed_wallet_balance': 0, 'unspent_coin_count': 0, 'wallet_id': 1}
```

8. Create a pool nft (on the farmer key) by doing `chia plotnft create -s pool -u https://127.0.0.1:80`, or whatever host:port you want
to use for your pool. Approve it and wait for transaction confirmation. This url must match *exactly* with what the 
   pool uses.
   
9. Do `chia plotnft show` to ensure that your plotnft is created. Now start making some plots for this pool nft.
You can make plots by specifying the -c argument in `chia plots create`. Make sure to *not* use the `-p` argument. The 
    value you should use for -c is the `P2 singleton address` from `chia plotnft show` output.
 You can start with small k25 plots and see if partials are submitted from the farmer to the pool server. The output
will be the following in the pool if everything is working:
```
INFO:root:Returning {'new_difficulty': 1963211364}, time: 0.017535686492919922 singleton: 0x1f8dab79a614a82f9834c8f395f5fe195ae020807169b71a10218b9788a7a573
```
    
Please send a message to @sorgente711 on keybase if you have questions about the 9 steps explained above. All other questions
should be send to the #pools channel in keybase. 

 
