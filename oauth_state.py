"""Signed, account-bound OAuth `state` for the mail connect flows.

WHY THIS MODULE EXISTS (live bug 2026-11-09)
--------------------------------------------
The mail OAuth `state` used to be an opaque random string kept in a
process-local dict (`server.py:_oauth_states`). That entry carried the CRM
user and the provider but **not the mail account**, so the callback had to
*guess* its target: it looked the account up by the email address in the
token response and, when the address did not match an existing row, created
a new one. Consequences seen in production:

  * "Connect" inside account 8's settings wrote the token to whichever row
    matched the Microsoft identity that happened to be signed in (MDM), so
    the Exchange/Graph grant on account 8 was never refreshed;
  * an unreadable identity (`email` == "") produced an INSERT with an empty
    email address instead of an error;
  * a restart/redeploy between "Connect" and the provider callback dropped
    the dict entry, so the flow died with "Invalid or expired state".

The state is now a signed (HMAC-SHA256) compact token that carries the
account it belongs to. It survives a restart, cannot be edited by the
client, expires in 10 minutes, and is bound to one (kind, user, account).

`kind` distinguishes the two flows that share the single registered
redirect URI:

  * "exchange" — IMAP/SMTP XOAUTH2 tokens (`oauth_*` columns)
  * "graph"    — Graph sendMail tokens (`oauth_graph_*` columns)

A Graph state can therefore never be replayed into the Exchange callback
(the old dispatch popped one dict and then the other).

Stdlib only — imported by `server.py`, testable without a database.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time

logger = logging.getLogger("sietch.oauth_state")

STATE_TTL_SECONDS = 600

KIND_EXCHANGE = "exchange"
KIND_GRAPH = "graph"

_SECRET_ENV_VARS = ("OAUTH_STATE_SECRET", "DOCS_JWT_SECRET")
_secret_cache: str | None = None
_secret_lock = threading.Lock()

# Single-use nonces seen by THIS process. Best effort: Microsoft auth codes
# are themselves single-use, so a replay across processes still cannot mint
# a second token — this only stops an accidental double-submit from
# re-running the callback (and re-writing the row) twice.
_used_nonces: dict[str, float] = {}
_nonce_lock = threading.Lock()


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def state_secret() -> str:
    """HMAC key for mail OAuth state.

    `OAUTH_STATE_SECRET` is the dedicated key. Falling back to
    `DOCS_JWT_SECRET` keeps existing deployments working without a new env
    var (it is already a shared, secret, high-entropy value). With neither
    set we mint a random per-process key: state then does not survive a
    restart, which is exactly the old behaviour and never a security
    downgrade.
    """
    global _secret_cache
    if _secret_cache:
        return _secret_cache
    with _secret_lock:
        if _secret_cache:
            return _secret_cache
        for name in _SECRET_ENV_VARS:
            val = (os.environ.get(name) or "").strip()
            if val:
                _secret_cache = val
                return _secret_cache
        _secret_cache = secrets.token_urlsafe(48)
        logger.warning(
            "Neither OAUTH_STATE_SECRET nor DOCS_JWT_SECRET is set — mail OAuth "
            "state is signed with a per-process key and will not survive a restart"
        )
        return _secret_cache


def reset_secret_cache() -> None:
    """Test hook: forget the cached key so an env change takes effect."""
    global _secret_cache
    with _secret_lock:
        _secret_cache = None


def _sign(payload: dict) -> str:
    body = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = hmac.new(state_secret().encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url_encode(sig)}"


def _unsign(state: str) -> dict | None:
    if not isinstance(state, str) or state.count(".") != 1:
        return None
    body, sig_text = state.split(".", 1)
    try:
        sig = _b64url_decode(sig_text)
    except Exception:
        return None
    expected = hmac.new(state_secret().encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_b64url_decode(body).decode("utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def sign_state(kind: str, user_id: int, provider: str,
               account_id: int | None = None, now: float | None = None,
               extra: dict | None = None) -> str:
    """Mint a signed state bound to (kind, user, provider, account).

    `account_id` is REQUIRED for the graph kind and optional for exchange
    (the "add a new mail account" flow has no row yet). `extra` carries
    non-security-critical flow flags (e.g. is_crm_mail) that the callback
    would otherwise have to re-derive.
    """
    if kind not in (KIND_EXCHANGE, KIND_GRAPH):
        raise ValueError(f"unknown oauth state kind: {kind!r}")
    if kind == KIND_GRAPH and account_id is None:
        raise ValueError("graph oauth state must be bound to an account")
    issued = int(now if now is not None else time.time())
    payload = {
        "v": 1,
        "k": kind,
        "u": int(user_id),
        "p": str(provider or ""),
        "a": int(account_id) if account_id is not None else None,
        "n": secrets.token_urlsafe(12),
        "iat": issued,
        "exp": issued + STATE_TTL_SECONDS,
    }
    if extra:
        payload["x"] = {str(k): v for k, v in extra.items() if k not in payload}
    return _sign(payload)


def verify_state(state: str, expected_kind: str | None = None,
                 now: float | None = None) -> dict | None:
    """Return the state payload when it is authentic, current and of the
    expected kind; otherwise None.

    Never raises: a malformed/tampered/expired state is simply invalid.
    """
    payload = _unsign(state)
    if not payload:
        return None
    if payload.get("v") != 1:
        return None
    kind = payload.get("k")
    if kind not in (KIND_EXCHANGE, KIND_GRAPH):
        return None
    if expected_kind and kind != expected_kind:
        logger.warning("OAuth state kind mismatch: got %r, expected %r", kind, expected_kind)
        return None
    try:
        exp = int(payload.get("exp") or 0)
        user_id = int(payload.get("u"))
    except (TypeError, ValueError):
        return None
    if exp <= int(now if now is not None else time.time()):
        return None
    if user_id <= 0:
        return None
    account_id = payload.get("a")
    if account_id is not None:
        try:
            account_id = int(account_id)
        except (TypeError, ValueError):
            return None
        if account_id <= 0:
            return None
        payload["a"] = account_id
    if kind == KIND_GRAPH and payload.get("a") is None:
        return None
    payload["u"] = user_id
    return payload


def consume_state(state: str, expected_kind: str | None = None,
                  now: float | None = None) -> dict | None:
    """verify_state() + single-use enforcement for this process."""
    payload = verify_state(state, expected_kind=expected_kind, now=now)
    if not payload:
        return None
    nonce = str(payload.get("n") or "")
    if not nonce:
        return None
    ts = now if now is not None else time.time()
    with _nonce_lock:
        # Opportunistic sweep keeps the dict bounded without a timer.
        for key in [k for k, exp in _used_nonces.items() if exp <= ts]:
            _used_nonces.pop(key, None)
        if nonce in _used_nonces:
            logger.warning("OAuth state replayed (nonce already used)")
            return None
        _used_nonces[nonce] = ts + STATE_TTL_SECONDS
    return payload


def reset_used_nonces() -> None:
    """Test hook."""
    with _nonce_lock:
        _used_nonces.clear()


__all__ = [
    "KIND_EXCHANGE",
    "KIND_GRAPH",
    "STATE_TTL_SECONDS",
    "consume_state",
    "reset_secret_cache",
    "reset_used_nonces",
    "sign_state",
    "state_secret",
    "verify_state",
]
