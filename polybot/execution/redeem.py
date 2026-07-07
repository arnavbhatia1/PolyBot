"""On-chain redemption of Polymarket conditional tokens through the funder Safe.

Polymarket BTC Up/Down markets are standard binary CTF markets. When one
resolves, the winning share is worth $1 and the losing share $0 — but neither
leaves the wallet until it is *redeemed*. Polymarket's auto-redeem claims
winners; losing shares have no payout so nothing burns them, and they linger
on-chain as $0 dust. This module redeems **every** resolved position — winner
(collateral credited to USDC) or loser (burned to $0) — so no shares sit around.

The funder (POLY_GNOSIS_SAFE) is a 1-of-1 Gnosis Safe v1.3.0 that *holds* the
tokens, so a redeem must run through the Safe: build ``redeemPositions``
calldata, wrap it in ``Safe.execTransaction``, sign the SafeTx with the owner
EOA, and broadcast the outer tx to a Polygon RPC (the EOA pays ~$0.001 gas).

Redemption cannot lose money: ``redeemPositions`` only burns *your* tokens and
pays *you* the resolved payout. A malformed tx reverts (a no-op costing dust
gas); it can never mis-send funds. Pure encoding/signing are module functions
(no network, fully unit-tested); :class:`PolygonRedeemer` adds JSON-RPC I/O.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
from eth_abi import encode as _abi_encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

logger = logging.getLogger(__name__)

# --- Polygon (chain 137) contracts -----------------------------------------
CHAIN_ID = 137
# Gnosis Conditional Tokens contract every Polymarket market settles against
# (matches py_clob_client_v2.config for chain 137).
CONDITIONAL_TOKENS = to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
# The collateral a market was created with must be passed to redeemPositions, or
# it computes a positionId you don't hold and burns nothing (a silent no-op).
# Polymarket migrated collateral to pUSD; older markets used USDC.e. We try each
# and confirm on-chain that the tokens actually cleared, so vintage never matters.
PUSD = to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
USDC_E = to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
COLLATERAL_CANDIDATES: tuple[str, ...] = (PUSD, USDC_E)

_ZERO_ADDR = "0x0000000000000000000000000000000000000000"
_ZERO_BYTES32 = b"\x00" * 32
# A binary market's two outcome slots are index sets 0b01 and 0b10. Redeeming
# both burns whichever side is held and credits the winning slot's payout, so a
# single call cleans the position regardless of which side we hold.
_BINARY_INDEX_SETS = [1, 2]

# Gnosis Safe v1.3.0 EIP-712 typehashes (the v1.3.0 domain has NO name/version).
_SAFE_DOMAIN_TYPEHASH = keccak(text="EIP712Domain(uint256 chainId,address verifyingContract)")
_SAFE_TX_TYPEHASH = keccak(
    text="SafeTx(address to,uint256 value,bytes data,uint8 operation,uint256 safeTxGas,"
         "uint256 baseGas,uint256 gasPrice,address gasToken,address refundReceiver,uint256 nonce)"
)

_SEL_REDEEM = keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
_SEL_BALANCE_OF = keccak(text="balanceOf(address,uint256)")[:4]
_SEL_NONCE = keccak(text="nonce()")[:4]
_SEL_EXEC_TX = keccak(
    text="execTransaction(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,bytes)"
)[:4]

POSITIONS_API_URL = "https://data-api.polymarket.com/positions"
_DUST_SHARES = 0.01  # ignore sub-cent share balances when confirming a burn


# ---------------------------------------------------------------------------
# Pure encoding / signing (no network — unit-tested against ecrecover)
# ---------------------------------------------------------------------------

def _bytes32(hexstr: str) -> bytes:
    """Parse a 0x-prefixed 32-byte hex value (e.g. a conditionId) to raw bytes."""
    raw = bytes.fromhex(hexstr[2:] if hexstr.startswith("0x") else hexstr)
    if len(raw) != 32:
        raise ValueError(f"expected a 32-byte hex value, got {len(raw)} bytes: {hexstr!r}")
    return raw


def redeem_positions_calldata(condition_id: str, collateral: str) -> bytes:
    """``redeemPositions(collateral, 0x0, conditionId, [1,2])`` calldata."""
    args = _abi_encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [to_checksum_address(collateral), _ZERO_BYTES32,
         _bytes32(condition_id), _BINARY_INDEX_SETS],
    )
    return _SEL_REDEEM + args


def balance_of_calldata(owner: str, token_id: int) -> bytes:
    """ERC-1155 ``balanceOf(owner, tokenId)`` calldata (confirms a burn cleared)."""
    return _SEL_BALANCE_OF + _abi_encode(
        ["address", "uint256"], [to_checksum_address(owner), int(token_id)])


def safe_nonce_calldata() -> bytes:
    """Gnosis Safe ``nonce()`` calldata."""
    return _SEL_NONCE


def safe_tx_digest(safe: str, to: str, data: bytes, nonce: int,
                   chain_id: int = CHAIN_ID) -> bytes:
    """EIP-712 SafeTx digest for a plain CALL with all gas fields zeroed.

    value=0, operation=0 (CALL), safeTxGas=baseGas=gasPrice=0,
    gasToken=refundReceiver=0x0. This is the exact 32-byte hash the Safe owner
    signs; verified by ecrecover in the test suite.
    """
    domain_sep = keccak(_abi_encode(
        ["bytes32", "uint256", "address"],
        [_SAFE_DOMAIN_TYPEHASH, chain_id, to_checksum_address(safe)]))
    struct_hash = keccak(_abi_encode(
        ["bytes32", "address", "uint256", "bytes32", "uint8",
         "uint256", "uint256", "uint256", "address", "address", "uint256"],
        [_SAFE_TX_TYPEHASH, to_checksum_address(to), 0, keccak(data), 0,
         0, 0, 0, _ZERO_ADDR, _ZERO_ADDR, int(nonce)]))
    return keccak(b"\x19\x01" + domain_sep + struct_hash)


def sign_safe_digest(digest: bytes, private_key: str) -> bytes:
    """65-byte ``r‖s‖v`` owner signature over a SafeTx digest (v as-is, 27/28)."""
    sig = Account.unsafe_sign_hash(digest, private_key)
    return sig.r.to_bytes(32, "big") + sig.s.to_bytes(32, "big") + bytes([sig.v])


def encode_exec_transaction(to: str, data: bytes, signature: bytes) -> bytes:
    """``Safe.execTransaction`` calldata wrapping ``data`` as a CALL to ``to``."""
    args = _abi_encode(
        ["address", "uint256", "bytes", "uint8", "uint256",
         "uint256", "uint256", "address", "address", "bytes"],
        [to_checksum_address(to), 0, data, 0, 0,
         0, 0, _ZERO_ADDR, _ZERO_ADDR, signature],
    )
    return _SEL_EXEC_TX + args


# ---------------------------------------------------------------------------
# Redeemable-position reporting (no keys/RPC — pure data-api read)
# ---------------------------------------------------------------------------

@dataclass
class Redeemable:
    token_id: int
    condition_id: str
    title: str
    outcome: str
    shares: float
    value_usd: float          # currentValue: >0 = a WINNER (real money), ~0 = $0 loser
    negative_risk: bool

    @property
    def is_winner(self) -> bool:
        return self.value_usd > 0.01


async def fetch_redeemable(funder: str,
                           http_client_factory: Callable[[], Any] = None) -> list[Redeemable]:
    """Resolved, still-held positions from the public data API (redeemable=true).

    Read-only and unauthenticated. Returns winners and $0 losers alike; callers
    decide what to do with each. Empty list on any failure (never raises).
    """
    factory = http_client_factory or (lambda: httpx.AsyncClient(timeout=20.0))
    try:
        async with factory() as http:
            resp = await http.get(POSITIONS_API_URL, params={
                "user": funder, "sizeThreshold": _DUST_SHARES,
                "redeemable": "true", "limit": 500})
            resp.raise_for_status()
            rows = resp.json()
    except Exception as e:
        logger.warning("Redeemable-positions fetch failed: %s", e)
        return []
    out: list[Redeemable] = []
    for p in rows if isinstance(rows, list) else []:
        if not isinstance(p, dict) or not p.get("redeemable"):
            continue
        try:
            out.append(Redeemable(
                token_id=int(str(p.get("asset"))),
                condition_id=str(p.get("conditionId") or ""),
                title=str(p.get("title") or "")[:80],
                outcome=str(p.get("outcome") or ""),
                shares=float(p.get("size") or 0.0),
                value_usd=float(p.get("currentValue") or 0.0),
                negative_risk=bool(p.get("negativeRisk")),
            ))
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------
# On-chain redeemer (JSON-RPC over httpx; no web3 dependency)
# ---------------------------------------------------------------------------

@dataclass
class RedeemResult:
    condition_id: str
    title: str
    cleared: bool
    tx_hashes: list[str] = field(default_factory=list)
    error: str = ""


class RedeemerConfigError(RuntimeError):
    """Missing/invalid RPC url, private key, or funder — raised at construction."""


class PolygonRedeemer:
    """Redeems Polymarket positions by executing ``redeemPositions`` through the
    funder Gnosis Safe. Sequential (waits for each receipt) so Safe/EOA nonces
    never race. Sends are gated behind an explicit method call — constructing
    one performs no I/O.
    """

    def __init__(self, rpc_url: str, private_key: str, funder: str,
                 http_client: httpx.Client | None = None) -> None:
        if not rpc_url:
            raise RedeemerConfigError("POLYGON_RPC_URL not set")
        if not private_key:
            raise RedeemerConfigError("POLYMARKET_PRIVATE_KEY not set")
        if not funder:
            raise RedeemerConfigError("POLYMARKET_FUNDER not set")
        self.rpc_url = rpc_url
        self._acct = Account.from_key(private_key)
        self.eoa = self._acct.address
        self.funder = to_checksum_address(funder)
        self._http = http_client or httpx.Client(timeout=30.0)
        self._rpc_id = 0

    # -- JSON-RPC ---------------------------------------------------------
    def _rpc(self, method: str, params: list) -> Any:
        self._rpc_id += 1
        resp = self._http.post(self.rpc_url, json={
            "jsonrpc": "2.0", "id": self._rpc_id, "method": method, "params": params})
        resp.raise_for_status()
        body = resp.json()
        if body.get("error"):
            raise RuntimeError(f"RPC {method} error: {body['error']}")
        return body.get("result")

    def _eth_call(self, to: str, data: bytes) -> bytes:
        result = self._rpc("eth_call", [
            {"to": to_checksum_address(to), "data": "0x" + data.hex()}, "latest"])
        return bytes.fromhex(result[2:]) if isinstance(result, str) and result.startswith("0x") else b""

    def _eth_call_uint256(self, to: str, data: bytes) -> int:
        """eth_call returning a single uint256. A valid read is ALWAYS exactly 32
        bytes (a true zero is 32 zero bytes), so a short/empty result is an RPC
        anomaly we must fail on — never coerce it to 0. Silently reading 0 would
        sign a nonce=0 SafeTx, or mark a still-held token as 'already cleared' and
        skip redeeming a real winner."""
        raw = self._eth_call(to, data)
        if len(raw) != 32:
            raise RuntimeError(
                f"eth_call to {to} returned {len(raw)} bytes, expected a 32-byte "
                "uint256 (RPC anomaly / wrong chain?)")
        return int.from_bytes(raw, "big")

    def safe_nonce(self) -> int:
        return self._eth_call_uint256(self.funder, safe_nonce_calldata())

    def token_balance(self, token_id: int) -> int:
        return self._eth_call_uint256(CONDITIONAL_TOKENS, balance_of_calldata(self.funder, token_id))

    def _eoa_nonce(self) -> int:
        return int(self._rpc("eth_getTransactionCount", [self.eoa, "pending"]), 16)

    def _gas_fees(self) -> tuple[int, int]:
        """(maxFeePerGas, maxPriorityFeePerGas) in wei. Polygon enforces a ~25-30
        gwei priority floor; we sit above it and cap the total generously — a
        redeem is ~150k gas, so even at 200 gwei it costs a fraction of a cent."""
        try:
            gas_price = int(self._rpc("eth_gasPrice", []), 16)
        except Exception:
            gas_price = 100_000_000_000  # 100 gwei fallback
        priority = 30_000_000_000  # 30 gwei — Polygon's effective minimum
        max_fee = max(gas_price * 2, 60_000_000_000) + priority
        return max_fee, priority

    def _estimate_gas(self, to: str, data: bytes) -> int:
        try:
            est = int(self._rpc("eth_estimateGas", [{
                "from": self.eoa, "to": to_checksum_address(to),
                "data": "0x" + data.hex()}]), 16)
            return int(est * 1.3)
        except Exception:
            return 400_000

    def send_safe_call(self, inner_to: str, inner_data: bytes) -> str:
        """Sign+broadcast a Safe.execTransaction wrapping a CALL to ``inner_to``.
        Returns the outer tx hash. Fetches fresh Safe+EOA nonces (safe because
        callers await each receipt before the next send)."""
        digest = safe_tx_digest(self.funder, inner_to, inner_data, self.safe_nonce())
        signature = sign_safe_digest(digest, self._acct.key)
        exec_data = encode_exec_transaction(inner_to, inner_data, signature)
        max_fee, priority = self._gas_fees()
        tx = {
            "chainId": CHAIN_ID,
            "nonce": self._eoa_nonce(),
            "to": self.funder,
            "value": 0,
            "data": "0x" + exec_data.hex(),
            "gas": self._estimate_gas(self.funder, exec_data),
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority,
            "type": 2,
        }
        signed = self._acct.sign_transaction(tx)
        return self._rpc("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])

    def wait_for_receipt(self, tx_hash: str, timeout_s: float = 120.0,
                         sleep: Callable[[float], None] | None = None) -> bool:
        """Poll for the receipt; True iff mined with status 1 (success)."""
        import time
        _sleep = sleep or time.sleep
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
            if receipt:
                return int(receipt.get("status", "0x0"), 16) == 1
            _sleep(3.0)
        return False

    def redeem_condition(self, condition_id: str, token_id: int, title: str = "") -> RedeemResult:
        """Redeem one resolved binary market so the held tokens leave the wallet.

        Tries each candidate collateral (pUSD, then USDC.e) and confirms success
        by the on-chain token balance dropping to ~0 — so a wrong-collateral
        no-op is caught and retried with the other, and success means the tokens
        are provably gone. Idempotent: an already-cleared position returns
        cleared immediately without sending a tx.
        """
        res = RedeemResult(condition_id=condition_id, title=title, cleared=False)
        if not condition_id:
            res.error = "missing conditionId"
            return res
        try:
            if self.token_balance(token_id) <= 0:
                res.cleared = True  # already redeemed/burned
                return res
        except Exception as e:
            res.error = f"balance check failed: {e}"
            return res
        for collateral in COLLATERAL_CANDIDATES:
            try:
                tx_hash = self.send_safe_call(
                    CONDITIONAL_TOKENS, redeem_positions_calldata(condition_id, collateral))
                res.tx_hashes.append(tx_hash)
                if not self.wait_for_receipt(tx_hash):
                    # Not mined within the poll window (or reverted): the Safe
                    # nonce has NOT advanced, so trying the next collateral would
                    # reuse it and revert. Bail — the next idempotent sweep
                    # re-reads the balance and either finds it cleared or retries.
                    res.error = "redeem tx not confirmed within the poll window (retries next sweep)"
                    return res
                if self.token_balance(token_id) <= 0:
                    res.cleared = True
                    return res
                # Mined but tokens remain → wrong collateral (a harmless no-op that
                # DID advance the Safe nonce); try the other candidate.
            except Exception as e:
                res.error = str(e).split("\n")[0][:160]
        if not res.cleared and not res.error:
            res.error = "tokens still held after redeem (unexpected collateral?)"
        return res
