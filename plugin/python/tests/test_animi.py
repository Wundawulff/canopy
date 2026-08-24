"""ANIMI Chain: end-to-end tests for submit + vote against a mock state store."""

import asyncio
import hashlib
from types import SimpleNamespace

from contract.contract import (
    Contract,
    key_for_account,
    key_for_fee_pool,
    key_for_fee_params,
    key_for_submission,
    key_for_vote,
    marshal,
)
from contract.proto import (
    Account,
    Pool,
    FeeParams,
    MessageSubmit,
    MessageVote,
    Submission,
    Vote,
    PluginStateReadResponse,
    PluginStateWriteResponse,
    PluginCheckRequest,
    PluginDeliverRequest,
    Transaction,
)
from contract.error import PluginError

CHAIN_ID = 1
FEE = 10_000


class MockPlugin:
    """In-memory key/value store standing in for the FSM."""

    def __init__(self):
        self.state = {}

    async def state_read(self, contract, request):
        resp = PluginStateReadResponse()
        for read in request.keys:
            result = resp.results.add()
            result.query_id = read.query_id
            value = self.state.get(bytes(read.key))
            if value is not None:
                entry = result.entries.add()
                entry.key = read.key
                entry.value = value
        return resp

    async def state_write(self, contract, request):
        for op in request.sets:
            self.state[bytes(op.key)] = bytes(op.value)
        for op in request.deletes:
            self.state.pop(bytes(op.key), None)
        return PluginStateWriteResponse()


def build(balances):
    plugin = MockPlugin()
    plugin.state[key_for_fee_params()] = marshal(FeeParams(send_fee=FEE))
    plugin.state[key_for_fee_pool(CHAIN_ID)] = marshal(Pool(amount=0))
    for addr, amt in balances.items():
        plugin.state[key_for_account(addr)] = marshal(Account(address=addr, amount=amt))
    contract = Contract(
        config=SimpleNamespace(chain_id=CHAIN_ID),
        plugin=plugin,
        fsm_id=1,
    )
    return contract, plugin


def deliver(contract, msg, type_name, height=100, fee=FEE):
    tx = Transaction(fee=fee)
    tx.msg.Pack(msg, type_url_prefix="type.googleapis.com/")
    tx.msg.type_url = f"type.googleapis.com/types.{type_name}"
    req = PluginDeliverRequest(tx=tx, height=height)
    return asyncio.run(contract.deliver_tx(req))


ALICE = bytes.fromhex("11" * 20)   # creator
BOB = bytes.fromhex("22" * 20)     # voter
CAROL = bytes.fromhex("33" * 20)   # second voter
HASH = hashlib.sha256(b"animi-meme-001.mp4").digest()


def sample_submit(creator=ALICE, h=HASH):
    return MessageSubmit(
        creator_address=creator,
        content_hash=h,
        media_uri="ipfs://bafybeigdyrztanimimeme001",
        media_type=1,
        title="AI cat explains blockchain",
    )


# --------------------------------------------------------------------------
# check_tx validation
# --------------------------------------------------------------------------

def test_check_submit_valid():
    contract, _ = build({ALICE: 1_000_000})
    resp = contract._check_message_submit(sample_submit())
    assert list(resp.authorized_signers) == [ALICE]


def test_check_submit_rejects_bad_hash_length():
    contract, _ = build({ALICE: 1_000_000})
    msg = sample_submit(h=b"tooshort")
    try:
        contract._check_message_submit(msg)
        assert False, "should have raised"
    except PluginError as e:
        assert "content_hash must be 32 bytes" in e.msg


def test_check_submit_rejects_bad_uri_scheme():
    contract, _ = build({ALICE: 1_000_000})
    msg = sample_submit()
    msg.media_uri = "ftp://sketchy.example/meme.mp4"
    try:
        contract._check_message_submit(msg)
        assert False, "should have raised"
    except PluginError as e:
        assert "ipfs:// or https://" in e.msg


def test_check_vote_rejects_zero_amount():
    contract, _ = build({BOB: 1_000_000})
    try:
        contract._check_message_vote(
            MessageVote(voter_address=BOB, content_hash=HASH, amount=0)
        )
        assert False, "should have raised"
    except PluginError as e:
        assert "amount" in e.msg


# --------------------------------------------------------------------------
# deliver_tx: submission
# --------------------------------------------------------------------------

def test_submit_writes_record_and_charges_fee():
    contract, plugin = build({ALICE: 1_000_000})
    resp = deliver(contract, sample_submit(), "MessageSubmit")
    assert not resp.HasField("error"), resp.error

    stored = Submission.FromString(plugin.state[key_for_submission(HASH)])
    assert stored.creator_address == ALICE
    assert stored.title == "AI cat explains blockchain"
    assert stored.created_height == 100
    assert stored.vote_count == 0

    alice = Account.FromString(plugin.state[key_for_account(ALICE)])
    assert alice.amount == 1_000_000 - FEE
    pool = Pool.FromString(plugin.state[key_for_fee_pool(CHAIN_ID)])
    assert pool.amount == FEE


def test_submit_rejects_duplicate_hash():
    contract, _ = build({ALICE: 1_000_000})
    deliver(contract, sample_submit(), "MessageSubmit")
    resp = deliver(contract, sample_submit(), "MessageSubmit")
    assert resp.HasField("error")
    assert "already exists" in resp.error.msg


def test_submit_rejects_insufficient_funds():
    contract, _ = build({ALICE: 100})
    resp = deliver(contract, sample_submit(), "MessageSubmit")
    assert resp.HasField("error")
    assert "insufficient funds" in resp.error.msg


# --------------------------------------------------------------------------
# deliver_tx: voting
# --------------------------------------------------------------------------

def test_vote_tips_creator_and_records_vote():
    contract, plugin = build({ALICE: 1_000_000, BOB: 500_000})
    deliver(contract, sample_submit(), "MessageSubmit")

    tip = 25_000
    resp = deliver(
        contract,
        MessageVote(voter_address=BOB, content_hash=HASH, amount=tip),
        "MessageVote",
        height=105,
    )
    assert not resp.HasField("error"), resp.error

    sub = Submission.FromString(plugin.state[key_for_submission(HASH)])
    assert sub.vote_count == 1
    assert sub.total_tipped == tip

    alice = Account.FromString(plugin.state[key_for_account(ALICE)])
    bob = Account.FromString(plugin.state[key_for_account(BOB)])
    assert alice.amount == 1_000_000 - FEE + tip
    assert bob.amount == 500_000 - tip - FEE

    vote = Vote.FromString(plugin.state[key_for_vote(HASH, BOB)])
    assert vote.voter_address == BOB and vote.amount == tip and vote.height == 105

    pool = Pool.FromString(plugin.state[key_for_fee_pool(CHAIN_ID)])
    assert pool.amount == FEE * 2


def test_vote_rejects_double_vote():
    contract, _ = build({ALICE: 1_000_000, BOB: 500_000})
    deliver(contract, sample_submit(), "MessageSubmit")
    deliver(contract, MessageVote(voter_address=BOB, content_hash=HASH, amount=1_000), "MessageVote")
    resp = deliver(
        contract, MessageVote(voter_address=BOB, content_hash=HASH, amount=1_000), "MessageVote"
    )
    assert resp.HasField("error")
    assert "already voted" in resp.error.msg


def test_vote_rejects_self_vote():
    contract, _ = build({ALICE: 1_000_000})
    deliver(contract, sample_submit(), "MessageSubmit")
    resp = deliver(
        contract, MessageVote(voter_address=ALICE, content_hash=HASH, amount=1_000), "MessageVote"
    )
    assert resp.HasField("error")
    assert "own submission" in resp.error.msg


def test_vote_rejects_unknown_submission():
    contract, _ = build({BOB: 500_000})
    missing = hashlib.sha256(b"does-not-exist").digest()
    resp = deliver(
        contract, MessageVote(voter_address=BOB, content_hash=missing, amount=1_000), "MessageVote"
    )
    assert resp.HasField("error")
    assert "no submission found" in resp.error.msg


def test_vote_rejects_insufficient_funds():
    contract, _ = build({ALICE: 1_000_000, BOB: 1_000})
    deliver(contract, sample_submit(), "MessageSubmit")
    resp = deliver(
        contract, MessageVote(voter_address=BOB, content_hash=HASH, amount=999_999), "MessageVote"
    )
    assert resp.HasField("error")
    assert "insufficient funds" in resp.error.msg


def test_two_voters_accumulate():
    contract, plugin = build({ALICE: 1_000_000, BOB: 500_000, CAROL: 500_000})
    deliver(contract, sample_submit(), "MessageSubmit")
    deliver(contract, MessageVote(voter_address=BOB, content_hash=HASH, amount=3_000), "MessageVote")
    deliver(contract, MessageVote(voter_address=CAROL, content_hash=HASH, amount=7_000), "MessageVote")

    sub = Submission.FromString(plugin.state[key_for_submission(HASH)])
    assert sub.vote_count == 2
    assert sub.total_tipped == 10_000


def test_vote_keys_are_distinct_per_voter():
    assert key_for_vote(HASH, BOB) != key_for_vote(HASH, CAROL)
    other = hashlib.sha256(b"other").digest()
    assert key_for_vote(HASH, BOB) != key_for_vote(other, BOB)


# --------------------------------------------------------------------------
# check_tx routing (type_url dispatch) — the full path the FSM actually calls
# --------------------------------------------------------------------------

def check(contract, msg, type_name, fee=FEE):
    tx = Transaction(fee=fee)
    tx.msg.Pack(msg, type_url_prefix="type.googleapis.com/")
    tx.msg.type_url = f"type.googleapis.com/types.{type_name}"
    return asyncio.run(contract.check_tx(PluginCheckRequest(tx=tx, height=100)))


def test_check_tx_routes_submit():
    contract, _ = build({ALICE: 1_000_000})
    resp = check(contract, sample_submit(), "MessageSubmit")
    assert not resp.HasField("error"), resp.error
    assert list(resp.authorized_signers) == [ALICE]


def test_check_tx_routes_vote():
    contract, _ = build({BOB: 1_000_000})
    resp = check(contract, MessageVote(voter_address=BOB, content_hash=HASH, amount=5), "MessageVote")
    assert not resp.HasField("error"), resp.error
    assert list(resp.authorized_signers) == [BOB]


def test_check_tx_rejects_fee_below_minimum():
    contract, _ = build({ALICE: 1_000_000})
    resp = check(contract, sample_submit(), "MessageSubmit", fee=1)
    assert resp.HasField("error")
    assert "fee" in resp.error.msg.lower()


def test_check_tx_rejects_unknown_type():
    contract, _ = build({ALICE: 1_000_000})
    resp = check(contract, sample_submit(), "MessageNonsense")
    assert resp.HasField("error")
