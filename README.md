This prototoype is not yet supported and is still in development.

### Minimum difficulty

### Collecting pool rewards

### Calculating farmer rewards

Periodically (for example once a day), the pool executes the code in `create_payment_loop`. This first sums up all the 
confirmed funds in the pool that have a certain number of confirmations.

Then, the pool divides the total amount by the points of all pool members, to obtain the `mojo_per_point` (minus the pool fee
and the blockchain fee). A new coin gets created for each pool member (and for the pool), and the payments are added
to the pending_payments list. Note that since blocks have a maximum size, we have to limit the size of each transaction.
There is a configurable parameter: `max_additions_per_transaction`. After adding the payments to the pending list,
the pool members' points are all reset to zero. This logic can be customized.


### Making payments

### Start the server
```
sudo CHIA_ROOT="/home/mariano/.chia/testnet7"  ./venv/bin/python pool_server.py
```