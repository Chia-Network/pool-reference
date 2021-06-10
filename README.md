This prototype is not yet supported and is still in development.
This code is provided under the Apache 2.0 license.
Note: the draft specification is in the SPECIFICATION.md file.

### Customizing
Several things are customizable in this pool reference. This includes:
* How long the timeout is for leaving the pool
* How difficulty adjustment happens
* Fees to take, and how much to pay in blockchain fees  
* How farmers' points are counted when paying (PPS, PPLNS, etc)
* How farmers receive payouts (XCH, BTC, ETH, etc), and how often

However, some things cannot be changed. These are described in SPECIFICATION.md, and mostly relate to validation,
protocol, and the singleton format for smart coins. 

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
To run a pool, you must use this along with a branch of `chia-blockchain`.

1. Checkout the `pools.2021-may-25` branch of `chia-blockchain`, and install it. Checkout this repo in another
directory next to (not inside) `chia-blockchain`. Make sure to be on testnet by doing `export CHIA_ROOT=".chia/testnet7"` and `chia configure --testnet true`.

2. Create two keys, one which will be used for the block rewards from the blockchain, and the other
which will receive the pool fee that is kept by the pool.

3. Change the `wallet_fingerprint` and `wallet_id` in the `config.yaml` config file, using the information from the first
key you created in step 2. These can be obtained by doing `chia wallet show`.

4. Do `chia keys show` and get the first address for each of the keys created in step 2. Put these into the `config.yaml` 
config file in `default_target_puzzle_hash` and `pool_fee_puzzle_hash` respectively.
   
5. Change the pool_url in pool.py to point to your external ip or hostname. 
   This must match exactly with what the user enters into their UI or CLI, and must start with https://. For now
   http:// can also be used.
   
6. If you would like to test with smaller plots (instead of using k32s), go to the file `default_constants.py` and 
increase POOL_SUB_SLOT_ITERS from 37600000000 to 37600000000 * (2**11). The default value with difficulty 1 (the lowest)
   will result in 10 partials per day per k32. This makes it difficult to test due to large plots.

7. Start the node using `chia start farmer`, and log in to a different key (not the two keys created for the pool). 
This will be referred to as the farmer's key here. Sync up your wallet on testnet for the farmer key. 

8. Create a venv (different from chia-blockchain) and start the pool server using the following commands:

```
cd pool-reference
python3 -m venv ./venv
source ./venv/bin/activate
pip install ../chia-blockchain/ 
sudo CHIA_ROOT="/your/home/dir/.chia/testnet7" ./venv/bin/python pool/pool_server.py
```

You should see something like this when starting, but no errors:
```
INFO:root:Logging in: {'fingerprint': 2164248527, 'success': True}
INFO:root:Obtaining balance: {'confirmed_wallet_balance': 0, 'max_send_amount': 0, 'pending_change': 0, 'pending_coin_removal_count': 0, 'spendable_balance': 0, 'unconfirmed_wallet_balance': 0, 'unspent_coin_count': 0, 'wallet_id': 1}
```

9. Create a pool nft (on the farmer key) by doing `chia plotnft create -u http://127.0.0.1:80`, or whatever host:port you want
to use for your pool. Approve it and wait for transaction confirmation. This url must match *exactly* with what the 
   pool uses.
   
10. Do `chia plotnft show` to ensure that your plotnft is created. Now start making some plots for this pool nft.
You can make plots by specifying the -c argument in `chia plots create`. Make sure to *not* use the `-p` argument. The 
    value you should use for -c is the `P2 singleton address` from `chia plotnft show` output.
 You can start with small k25 plots and see if partials are submitted from the farmer to the pool server. The output
will be the following in the pool if everything is working:
```
INFO:root:Returning {'points_balance': 82629918227, 'current_difficulty': 1963211364}, time: 0.017535686492919922 singleton: 0x1f8dab79a614a82f9834c8f395f5fe195ae020807169b71a10218b9788a7a573
```
    
Note that switching pools is still not enabled, but will be added very shortly. Please
send a message to @sorgente711 on keybase if you have questions about the 10 steps explained above. All other questions
should be send to the #pools channel in keybase. Note that there will probably be breaking changes soon which will
require re-plotting and re-running all the steps above. 

