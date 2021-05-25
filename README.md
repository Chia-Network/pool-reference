This prototoype is not yet supported and is still in development.
Note: the draft specification is in the SPECIFICATION.md file.

### Collecting pool rewards

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

- Obtain the last successful partial for this singleton genesis
- If > 3 hours, divide difficulty by 5
- If > 45 minutes < 6 hours, divide difficulty by 1.5
- If < 45 minutes:
   - If have < 300 partials at this difficulty, maintain same difficulty
   - Else, multiply the difficulty by (24 * 3600 / (time taken for 300 partials))
  
The 6 hours is used to handle rare cases where a farmer's storage drops dramatically. The 45 minutes is similar, but
for less extreme cases. Finally, the last case of < 45 minutes should properly handle users with increasing space,
or slightly decreasing space.

### Making payments
Note that the payout info is provided with each partial. The user can choose where rewards are paid out to, and this
does not have to be an XCH address. The pool should ONLY update the payout info for successful partials with the
latest seen authentication key for that singleton_genesis.


### Start the server
```
sudo CHIA_ROOT="/home/mariano/.chia/testnet7"  ./venv/bin/python pool_server.py
```