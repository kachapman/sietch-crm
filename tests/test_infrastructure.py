#!/usr/bin/env python3
"""Unit tests for the infrastructure / admin-console code.

Covers the audit-round fixes:

  #1 restart-server flushes the 200 before scheduling os._exit
  #2 _bind_server_with_retry survives the bind race (the critical fix —
     without it a replacement process dies with EADDRINUSE and the
     `restart: on-failure:5` container policy never recovers it)
  #3 _detect_container_runtime replaces the duplicated, buggy cgroup
     checks (one of which called f.read() twice, killing the kubepods branch)
  #4 _disk_usage dedupes by filesystem, not by path string
  #5 _mem_usage falls back to MemFree+Buffers+Cached when MemAvailable
     is absent (pre-3.14 kernels previously reported 100% used)
  #6 _handle_infrastructure probes run concurrently
  #7 _infra_error_stats truncates with '…', not '...'
  #8 docker RestartCount parse is guarded against 'N/A'
  +  the _require_admin gate on /api/v2/admin/infrastructure

`server` imports cleanly (it only binds the socket inside main(), which
the tests never call), so these exercise the real functions rather than
AST-extracted copies.

Run with: python3 tests/test_infrastructure.py
"""

import errno
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

# `import server` eagerly opens the psycopg2 connection pool, so it needs a
# reachable database. Skip cleanly (rather than erroring) when there isn't
# one, so this file is safe on a bare checkout.
try:
    import server  # noqa: E402
    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment dependent
    server = None
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

if server is None:
    # Exit 0 so a checkout without a database doesn't fail a CI run or a
    # developer's first `python3 tests/test_infrastructure.py`.
    print(f"SKIP: server module not importable (no reachable database?): {_IMPORT_ERROR}")
    print("      Run with a live PostgreSQL instance to exercise these tests.")
    sys.exit(0)


# ── infra_log ring buffer ────────────────────────────────────────────────────

class TestInfraLogRingBuffer(unittest.TestCase):
    """infra_log is the shared, bounded event buffer the admin tab reads."""

    def test_append_and_read_back(self):
        import infra_log
        before = len(infra_log.infra_events())
        infra_log.log_infra_event("error", "unit-test event", source="test", path="/x")
        events = infra_log.infra_events()
        self.assertEqual(len(events), before + 1)
        self.assertEqual(events[-1]["msg"], "unit-test event")
        self.assertEqual(events[-1]["level"], "error")
        self.assertEqual(events[-1]["source"], "test")

    def test_event_has_all_documented_keys(self):
        import infra_log
        infra_log.log_infra_event("warn", "shape check", source="test",
                                  method="GET", path="/api", user="a@b.c")
        ev = infra_log.infra_events()[-1]
        for key in ("ts", "level", "msg", "source", "method", "path", "user"):
            self.assertIn(key, ev, f"missing key {key}")

    def test_ts_is_iso_with_timezone(self):
        import infra_log
        infra_log.log_infra_event("info", "ts check")
        ts = infra_log.infra_events()[-1]["ts"]
        parsed = datetime.fromisoformat(ts)
        # Must be timezone-aware or _infra_error_stats silently drops
        # every event via its `except: continue`.
        self.assertIsNotNone(parsed.tzinfo)

    def test_buffer_is_bounded(self):
        import infra_log
        self.assertEqual(infra_log.MAX_EVENTS, 500)
        self.assertLessEqual(len(infra_log.infra_events()), infra_log.MAX_EVENTS)

    def test_uptime_is_monotonic_and_positive(self):
        import infra_log
        up = infra_log.infra_uptime()
        self.assertGreater(up, 0)
        time.sleep(0.01)
        self.assertGreaterEqual(infra_log.infra_uptime(), up)

    def test_infra_events_returns_a_copy(self):
        import infra_log
        snapshot = infra_log.infra_events()
        n = len(snapshot)
        snapshot.clear()
        self.assertEqual(len(infra_log.infra_events()), n,
                         "caller mutation must not affect the ring buffer")


# ── #5 _mem_usage ─────────────────────────────────────────────────────────────

class TestMemUsage(unittest.TestCase):
    """_mem_usage must never report 100% used on a healthy box."""

    MEMINFO = (
        "MemTotal:       16384000 kB\n"
        "MemFree:         1048576 kB\n"
        "MemAvailable:    8192000 kB\n"
        "Buffers:          524288 kB\n"
        "Cached:          2097152 kB\n"
        "SReclaimable:     262144 kB\n"
    )

    def _run_with_meminfo(self, text):
        import io
        real_open = open

        def fake_open(path, *a, **kw):
            if path == "/proc/meminfo":
                return io.StringIO(text)
            return real_open(path, *a, **kw)

        with patch("builtins.open", side_effect=fake_open):
            return server._mem_usage()

    def test_uses_memavailable_when_present(self):
        m = self._run_with_meminfo(self.MEMINFO)
        self.assertEqual(m["total"], 16384000 * 1024)
        self.assertEqual(m["available"], 8192000 * 1024)
        self.assertEqual(m["used"], (16384000 - 8192000) * 1024)

    def test_pre_314_kernel_fallback_does_not_report_full(self):
        """Regression for audit #5: MemAvailable absent → previously 100% used."""
        legacy = (
            "MemTotal:       16384000 kB\n"
            "MemFree:         1048576 kB\n"
            "Buffers:          524288 kB\n"
            "Cached:          2097152 kB\n"
        )
        m = self._run_with_meminfo(legacy)
        self.assertEqual(m["total"], 16384000 * 1024)
        expected_avail = (1048576 + 524288 + 2097152) * 1024
        self.assertEqual(m["available"], expected_avail)
        self.assertNotEqual(m["used"], m["total"],
                            "must not report every byte in use")
        self.assertLess(m["used"], m["total"])
        pct = m["used"] / m["total"] * 100
        self.assertLess(pct, 90, f"implausible usage for a healthy box: {pct:.0f}%")

    def test_available_clamped_to_total(self):
        """SReclaimable can push the fallback above MemTotal; clamp it."""
        weird = (
            "MemTotal:       1000 kB\n"
            "MemFree:          900 kB\n"
            "Buffers:          900 kB\n"
            "Cached:           900 kB\n"
            "SReclaimable:     900 kB\n"
        )
        m = self._run_with_meminfo(weird)
        self.assertEqual(m["available"], m["total"])
        self.assertEqual(m["used"], 0)

    def test_malformed_lines_skipped_not_fatal(self):
        text = ("MemTotal: 16384000 kB\n"
                "GarbageWithoutColon\n"
                "Weird:  notanumber kB\n"
                "MemAvailable: 8192000 kB\n")
        m = self._run_with_meminfo(text)
        self.assertEqual(m["total"], 16384000 * 1024)
        self.assertEqual(m["available"], 8192000 * 1024)

    def test_unreadable_meminfo_returns_empty_dict(self):
        with patch("builtins.open", side_effect=OSError("nope")):
            self.assertEqual(server._mem_usage(), {})

    def test_real_meminfo_is_sane(self):
        """Smoke against the actual /proc/meminfo on this host."""
        m = server._mem_usage()
        if not m:
            self.skipTest("no /proc/meminfo on this platform")
        self.assertGreater(m["total"], 0)
        self.assertLessEqual(m["available"], m["total"])
        self.assertLessEqual(m["used"], m["total"])


# ── #4 _disk_usage ────────────────────────────────────────────────────────────

class TestDiskUsage(unittest.TestCase):
    """_disk_usage must not report the same filesystem several times."""

    def test_dedupes_by_filesystem(self):
        tmp = Path(os.environ.get("TMPDIR", "/tmp")) / "disk_usage_dedupe_test"
        tmp.mkdir(parents=True, exist_ok=True)
        # DATA_DIR and UPLOAD_DIR pointing at the same fs must yield one row.
        with patch.dict(os.environ, {"DATA_DIR": str(tmp), "UPLOAD_DIR": str(tmp)}):
            entries = server._disk_usage()
        paths = [e["path"] for e in entries]
        devs = [os.stat(p).st_dev for p in paths]
        self.assertEqual(len(devs), len(set(devs)),
                         f"duplicate filesystem reported: {list(zip(paths, devs))}")

    def test_entry_shape_and_percentage(self):
        entries = server._disk_usage()
        for e in entries:
            for key in ("path", "total", "used", "free", "percent"):
                self.assertIn(key, e)
            self.assertGreater(e["total"], 0)
            self.assertGreaterEqual(e["percent"], 0)
            self.assertLessEqual(e["percent"], 100)
            self.assertTrue(os.path.isabs(e["path"]),
                            "paths should be absolutised")

    def test_missing_directories_are_skipped(self):
        with patch.dict(os.environ,
                        {"DATA_DIR": "/nonexistent/a", "UPLOAD_DIR": "/nonexistent/b"}):
            entries = server._disk_usage()
        for e in entries:
            self.assertTrue(os.path.isdir(e["path"]))

    def test_disk_usage_failure_yields_empty_list(self):
        with patch("shutil.disk_usage", side_effect=OSError("boom")):
            self.assertEqual(server._disk_usage(), [])


# ── #3 container runtime detection ───────────────────────────────────────────

class TestDetectContainerRuntime(unittest.TestCase):
    """One robust detector replaces two duplicated, partially-dead checks."""

    def test_dockerenv_marker(self):
        with patch("os.path.exists", side_effect=lambda p: p == "/.dockerenv"):
            self.assertEqual(server._detect_container_runtime(), "docker")

    def test_podman_containerenv_marker(self):
        with patch("os.path.exists", side_effect=lambda p: p == "/run/.containerenv"):
            self.assertEqual(server._detect_container_runtime(), "podman")

    def test_kubernetes_env_var(self):
        with patch("os.path.exists", return_value=False):
            with patch.dict(os.environ, {"KUBERNETES_SERVICE_HOST": "10.0.0.1"}):
                self.assertEqual(server._detect_container_runtime(), "kubernetes")

    def test_bare_host_returns_empty(self):
        with patch("os.path.exists", return_value=False):
            with patch.dict(os.environ, {}, clear=True):
                with patch("builtins.open", side_effect=OSError("no cgroup")):
                    self.assertEqual(server._detect_container_runtime(), "")

    def _with_cgroup(self, content):
        """Fake /proc cgroup reads returning `content`; no marker files."""
        import io
        real_open = open

        def fake_open(path, *a, **kw):
            if path in ("/proc/1/cgroup", "/proc/self/cgroup"):
                return io.StringIO(content)
            return real_open(path, *a, **kw)

        with patch("os.path.exists", return_value=False):
            with patch.dict(os.environ, {}, clear=True):
                with patch("builtins.open", side_effect=fake_open):
                    return server._detect_container_runtime()

    def test_cgroup_v1_docker_path(self):
        self.assertEqual(self._with_cgroup("12:memory:/docker/abc123\n"), "docker")

    def test_cgroup_v1_kubepods_path(self):
        """Regression: the old code read f.read() twice so this never matched."""
        self.assertEqual(
            self._with_cgroup("11:cpu:/kubepods/besteffort/podXYZ\n"), "kubernetes")

    def test_cgroup_v1_podman_path(self):
        self.assertEqual(self._with_cgroup("9:memory:/libpod/xyz\n"), "podman")

    def test_empty_cgroup_v2_line_is_not_docker(self):
        # cgroup v2 collapses to "0::/" — that means nothing on its own.
        self.assertEqual(self._with_cgroup("0::/\n"), "")

    def test_is_running_in_docker_wraps_the_detector(self):
        with patch("server._detect_container_runtime", return_value="podman"):
            self.assertTrue(server._is_running_in_docker())
        with patch("server._detect_container_runtime", return_value=""):
            self.assertFalse(server._is_running_in_docker())

    def test_detection_is_consistent_with_itself(self):
        """No self-contradiction between the helper and its wrapper."""
        self.assertEqual(
            server._is_running_in_docker(),
            server._detect_container_runtime() != "")


# ── #2 _bind_server_with_retry (the critical fix) ────────────────────────────

class TestBindServerWithRetry(unittest.TestCase):
    """The restart flow's bind race. Without this the CRM can go down for good."""

    def _free_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def test_binds_immediately_when_port_is_free(self):
        port = self._free_port()
        srv = server._bind_server_with_retry(port, attempts=3, delay=0.1)
        try:
            self.assertIsNotNone(srv)
            self.assertEqual(srv.server_address[1], port)
        finally:
            srv.server_close()

    def test_retries_until_holder_releases(self):
        """The exact scenario: the old process still holds the socket."""
        port = self._free_port()
        holder = socket.socket()
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("0.0.0.0", port))
        holder.listen(1)

        def release():
            time.sleep(1.5)
            holder.close()

        threading.Thread(target=release, daemon=True).start()

        t0 = time.monotonic()
        srv = server._bind_server_with_retry(port, attempts=40, delay=0.25)
        elapsed = time.monotonic() - t0
        try:
            self.assertGreaterEqual(elapsed, 1.0,
                                    "returned before the port was released")
            self.assertEqual(srv.server_address[1], port)
        finally:
            srv.server_close()

    def test_gives_up_after_exhausting_attempts(self):
        """A permanently held port must raise, not hang or silently return None."""
        port = self._free_port()
        holder = socket.socket()
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("0.0.0.0", port))
        holder.listen(1)
        try:
            with self.assertRaises(OSError) as ctx:
                server._bind_server_with_retry(port, attempts=2, delay=0.2)
            self.assertEqual(ctx.exception.errno, errno.EADDRINUSE)
        finally:
            holder.close()

    def test_non_eaddrinuse_error_raises_immediately(self):
        """Permission / address errors must not be retried for 30s."""
        calls = []

        def boom(*a, **kw):
            calls.append(1)
            raise OSError(errno.EACCES, "permission denied")

        with patch("server.ThreadingHTTPServer", side_effect=boom):
            t0 = time.monotonic()
            with self.assertRaises(OSError) as ctx:
                server._bind_server_with_retry(9999, attempts=60, delay=0.5)
            elapsed = time.monotonic() - t0
        self.assertEqual(ctx.exception.errno, errno.EACCES)
        self.assertEqual(len(calls), 1, "EACCES must not be retried")
        self.assertLess(elapsed, 2.0)


# ── #7 + #8 error stats and container parsing ────────────────────────────────

class TestInfraErrorStats(unittest.TestCase):
    """Counts, windows, and the truncation/format details of the top-error list."""

    def setUp(self):
        import infra_log
        self._saved = list(infra_log._infra_log)
        infra_log._infra_log.clear()

    def tearDown(self):
        import infra_log
        infra_log._infra_log.clear()
        infra_log._infra_log.extend(self._saved)

    def _log(self, msg, level="error", age_hours=0):
        import infra_log
        ts = datetime.now(timezone.utc) - timedelta(hours=age_hours)
        infra_log._infra_log.append({
            "ts": ts.isoformat(), "level": level, "msg": msg,
            "source": "test", "method": "", "path": "", "user": "",
        })

    def test_counts_errors_and_warns(self):
        self._log("a", "error")
        self._log("b", "error")
        self._log("c", "warn")
        self._log("d", "info")
        s = server._infra_error_stats()
        self.assertEqual(s["totalErrors"], 2)
        self.assertEqual(s["totalWarns"], 1)

    def test_time_windows(self):
        self._log("recent", "error", age_hours=0.5)
        self._log("yesterday", "error", age_hours=5)
        self._log("lastweek", "error", age_hours=24 * 10)
        s = server._infra_error_stats()
        self.assertEqual(s["last1h"], 1)
        self.assertEqual(s["last24h"], 2)

    def test_top_grouping_and_ordering(self):
        for _ in range(3):
            self._log("Database unreachable")
        self._log("Doc server timeout")
        top = server._infra_error_stats()["top"]
        self.assertEqual(top[0]["msg"], "Database unreachable")
        self.assertEqual(top[0]["count"], 3)
        self.assertEqual(top[1]["count"], 1)

    def test_long_message_truncated_to_120_with_ellipsis(self):
        """Audit #7: uses '…' (the app-wide char) and totals exactly 120 chars."""
        self._log("E" * 400)
        msg = server._infra_error_stats()["top"][0]["msg"]
        self.assertEqual(len(msg), 120)
        self.assertTrue(msg.endswith("…"), f"expected ellipsis, got {msg[-5:]!r}")
        self.assertNotIn("...", msg)

    def test_short_message_untouched(self):
        self._log("short")
        self.assertEqual(server._infra_error_stats()["top"][0]["msg"], "short")

    def test_unparseable_ts_is_skipped_not_fatal(self):
        import infra_log
        infra_log._infra_log.append({
            "ts": "not-a-timestamp", "level": "error", "msg": "m",
            "source": "t", "method": "", "path": "", "user": "",
        })
        s = server._infra_error_stats()
        self.assertEqual(s["totalErrors"], 1)
        self.assertEqual(s["last1h"], 0)

    def test_empty_ring(self):
        s = server._infra_error_stats()
        self.assertEqual(s["totalErrors"], 0)
        self.assertEqual(s["top"], [])


class TestDockerContainerParsing(unittest.TestCase):
    """Audit #8: `docker inspect` can render RestartCount as 'N/A'."""

    def _run(self, stdout, returncode=0):
        """Run _docker_containers with a faked `docker inspect`.

        `stdout` may be a single string (same output for every container) or
        a list of per-container outputs.
        """
        outputs = stdout if isinstance(stdout, list) else [stdout] * 4

        def fake_run(*a, **kw):
            idx = min(fake_run.calls, len(outputs) - 1)
            fake_run.calls += 1
            fake = MagicMock()
            fake.returncode = returncode
            fake.stdout = outputs[idx]
            fake.stderr = ""
            return fake

        fake_run.calls = 0
        with patch("subprocess.run", side_effect=fake_run):
            return server._docker_containers()

    def test_normal_output_parsed(self):
        out = self._run("running 3\n" * 4)
        self.assertEqual(len(out), 4)
        for entry in out:
            self.assertEqual(entry["status"], "running")
            self.assertEqual(entry["restartCount"], 3)

    def test_na_restart_count_does_not_abort_the_list(self):
        """Regression: int('N/A') raised and dropped every container."""
        out = self._run("running N/A\n" * 4)
        self.assertEqual(len(out), 4, "list was aborted by a bad RestartCount")
        for entry in out:
            self.assertEqual(entry["restartCount"], None)

    def test_missing_restart_count_defaults_zero(self):
        """Only the status token is present — no RestartCount field."""
        out = self._run(["exited\n"])
        self.assertEqual(len(out), 4)
        for entry in out:
            self.assertEqual(entry["status"], "exited")
            self.assertEqual(entry["restartCount"], 0)

    def test_mixed_valid_and_na_across_containers(self):
        """A healthy sibling must still be reported when one is N/A."""
        out = self._run(["running 2\n", "running N/A\n", "exited 0\n", "paused 1\n"])
        self.assertEqual(len(out), 4)
        self.assertEqual([e["restartCount"] for e in out], [2, None, 0, 1])
        self.assertEqual([e["status"] for e in out],
                         ["running", "running", "exited", "paused"])

    def test_container_absent_is_skipped(self):
        out = self._run("", returncode=1)
        self.assertEqual(out, [])

    def test_subprocess_exception_skips_that_container(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 5)):
            self.assertEqual(server._docker_containers(), [])


# ── _docker_status payload ───────────────────────────────────────────────────

class TestDockerStatus(unittest.TestCase):

    def test_shape_has_runtime_and_note(self):
        st = server._docker_status()
        for key in ("accessible", "inDocker", "runtime", "containerName",
                    "containers", "note"):
            self.assertIn(key, st)

    def test_runtime_none_on_bare_host(self):
        with patch("server._detect_container_runtime", return_value=""):
            st = server._docker_status()
            self.assertFalse(st["inDocker"])
            self.assertIsNone(st["runtime"])
            self.assertIsNone(st["containerName"])
            self.assertEqual(st["note"], "")

    def test_container_name_set_when_in_container(self):
        with patch("server._detect_container_runtime", return_value="docker"):
            with patch("server.shutil.which", return_value="/usr/bin/docker"):
                with patch("server._docker_containers", return_value=[]):
                    st = server._docker_status()
        self.assertTrue(st["inDocker"])
        self.assertEqual(st["runtime"], "docker")
        self.assertEqual(st["containerName"], socket.gethostname())

    def test_note_names_the_actual_runtime(self):
        for runtime, label in (("kubernetes", "Kubernetes"), ("podman", "Podman")):
            with patch("server._detect_container_runtime", return_value=runtime):
                with patch("server.shutil.which", return_value=None):
                    st = server._docker_status()
            self.assertIn(label, st["note"])
            self.assertTrue(st["inDocker"])

    def test_no_containers_when_cli_missing(self):
        with patch("server._detect_container_runtime", return_value="docker"):
            with patch("server.shutil.which", return_value=None):
                st = server._docker_status()
        self.assertFalse(st["accessible"])
        self.assertEqual(st["containers"], [])


# ── _effective_docs_internal_url ─────────────────────────────────────────────

class TestEffectiveDocsInternalUrl(unittest.TestCase):

    def test_internal_url_wins(self):
        with patch.object(server, "DOCS_INTERNAL_URL", "http://onlyoffice-docserver:80"):
            with patch.object(server, "DOCS_PUBLIC_URL", "https://docs.example.com"):
                self.assertEqual(server._effective_docs_internal_url(),
                                 "http://onlyoffice-docserver:80")

    def test_legacy_placeholder_is_ignored(self):
        """The documented broken placeholder must fall back to the public URL."""
        with patch.object(server, "DOCS_INTERNAL_URL", "http://docserver:8080"):
            with patch.object(server, "DOCS_PUBLIC_URL", "https://docs.example.com"):
                self.assertEqual(server._effective_docs_internal_url(),
                                 "https://docs.example.com")

    def test_placeholder_rejected_on_any_port(self):
        """The guard is a prefix match, so any http://docserver* port is ignored."""
        for bad in ("http://docserver:80", "http://docserver:8080", "http://docserver"):
            with patch.object(server, "DOCS_INTERNAL_URL", bad):
                with patch.object(server, "DOCS_PUBLIC_URL", "https://docs.example.com"):
                    self.assertEqual(server._effective_docs_internal_url(),
                                     "https://docs.example.com", f"not ignored: {bad}")

    def test_real_service_hostname_is_honoured(self):
        """The documented production value must NOT be filtered out."""
        with patch.object(server, "DOCS_INTERNAL_URL", "https://onlyoffice-docserver:443"):
            with patch.object(server, "DOCS_PUBLIC_URL", "https://docs.example.com"):
                self.assertEqual(server._effective_docs_internal_url(),
                                 "https://onlyoffice-docserver:443")

    def test_public_url_fallback_when_internal_empty(self):
        with patch.object(server, "DOCS_INTERNAL_URL", ""):
            with patch.object(server, "DOCS_PUBLIC_URL", "https://docs.example.com"):
                self.assertEqual(server._effective_docs_internal_url(),
                                 "https://docs.example.com")

    def test_both_empty_returns_empty(self):
        with patch.object(server, "DOCS_INTERNAL_URL", ""):
            with patch.object(server, "DOCS_PUBLIC_URL", ""):
                self.assertEqual(server._effective_docs_internal_url(), "")


# ── source-level guards for the handler changes ──────────────────────────────

class TestHandlerSourceGuards(unittest.TestCase):
    """Structural assertions on server.py for changes that need a live
    socket/admin session to exercise end-to-end."""

    @classmethod
    def setUpClass(cls):
        cls.src = Path(server.__file__).read_text(encoding="utf-8")

    def _handler_body(self, name):
        """Extract a method/function body from the real source by name."""
        start = self.src.index(f"def {name}")
        nxt = self.src.find("\n    def ", start + 1)
        nxt2 = self.src.find("\ndef ", start + 1)
        ends = [e for e in (nxt, nxt2) if e != -1]
        return self.src[start:min(ends) if ends else len(self.src)]

    def test_infrastructure_handler_is_admin_gated(self):
        """The authz fix: _require_admin must precede every probe."""
        body = self._handler_body("_handle_infrastructure")
        self.assertIn("_require_admin(self)", body)
        self.assertLess(body.index("_require_admin(self)"),
                        body.index("db.query"),
                        "authz check must run before the DB probe")
        self.assertLess(body.index("_require_admin(self)"),
                        body.index("_probe_docserver"),
                        "authz check must run before the docserver probe")
        self.assertLess(body.index("_require_admin(self)"),
                        body.index("_docker_status"),
                        "authz check must run before the docker probe")

    def test_infrastructure_probes_run_concurrently(self):
        body = self._handler_body("_handle_infrastructure")
        self.assertIn("ThreadPoolExecutor", body,
                      "probes should run in a thread pool")
        self.assertGreaterEqual(body.count("pool.submit("), 3,
                                "db, docker and resources should all be submitted")

    def _section_after(self, anchor, length=2600):
        """Grab `length` chars of source starting at `anchor`.

        do_POST is thousands of lines with nested defs, so extracting it by
        function boundary is unreliable — anchor directly on the route.
        """
        start = self.src.index(anchor)
        return self.src[start:start + length]

    def test_restart_server_flushes_before_scheduling_exit(self):
        section = self._section_after('"/api/v2/admin/restart-server"')
        self.assertIn("wfile.flush()", section,
                      "the 200 must be flushed before os._exit is scheduled")
        self.assertLess(section.index("wfile.flush()"),
                        section.index("threading.Timer"),
                        "flush must happen before the exit timer is armed")

    def test_restart_server_stays_alive_if_spawn_fails(self):
        section = self._section_after('"/api/v2/admin/restart-server"')
        spawn = section.index("subprocess.Popen")
        guard = section.index("return", section.index("except", spawn))
        exit_call = section.index("os._exit(0)", spawn)
        self.assertLess(guard, exit_call,
                        "a failed spawn must return before os._exit is reached")

    def test_restart_server_logs_the_replacement_pid(self):
        section = self._section_after('"/api/v2/admin/restart-server"')
        self.assertIn("proc.pid", section,
                      "the replacement pid should be recorded in the infra log")

    def test_main_uses_bind_retry(self):
        self.assertIn("server = _bind_server_with_retry(PORT)", self.src)
        self.assertNotIn('server = ThreadingHTTPServer(("0.0.0.0", PORT)',
                         self.src,
                         "main() must not bind directly without retry")

    def test_no_duplicate_double_read_cgroup_check(self):
        """The old _is_running_in_docker called f.read() twice in one with-block."""
        self.assertNotIn('return "docker" in f.read() or "kubepods" in f.read()',
                         self.src)

    def test_docker_health_handler_uses_shared_detector(self):
        body = self._handler_body("_handle_docker_health")
        self.assertIn("_detect_container_runtime()", body)
        self.assertNotIn("/proc/1/cgroup", body,
                         "detection should be delegated to the shared helper")


# ── app.js structural guards ─────────────────────────────────────────────────

class TestInfraFrontendSource(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.src = (HERE.parent / "public" / "app.js").read_text(encoding="utf-8")

    def _fn(self, name, length=1200):
        start = self.src.index(f"function {name}")
        return self.src[start:start + length]

    def test_polling_survives_missing_checkbox(self):
        body = self._fn("startInfraPolling")
        self.assertIn("if (!auto)", body,
                      "a missing checkbox must be handled explicitly")
        self.assertIn("console.warn", body)
        # And the fallback path must still start the interval.
        warn_at = body.index("console.warn")
        interval_at = body.index("setInterval", warn_at)
        self.assertLess(warn_at, interval_at,
                        "the fallback must start polling, not just warn")

    def test_polling_uses_a_single_timer(self):
        body = self._fn("startInfraPolling", 1600)
        self.assertEqual(body.count("setInterval("), 2,
                         "one unconditional fallback + one checkbox-gated timer")
        self.assertIn("stopInfraPolling();", body,
                      "must clear any existing timer first")


if __name__ == "__main__":
    unittest.main(verbosity=2)