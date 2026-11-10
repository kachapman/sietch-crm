"""Fail any non-localhost network connection in tests.

Ken (2026-11-10): every test must be isolated from outside sources. Import and
call `install()` (autouse via conftest for pytest; the unittest suites call it
at module import) and any attempt to reach a real host raises immediately.
Localhost/unix sockets stay allowed for the test database/app.
"""

from __future__ import annotations

import socket

_ALLOWED = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}
_installed = False
_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex


class NetworkBlocked(RuntimeError):
    """Raised when a test tries to reach a non-local host."""


def _check(sock, address):
    if sock.family == socket.AF_UNIX:
        return
    host = ""
    if isinstance(address, tuple) and address:
        host = str(address[0])
    if host in _ALLOWED or host.startswith("127.") or host.startswith("::1"):
        return
    raise NetworkBlocked(f"blocked outbound connection to {host!r} (test isolation)")


def install() -> None:
    global _installed
    if _installed:
        return

    def guarded_connect(self, address):
        _check(self, address)
        return _original_connect(self, address)

    def guarded_connect_ex(self, address):
        _check(self, address)
        return _original_connect_ex(self, address)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    _installed = True


def uninstall() -> None:
    global _installed
    socket.socket.connect = _original_connect
    socket.socket.connect_ex = _original_connect_ex
    _installed = False


__all__ = ["NetworkBlocked", "install", "uninstall"]
