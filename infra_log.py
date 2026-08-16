"""Rolling in-memory infrastructure event ring buffer.

Shared across server.py and background modules (mail scanner, dispatcher) so
every subsystem can report errors to the admin Infrastructure tab without
import cycles. Bounded to the most recent events; oldest are auto-evicted.
"""

from __future__ import annotations

import collections
import time
from datetime import datetime, timezone

MAX_EVENTS = 500

_infra_log: collections.deque = collections.deque(maxlen=MAX_EVENTS)
_infra_start_time = time.time()
_last_docserver_ok: bool | None = None


def log_infra_event(level: str, msg: str, source: str = "app", method: str = "", path: str = "", user: str = "") -> None:
    """Append an event to the infrastructure ring buffer."""
    _infra_log.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "msg": msg,
        "source": source,
        "method": method,
        "path": path,
        "user": user,
    })


def infra_events() -> list[dict]:
    """Return a snapshot of the ring buffer (newest last)."""
    return list(_infra_log)


def infra_uptime() -> float:
    return time.time() - _infra_start_time


def infra_start_time() -> float:
    return _infra_start_time
