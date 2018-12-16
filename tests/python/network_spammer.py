"""Send prioritized and unprioritized transactions and verify prioritized transactions get priority when added to ledgers."""
import argparse
import asyncio
import csv
import logging
import math
import queue
import time
from datetime import datetime
from typing import List

from kin import KinClient, Environment as KinEnvironment, Keypair

from helpers import (TX_SET_SIZE, NETWORK_NAME,
                     send_txs, get_sequences)


AVG_BLOCK_TIME = 5  # seconds


logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s %(message)s')


def parse_args():
    """Generate and parse CLI arguments."""
    parser = argparse.ArgumentParser()

    parser.add_argument('--length', required=True, type=int, help='Test length in seconds')
    parser.add_argument('--txs-per-ledger', required=True, type=int, help='Transaction rate to submit (spam) in parallel for every ledger round')
    parser.add_argument('--prioritizer-seeds-file', required=True, type=str, help='Filepath to prioritizer seeds file')
    parser.add_argument('--spammer-seeds-file', required=True, type=str, help='Filepath to spammer seeds file')
    parser.add_argument('--csv', default='spam-results-{}.csv'.format(str(int(time.time()))), type=str, help='Spam results CSV output')

    parser.add_argument('--passphrase', type=str, help='Network passphrase')
    parser.add_argument('--horizon', action='append',
                        help='Horizon endpoint URL (use multiple --horizon flags for multiple addresses)')

    return parser.parse_args()


def load_accounts(path) -> List[Keypair]:
    """Load seeds from file path and return Keypair list.

    Expected file format is a newline-delimited seed list.
    """
    kps = []
    with open(path) as f:
        for seed in f:
            kps.append(Keypair(seed))
    return kps


def spam(env, prioritizers, builders, length, tx_per_ledger):
    """Have each account submit transactions every 5s (avg block time) for given length in seconds."""
    coroutines = []

    # create a queue of builders.
    # we will fetch a batch of builders for each spam round
    builders_queue = queue.Queue()
    for b in builders:
        builders_queue.put(b)

    rounds = math.ceil(length // AVG_BLOCK_TIME)
    for rnd in range(rounds):
        round_builders = [builders_queue.get() for _ in range(tx_per_ledger)]

        coroutines.append(spam_round(env, prioritizers,
                                     round_builders[:TX_SET_SIZE - 1],
                                     round_builders[TX_SET_SIZE - 1:tx_per_ledger],
                                     rnd))

        # push this round's builders to the back of the queue.
        # they will be reused in the next X  ledgers,
        # where x = (spammer_seeds // tx_per_ledger)
        for b in round_builders:
            builders_queue.put(b)

    return asyncio.gather(*coroutines)


# all transactions are payments to the same address
#
# TODO can this potentially create deadlocks when applying them?
async def spam_round(env, prioritizers: List[Keypair], prioritized_builders, unprioritized_builders, rnd):
    """Send prioritized and unprioritzied transactions for a single ledger.

    All prioritized transactions are expected to be included in the next ledger.
    Only one out of all unprioritized transactions is expected to be included in the next ledger.

    Return a result list and hash of all submitted transactions.
    """
    payment_amount = 1
    payment_dest = prioritizers[0]  # all transactions are payments to the same address

    # this round should be started only after all previous ledgers have been
    # added, so we sleep until then
    await asyncio.sleep(rnd * AVG_BLOCK_TIME)

    # generate unprioritized payment transactions
    # we submit them first because we want to test if prioritized transactions
    # actually get priority over them
    tx_metadata = {}
    xdrs = []
    for i, _ in enumerate(unprioritized_builders):
        builder = unprioritized_builders[i]
        builder.append_payment_op(payment_dest.public_address, str(payment_amount))
        builder.sign(builder.keypair.seed().decode())

        xdrs.append(builder.gen_xdr())

        # cache tx hash for later review
        tx_metadata[builder.hash_hex()] = {'round': rnd,
                                           'prioritized': False}

    # generate prioritized transactions
    for i, _ in enumerate(prioritized_builders):
        builder = prioritized_builders[i]
        builder.append_payment_op(payment_dest.public_address, str(payment_amount))
        builder.sign(builder.keypair.seed().decode())

        # prioritize transaction by adding a prioritizer signature
        builder.sign(prioritizers[i].secret_seed)

        xdrs.append(builder.gen_xdr())

        tx_metadata[builder.hash_hex()] = {'round': rnd,
                                           'prioritized': True}

    # remember submission time and send txs
    submission_time = time.time()

    # some transactions can fail with HTTP 500 Server Error or HTTP 504 Server
    # Timeout, so don't raise an exception if this happens
    tx_results = await send_txs(env.horizon_uri, xdrs, expected_statuses=[200, 500, 504])

    # fetch builder sequence number.
    #
    # NOTE this is necessary because not all transactions have been processed
    # when we reach this stage,specifically unprioritized ones.
    # thus, it is unknown if the sequence has increased or not
    all_builders = prioritized_builders + unprioritized_builders
    sequences = await get_sequences(prioritized_builders[0].horizon.horizon_uri,
                                    [b.address for b in all_builders])

    # update builder sequence number
    for i, _ in enumerate(all_builders):
        b = all_builders[i]
        b.clear()
        b.sequence = str(sequences[i])

    # create tx result object for each tx for reviewing later on
    results = []
    for (hsh, metadata), result in zip(tx_metadata.items(), tx_results):
        try:
            ledger = result['ledger']
        except KeyError:  # probably 504
            ledger = None
        results.append({'hash': hsh,
                        'round': metadata['round'],
                        'prioritized': metadata['prioritized'],
                        'ledger': ledger,
                        'submission_time': submission_time})

    return results


LEDGERS = {}


def ledger_time(ledger, horizon):
    """Return ledger close time."""
    # failed txs have ledger = None
    # so we return an empty timestamp instead
    if not ledger:
        return ''

    global LEDGERS
    if ledger not in LEDGERS:
        logging.debug('fetching ledger %d', ledger)
        date_str = horizon.ledger(ledger)['closed_at']
        LEDGERS[ledger] = datetime.strptime(date_str, '%Y-%m-%dT%H:%M:%SZ').timestamp()
    return LEDGERS[ledger]


async def main():
    args = parse_args()

    prioritizer_kps = [Keypair(kp) for kp in load_accounts(args.prioritizer_seeds_file)]
    spam_builders = [Keypair(kp) for kp in load_accounts(args.spammer_seeds_file)]

    env = KinEnvironment(NETWORK_NAME, args.horizon[0], args.passphrase)
    results = await spam(env, prioritizer_kps, spam_builders, args.length, args.txs_per_ledger)

    # save results into csv file
    horizon = KinClient(env).horizon
    with open(args.csv, 'w') as csvfile:
        w = csv.DictWriter(csvfile, fieldnames=list(results[0][0].keys()) + ['ledger_time'])
        w.writeheader()
        for spam_round in results:
            for tx in spam_round:
                w.writerow({**tx, **{'ledger_time': ledger_time(tx['ledger'], horizon)}})


if __name__ == '__main__':
    asyncio.run(main())