#!/usr/bin/env python3
"""End-to-end admin API-token UI contract against a THROWAWAY local server.

What this proves (real HTTP, real PostgreSQL, synthetic fixtures only):
  * anonymous requests are 401; non-admin sessions are 403 on list, create
    and revoke, and never receive token data;
  * create validates the name, refuses unknown scopes, and returns the
    plaintext exactly once, with only its SHA-256 stored;
  * the list endpoint returns safe metadata only — the secret never appears
    in a later response, including a second "revisit" request;
  * revoke flips the metadata, is idempotent (404 on repeat), and the list is
    re-fetchable with accurate state;
  * a minted token can read broad CRM data but is refused on admin routes.

Safety rails (the suite SKIPS unless all hold):
  * SIETCH_TEST_DB_NAME must start with "sietch_test_" and must not be any
    known real database name;
  * the DB host must be loopback;
  * the suite connects only to that database, starts its own server on an
    ephemeral port, never touches an already-running service, and never
    prints a token secret (failure messages are redacted).

Run (after starting a disposable PostgreSQL, e.g. a --rm container):
  SIETCH_TEST_DB_HOST=127.0.0.1 SIETCH_TEST_DB_PORT=<port> \
  SIETCH_TEST_DB_NAME=sietch_test_tokens SIETCH_TEST_DB_USER=postgres \
  SIETCH_TEST_DB_PASSWORD=<synthetic> \
  python3 tests/test_admin_api_tokens_http.py -v
"""

import hashlib
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # allow "import auth" in this process

SESSION_COOKIE = "sietch_session"
DENY_DB_NAMES = {"sietch_crm", "postgres", "template0", "template1", "sietch"}
SECRET_RE = re.compile(r"sietch_[A-Za-z0-9_\-]{8,}")


def _redact(text: str) -> str:
    return SECRET_RE.sub("sietch_[REDACTED]", str(text))[:600]


def _test_db_config():
    name = os.environ.get("SIETCH_TEST_DB_NAME", "").strip()
    host = os.environ.get("SIETCH_TEST_DB_HOST", "").strip()
    if not name:
        return None
    if name in DENY_DB_NAMES or not name.startswith("sietch_test_"):
        return None
    if host not in ("127.0.0.1", "localhost", "::1"):
        return None
    try:
        port = int(os.environ.get("SIETCH_TEST_DB_PORT", "5432"))
    except ValueError:
        return None
    return {
        "host": host,
        "port": port,
        "name": name,
        "user": os.environ.get("SIETCH_TEST_DB_USER", "postgres"),
        "password": os.environ.get("SIETCH_TEST_DB_PASSWORD", ""),
    }


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


class AdminApiTokenHttpTests(unittest.TestCase):
    proc = None
    conn = None
    log_path = None
    base = ""
    admin_cookie = ""
    member_cookie = ""
    admin_id = None
    member_id = None

    # ── lifecycle ────────────────────────────────────────────────────────
    @classmethod
    def setUpClass(cls):
        cls.db_cfg = _test_db_config()
        if not cls.db_cfg:
            raise unittest.SkipTest(
                "SIETCH_TEST_DB_* not set to a throwaway sietch_test_* database on loopback"
            )
        import psycopg2  # imported lazily so the skip is cheap

        cls.psycopg2 = psycopg2
        cls.conn = psycopg2.connect(
            host=cls.db_cfg["host"], port=cls.db_cfg["port"],
            dbname=cls.db_cfg["name"], user=cls.db_cfg["user"],
            password=cls.db_cfg["password"], connect_timeout=10,
        )
        cls.conn.autocommit = True
        with cls.conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.users')")
            if cur.fetchone()[0] is None:
                cur.execute((ROOT / "init.sql").read_text(encoding="utf-8"))

        cls.port = _free_port()
        cls.base = "http://127.0.0.1:%d" % cls.port
        env = dict(os.environ)
        env.update({
            "DB_HOST": cls.db_cfg["host"],
            "DB_PORT": str(cls.db_cfg["port"]),
            "DB_NAME": cls.db_cfg["name"],
            "DB_USER": cls.db_cfg["user"],
            "DB_PASSWORD": cls.db_cfg["password"],
            "PORT": str(cls.port),
            "COOKIE_SECURE": "false",
            "COOKIE_SECRET": secrets.token_urlsafe(32),
            "SCANNER_ENABLED": "false",
            "BACKUP_ENABLED": "false",
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_NOTIFY_ENABLED": "false",
            "ONLYOFFICE_PORTAL_URL": "",
            "CRM_PUBLIC_URL": cls.base,
            "DOCS_PUBLIC_URL": "",
            "DOCS_INTERNAL_URL": "",
            "PYTHONUNBUFFERED": "1",
        })
        fd, cls.log_path = tempfile.mkstemp(prefix="sietch-token-ui-test-", suffix=".log")
        cls._log = os.fdopen(fd, "w")
        cls.proc = subprocess.Popen(
            [sys.executable, "server.py"],
            cwd=str(ROOT), env=env,
            stdout=cls._log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        cls._wait_ready()

        tag = secrets.token_hex(4)
        cls.admin_id, cls.admin_cookie = cls._make_user(
            "admin+%s@synthetic.test" % tag, is_admin=True, role="admin"
        )
        cls.member_id, cls.member_cookie = cls._make_user(
            "member+%s@synthetic.test" % tag, is_admin=False, role="member"
        )

    @classmethod
    def _wait_ready(cls):
        deadline = time.time() + 90
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                raise AssertionError(
                    "server exited during startup; log tail:\n" + cls._log_tail()
                )
            try:
                with urllib.request.urlopen(cls.base + "/api/config", timeout=2) as resp:
                    if resp.status == 200:
                        return
            except Exception:
                time.sleep(0.5)
        raise AssertionError("server did not become ready; log tail:\n" + cls._log_tail())

    @classmethod
    def _log_tail(cls, lines=12):
        try:
            with open(cls.log_path, "r", encoding="utf-8", errors="replace") as fh:
                return _redact("\n".join(fh.read().splitlines()[-lines:]))
        except Exception:
            return "(no log)"

    @classmethod
    def _make_user(cls, email, is_admin, role):
        import auth  # imports db only; no connection is opened here

        pw_hash, pw_salt = auth.hash_password("synthetic-test-password")
        cookie = secrets.token_urlsafe(32)
        with cls.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO users
                       (email, password_hash, password_salt, display_name, first_name,
                        last_name, is_admin, is_active, role, must_change_password,
                        twofa_method)
                   VALUES (%s, %s, %s, %s, 'Syn', 'Thetic', %s, TRUE, %s, FALSE, NULL)
                   RETURNING id""",
                (email, pw_hash, pw_salt, email.split("@")[0], is_admin, role),
            )
            uid = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO sessions (token, user_id, expires_at) VALUES (%s, %s, to_timestamp(%s))",
                (cookie, uid, int(time.time()) + 3600),
            )
        return uid, cookie

    @classmethod
    def tearDownClass(cls):
        if cls.proc and cls.proc.poll() is None:
            try:
                os.killpg(os.getpgid(cls.proc.pid), signal.SIGTERM)
            except Exception:
                cls.proc.terminate()
            try:
                cls.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(cls.proc.pid), signal.SIGKILL)
                except Exception:
                    cls.proc.kill()
                cls.proc.wait(timeout=10)
        try:
            cls._log.close()
        except Exception:
            pass
        if cls.log_path and os.path.exists(cls.log_path):
            try:
                os.unlink(cls.log_path)
            except OSError:
                pass
        if cls.conn is not None:
            cls.conn.close()

    # ── HTTP helpers ─────────────────────────────────────────────────────
    def _req(self, method, path, cookie=None, body=None, bearer=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if cookie:
            req.add_header("Cookie", "%s=%s" % (SESSION_COOKIE, cookie))
        if bearer:
            req.add_header("Authorization", "Bearer " + bearer)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def _admin(self, method, path, body=None):
        return self._req(method, path, cookie=self.admin_cookie, body=body)

    def _member(self, method, path, body=None):
        return self._req(method, path, cookie=self.member_cookie, body=body)

    def _json(self, text):
        try:
            return json.loads(text)
        except Exception:
            self.fail("expected JSON, got: " + _redact(text))

    def _create_token(self, name="synthetic fixture token"):
        status, text = self._admin("POST", "/api/v2/admin/api-tokens", {"name": name})
        self.assertEqual(status, 201, "create failed: " + _redact(text))
        payload = self._json(text)
        return payload

    def _count_rows(self, sql, params=()):
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()[0]

    # ── tests ────────────────────────────────────────────────────────────
    def test_01_anonymous_access_is_rejected(self):
        status, text = self._req("GET", "/api/v2/admin/api-tokens")
        self.assertEqual(status, 401, _redact(text))
        self.assertNotIn("tokens", text)
        status, text = self._req("POST", "/api/v2/admin/api-tokens", body={"name": "x"})
        self.assertEqual(status, 401, _redact(text))
        status, text = self._req("POST", "/api/v2/admin/api-tokens/1/revoke")
        self.assertEqual(status, 401, _redact(text))

    def test_02_non_admin_is_forbidden_and_gets_no_data(self):
        admin_token = self._create_token("synthetic member-denial fixture")
        secret = admin_token["token"]
        tid = admin_token["id"]
        try:
            status, text = self._member("GET", "/api/v2/admin/api-tokens")
            self.assertEqual(status, 403, _redact(text))
            self.assertNotIn("tokens", text)
            self.assertNotIn(secret, text)

            status, text = self._member("POST", "/api/v2/admin/api-tokens",
                                        {"name": "member should not create"})
            self.assertEqual(status, 403, _redact(text))
            self.assertNotIn(secret, text)
            self.assertEqual(
                self._count_rows("SELECT COUNT(*) FROM api_tokens WHERE name = %s",
                                 ("member should not create",)),
                0,
            )

            status, text = self._member("POST", "/api/v2/admin/api-tokens/%d/revoke" % tid)
            self.assertEqual(status, 403, _redact(text))
            self.assertEqual(
                self._count_rows("SELECT COUNT(*) FROM api_tokens WHERE id = %s AND revoked_at IS NULL", (tid,)),
                1,
                "a non-admin revoke must not take effect",
            )
        finally:
            self._admin("POST", "/api/v2/admin/api-tokens/%d/revoke" % tid)

    def test_03_create_validation_errors(self):
        before = self._count_rows("SELECT COUNT(*) FROM api_tokens")
        for body in ({"name": ""}, {"name": "   "}, {"name": "\t\n"}, {}, {"name": None}):
            status, text = self._admin("POST", "/api/v2/admin/api-tokens", body)
            self.assertEqual(status, 400, "expected 400 for %r: %s" % (body, _redact(text)))
            payload = self._json(text)
            self.assertIn("error", payload)
            self.assertNotIn("token", payload)
        status, text = self._admin("POST", "/api/v2/admin/api-tokens",
                                   {"name": "synthetic widen attempt", "scope": "write"})
        self.assertEqual(status, 400, _redact(text))
        self.assertNotIn("token", self._json(text))
        self.assertEqual(self._count_rows("SELECT COUNT(*) FROM api_tokens"), before,
                         "rejected requests must not mint tokens")

    def test_04_create_shows_secret_once_and_list_stays_metadata_only(self):
        name = "synthetic nightly archive"
        created = self._create_token(name)
        secret = created["token"]
        tid = created["id"]
        try:
            self.assertTrue(secret.startswith("sietch_"))
            self.assertGreaterEqual(len(secret), 40)
            self.assertEqual(created["scope"], "read")
            self.assertEqual(created["name"], name)
            self.assertEqual(len(created["token_prefix"]), 12)
            self.assertTrue(secret.startswith(created["token_prefix"]))
            self.assertIn("only once", created.get("warning", ""))

            # Only a SHA-256 hash (never the plaintext) is stored.
            with self.conn.cursor() as cur:
                cur.execute("SELECT * FROM api_tokens WHERE id = %s", (tid,))
                cols = [d[0] for d in cur.description]
                row = cur.fetchone()
                values = [str(v) for v in row]
            self.assertIn("token_hash", cols)
            self.assertNotIn("token", cols)
            self.assertNotIn(secret, values)
            stored_hash = row[cols.index("token_hash")]
            self.assertEqual(stored_hash, hashlib.sha256(secret.encode()).hexdigest())

            # First list call: safe metadata only.
            status, text = self._admin("GET", "/api/v2/admin/api-tokens")
            self.assertEqual(status, 200, _redact(text))
            self.assertNotIn(secret, text)
            self.assertNotIn("token_hash", text)
            payload = self._json(text)
            row_json = next(t for t in payload["tokens"] if t["id"] == tid)
            self.assertEqual(set(row_json), {
                "id", "name", "token_prefix", "scope", "created_by",
                "created_by_email", "created_at", "last_used_at", "revoked_at", "revoked",
            })
            self.assertEqual(row_json["name"], name)
            self.assertIsNone(row_json["revoked_at"])
            self.assertFalse(row_json["revoked"])

            # "Revisit"/refresh: a second fetch must not resurrect the secret.
            status2, text2 = self._admin("GET", "/api/v2/admin/api-tokens")
            self.assertEqual(status2, 200)
            self.assertNotIn(secret, text2)
            self.assertEqual(text2, text, "list metadata should be stable between visits")
        finally:
            self._admin("POST", "/api/v2/admin/api-tokens/%d/revoke" % tid)

    def test_05_revoke_updates_metadata_and_is_idempotent(self):
        created = self._create_token("synthetic revoke fixture")
        tid = created["id"]
        secret = created["token"]

        status, text = self._admin("POST", "/api/v2/admin/api-tokens/%d/revoke" % tid)
        self.assertEqual(status, 200, _redact(text))
        self.assertEqual(self._json(text), {"ok": True})

        status, text = self._admin("GET", "/api/v2/admin/api-tokens")
        self.assertEqual(status, 200)
        self.assertNotIn(secret, text)
        row = next(t for t in self._json(text)["tokens"] if t["id"] == tid)
        self.assertTrue(row["revoked"])
        self.assertIsNotNone(row["revoked_at"])

        status, text = self._admin("POST", "/api/v2/admin/api-tokens/%d/revoke" % tid)
        self.assertEqual(status, 404, _redact(text))

        status, text = self._admin("POST", "/api/v2/admin/api-tokens/999999/revoke")
        self.assertEqual(status, 404, _redact(text))

    def test_06_bearer_reads_data_but_cannot_manage_tokens(self):
        created = self._create_token("synthetic broad-read fixture")
        secret = created["token"]
        tid = created["id"]
        try:
            status, text = self._req("GET", "/api/v2/projects", bearer=secret)
            self.assertEqual(status, 200, _redact(text))
            status, text = self._req("GET", "/api/v2/contacts", bearer=secret)
            self.assertEqual(status, 200, _redact(text))

            status, text = self._req("GET", "/api/v2/admin/api-tokens", bearer=secret)
            self.assertEqual(status, 403, _redact(text))
            self.assertNotIn('"tokens":', text, "no list payload may be returned")
            self.assertNotIn(secret, text)

            status, text = self._req("POST", "/api/v2/admin/api-tokens",
                                     bearer=secret, body={"name": "bearer escalation"})
            self.assertEqual(status, 403, _redact(text))
            self.assertEqual(
                self._count_rows("SELECT COUNT(*) FROM api_tokens WHERE name = %s",
                                 ("bearer escalation",)),
                0,
            )

            # last_used_at becomes visible as safe metadata after a read.
            status, text = self._admin("GET", "/api/v2/admin/api-tokens")
            row = next(t for t in self._json(text)["tokens"] if t["id"] == tid)
            self.assertIsNotNone(row["last_used_at"])
        finally:
            self._admin("POST", "/api/v2/admin/api-tokens/%d/revoke" % tid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
