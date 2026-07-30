"""Simple TCP forwarder: listens on port 6777, forwards to 127.0.0.1:9443.
Used to bridge localtonet's docserver TCP tunnel (configured to hit port 6777)
to the OnlyOffice Document Server on port 9443.

Kill with: pkill -f tcp-forwarder.py
"""
import socket
import threading
import sys
import os

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 6777
TARGET_HOST = "127.0.0.1"
TARGET_PORT = 9443


def forward(src, dst):
    try:
        while True:
            data = src.recv(4096)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        try:
            src.close()
        except Exception:
            pass
        try:
            dst.close()
        except Exception:
            pass


def handle(conn, addr):
    try:
        target = socket.create_connection((TARGET_HOST, TARGET_PORT), timeout=10)
        threading.Thread(target=forward, args=(conn, target), daemon=True).start()
        threading.Thread(target=forward, args=(target, conn), daemon=True).start()
    except Exception as e:
        print(f"[forwarder] Failed: {e}", flush=True)
        try:
            conn.close()
        except Exception:
            pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_HOST, LISTEN_PORT))
    srv.listen(128)
    print(
        f"[forwarder] Listening on {LISTEN_HOST}:{LISTEN_PORT} -> {TARGET_HOST}:{TARGET_PORT}",
        flush=True,
    )
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handle, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    main()
