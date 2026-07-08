"""Tests for on-chain redemption primitives and the PolygonRedeemer control flow.

The signing test is the load-bearing one: it proves the SafeTx digest + owner
signature recover to the signing EOA (ecrecover), so the Safe will accept it.
Network I/O is mocked — no real RPC or chain is touched.
"""
import pytest
from eth_abi import decode as abi_decode
from eth_account import Account
from eth_keys.datatypes import Signature
from eth_utils import to_checksum_address

from polybot.execution import redeem as R
from polybot.execution.redeem import (
    PolygonRedeemer, RedeemerConfigError, fetch_redeemable,
    redeem_positions_calldata, balance_of_calldata, encode_exec_transaction,
    safe_tx_digest, sign_safe_digest, PUSD, USDC_E, CONDITIONAL_TOKENS,
)

_KEY = "0x" + "11" * 32
_FUNDER = "0x863DB57D4a54fA306091D53B4Fe19f1611221Be8"
_COND = "0xc7cdcc6a6fede727976348abf31216fa2f9280acb1e2c39302debb3713b3d37e"
_TOKEN = 94579776747425357940013902699990033099675531413253579923737606671996949643584


# --- pure encoding ---------------------------------------------------------

def test_redeem_calldata_decodes_to_binary_partition():
    data = redeem_positions_calldata(_COND, PUSD)
    assert data[:4].hex() == "01b7037c"
    coll, parent, cond, index_sets = abi_decode(
        ["address", "bytes32", "bytes32", "uint256[]"], data[4:])
    assert to_checksum_address(coll) == PUSD
    assert parent == b"\x00" * 32
    assert cond.hex() == _COND[2:]
    assert index_sets == (1, 2)


def test_balance_of_calldata_decodes():
    data = balance_of_calldata(_FUNDER, _TOKEN)
    assert data[:4].hex() == "00fdd58e"
    owner, tid = abi_decode(["address", "uint256"], data[4:])
    assert to_checksum_address(owner) == to_checksum_address(_FUNDER)
    assert tid == _TOKEN


def test_exec_transaction_calldata_roundtrip():
    inner = redeem_positions_calldata(_COND, PUSD)
    sig = b"\x01" * 65
    data = encode_exec_transaction(CONDITIONAL_TOKENS, inner, sig)
    assert data[:4].hex() == "6a761202"
    to, value, payload, operation, stg, bg, gp, gtok, refund, signature = abi_decode(
        ["address", "uint256", "bytes", "uint8", "uint256",
         "uint256", "uint256", "address", "address", "bytes"], data[4:])
    assert to_checksum_address(to) == CONDITIONAL_TOKENS
    assert value == 0 and operation == 0 and stg == 0 and bg == 0 and gp == 0
    assert to_checksum_address(gtok) == R._ZERO_ADDR and to_checksum_address(refund) == R._ZERO_ADDR
    assert payload == inner
    assert signature == sig


def test_safe_tx_signature_recovers_to_signer():
    """THE correctness proof: the Safe recovers signatures via ecrecover, so a
    correctly-built digest+signature must recover to the owner EOA."""
    acct = Account.from_key(_KEY)
    inner = redeem_positions_calldata(_COND, PUSD)
    digest = safe_tx_digest(_FUNDER, CONDITIONAL_TOKENS, inner, nonce=5)
    sig = sign_safe_digest(digest, _KEY)
    assert len(sig) == 65
    r = int.from_bytes(sig[0:32], "big")
    s = int.from_bytes(sig[32:64], "big")
    v = sig[64]
    assert v in (27, 28)
    recovered = Signature(vrs=(v - 27, r, s)).recover_public_key_from_msg_hash(digest)
    assert recovered.to_checksum_address().lower() == acct.address.lower()


def test_safe_tx_digest_is_deterministic_and_nonce_sensitive():
    inner = redeem_positions_calldata(_COND, PUSD)
    d1 = safe_tx_digest(_FUNDER, CONDITIONAL_TOKENS, inner, nonce=1)
    d1b = safe_tx_digest(_FUNDER, CONDITIONAL_TOKENS, inner, nonce=1)
    d2 = safe_tx_digest(_FUNDER, CONDITIONAL_TOKENS, inner, nonce=2)
    assert d1 == d1b and d1 != d2 and len(d1) == 32


# --- data-api reporting ----------------------------------------------------

class _FakeResp:
    def __init__(self, payload): self._p = payload
    def json(self): return self._p
    def raise_for_status(self): pass


class _FakeHTTP:
    def __init__(self, payload): self._p = payload
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, url, params=None): return _FakeResp(self._p)


@pytest.mark.asyncio
async def test_fetch_redeemable_classifies_winners_and_losers():
    payload = [
        {"asset": "111", "conditionId": "0xaa", "title": "win mkt", "outcome": "Up",
         "size": 138.0, "currentValue": 138.0, "redeemable": True, "negativeRisk": False},
        {"asset": "222", "conditionId": "0xbb", "title": "lose mkt", "outcome": "Down",
         "size": 25.7, "currentValue": 0.0, "redeemable": True, "negativeRisk": False},
        {"asset": "333", "conditionId": "0xcc", "title": "still open", "outcome": "Up",
         "size": 5.0, "currentValue": 2.5, "redeemable": False, "negativeRisk": False},
    ]
    items = await fetch_redeemable(_FUNDER, http_client_factory=lambda: _FakeHTTP(payload))
    assert len(items) == 2  # the redeemable:false one is filtered out
    winner = next(i for i in items if i.token_id == 111)
    loser = next(i for i in items if i.token_id == 222)
    assert winner.is_winner and winner.value_usd == 138.0
    assert not loser.is_winner and loser.value_usd == 0.0


@pytest.mark.asyncio
async def test_fetch_redeemable_empty_on_error():
    class _Boom:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): raise RuntimeError("network down")
    assert await fetch_redeemable(_FUNDER, http_client_factory=lambda: _Boom()) == []


# --- redeemer config + control flow (mocked chain) -------------------------

def _redeemer():
    return PolygonRedeemer(rpc_url="http://rpc.test", private_key=_KEY, funder=_FUNDER)


def test_config_errors():
    with pytest.raises(RedeemerConfigError):
        PolygonRedeemer(rpc_url="", private_key=_KEY, funder=_FUNDER)
    with pytest.raises(RedeemerConfigError):
        PolygonRedeemer(rpc_url="http://x", private_key="", funder=_FUNDER)
    with pytest.raises(RedeemerConfigError):
        PolygonRedeemer(rpc_url="http://x", private_key=_KEY, funder="")


def _collateral_of(inner_data: bytes) -> str:
    return to_checksum_address(abi_decode(
        ["address", "bytes32", "bytes32", "uint256[]"], inner_data[4:])[0])


def test_redeem_condition_already_cleared_sends_nothing():
    r = _redeemer()
    r.token_balance = lambda tid: 0
    sent = []
    r.send_safe_call = lambda to, data: sent.append(1) or "0xhash"
    res = r.redeem_condition(_COND, _TOKEN, "t")
    assert res.cleared and res.tx_hashes == [] and sent == []


def test_redeem_condition_first_collateral_clears():
    r = _redeemer()
    state = {"bal": 50}
    r.wait_for_receipt = lambda h, **k: True
    def send(to, data):
        if _collateral_of(data) == PUSD:
            state["bal"] = 0
        return "0x" + "ab" * 32
    r.send_safe_call = send
    r.token_balance = lambda tid: state["bal"]
    res = r.redeem_condition(_COND, _TOKEN, "t")
    assert res.cleared and len(res.tx_hashes) == 1


def test_redeem_condition_falls_back_to_second_collateral():
    r = _redeemer()
    state = {"bal": 50}
    r.wait_for_receipt = lambda h, **k: True
    def send(to, data):
        if _collateral_of(data) == USDC_E:  # only USDC.e clears this (legacy) position
            state["bal"] = 0
        return "0x" + "cd" * 32
    r.send_safe_call = send
    r.token_balance = lambda tid: state["bal"]
    res = r.redeem_condition(_COND, _TOKEN, "t")
    assert res.cleared and len(res.tx_hashes) == 2  # pUSD no-op, then USDC.e


def test_redeem_condition_never_clears_reports_error():
    r = _redeemer()
    r.wait_for_receipt = lambda h, **k: True
    r.send_safe_call = lambda to, data: "0x" + "ee" * 32
    r.token_balance = lambda tid: 50  # never drops
    res = r.redeem_condition(_COND, _TOKEN, "t")
    assert not res.cleared and len(res.tx_hashes) == 2 and res.error


def test_eth_call_uint256_raises_on_short_read():
    """A uint256 read is always 32 bytes; an empty/short result must fail loud,
    never coerce to 0 (else a nonce=0 SafeTx, or a held token read as cleared)."""
    r = _redeemer()
    r._eth_call = lambda to, data: b""  # RPC anomaly: empty result
    with pytest.raises(RuntimeError):
        r.token_balance(_TOKEN)
    with pytest.raises(RuntimeError):
        r.safe_nonce()


def test_redeem_condition_bad_balance_read_fails_loud_not_cleared():
    """A failed balance read must NOT be treated as 'already cleared' — it errors
    and sends no tx, so a real winner isn't silently skipped."""
    r = _redeemer()
    def boom(tid): raise RuntimeError("RPC anomaly")
    r.token_balance = boom
    sent = []
    r.send_safe_call = lambda to, data: sent.append(1) or "0xhash"
    res = r.redeem_condition(_COND, _TOKEN, "t")
    assert not res.cleared and "balance check failed" in res.error and sent == []


def test_redeem_condition_bails_when_tx_unconfirmed_no_second_send():
    """If a tx doesn't confirm within the poll window the Safe nonce hasn't
    advanced, so we must NOT fire a second (stale-nonce) tx — bail and retry
    next sweep."""
    r = _redeemer()
    r.token_balance = lambda tid: 50  # still held throughout
    sent = []
    r.send_safe_call = lambda to, data: (sent.append(1), "0x" + "ab" * 32)[1]
    r.wait_for_receipt = lambda h, **k: False  # never confirms
    res = r.redeem_condition(_COND, _TOKEN, "t")
    assert not res.cleared
    assert len(sent) == 1 and len(res.tx_hashes) == 1  # exactly one tx — no stale-nonce resend
    assert "not confirmed" in res.error
