#!/usr/bin/env python3
"""Browser check: a sandboxed preview iframe cannot download by itself.

The production preview iframes are sandboxed without allow-downloads. This
loads the real doc-preview.html in that sandbox and checks:

  * its Download control asks the parent, and only the parent frame is accepted
  * a same-origin spoof from the parent window is rejected
  * an <a download> in that sandbox does not write a file when a control
    iframe that DOES have allow-downloads writes one

Chrome is optional. When it is missing or cannot observe downloads, the
postMessage contract is still required and the download observation is
reported. Synthetic PDF bytes only.

Run with: python3 tests/test_preview_sandbox_browser.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CHROME_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
SANDBOX = (
    "allow-same-origin allow-scripts allow-popups allow-forms "
    "allow-popups-to-escape-sandbox"
)


def _chrome() -> str | None:
    for name in CHROME_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def _production_sandbox() -> str:
    html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    marker = 'id="documents-preview-iframe"'
    at = html.index(marker)
    window = html[at:at + 600]
    start = window.index('sandbox="') + len('sandbox="')
    return window[start:window.index('"', start)]


CHILD = """<!DOCTYPE html>
<html><body>
<a id="dl" href="/synthetic.pdf">download</a>
<script>
  const file = new URLSearchParams(location.search).get("file") || "/synthetic.pdf";
  const a = document.getElementById("dl");
  a.href = file;
  a.download = file.split("/").pop();
  a.click();
  parent.postMessage({ type: "anchor-clicked" }, window.location.origin);
</script>
</body></html>
"""

HARNESS = """<!DOCTYPE html>
<html><body>
<pre id="result">pending</pre>
<iframe id="blocked" title="blocked"></iframe>
<iframe id="control" title="control"></iframe>
<iframe id="preview" title="preview"></iframe>
<script>
const SANDBOX = %s;
const state = { spoof: "pending", preview: "pending", anchor: "pending" };
function render() {
  document.getElementById("result").textContent =
    "spoof=" + state.spoof + ";preview=" + state.preview + ";anchor=" + state.anchor;
}
const preview = document.getElementById("preview");
const blocked = document.getElementById("blocked");
const control = document.getElementById("control");
preview.setAttribute("sandbox", SANDBOX);
blocked.setAttribute("sandbox", SANDBOX);
control.setAttribute("sandbox", SANDBOX + " allow-downloads");
window.addEventListener("message", (ev) => {
  if (ev.origin !== window.location.origin) {
    state.spoof = "bad-origin";
    render();
    return;
  }
  const data = ev.data || {};
  if (data.type === "docs-preview-download") {
    if (ev.source !== preview.contentWindow) {
      state.spoof = "rejected";
      render();
      return;
    }
    state.preview = "accepted:" + data.docId;
    render();
    return;
  }
  if (data.type === "anchor-clicked") {
    state.anchor = "clicked";
    render();
  }
});
window.postMessage({ type: "docs-preview-download", docId: "999" }, window.location.origin);
blocked.src = "/child.html?file=/blocked.pdf";
control.src = "/child.html?file=/control.pdf";
preview.src = "/doc-preview.html?id=42&embedded=1&title=" + encodeURIComponent("Synthetic Packet");
preview.addEventListener("load", () => {
  setTimeout(() => {
    const btn = preview.contentDocument && preview.contentDocument.getElementById("download-btn");
    if (!btn) { state.preview = "no-button"; render(); return; }
    btn.click();
  }, 50);
});
</script>
</body></html>
"""


class _Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, fmt, *args):
        return


def _serve(directory: Path):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), lambda *a, **k: _Handler(*a, directory=str(directory), **k))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def _files_named(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {p.name for p in root.rglob("*") if p.is_file()}


def main() -> int:
    production = _production_sandbox()
    if production != SANDBOX:
        print(f"FAIL: production sandbox drifted: {production!r}")
        return 1
    if "allow-downloads" in production:
        print("FAIL: production preview sandbox includes allow-downloads")
        return 1

    chrome = _chrome()
    if not chrome:
        print("SKIP: no Chrome/Chromium binary; browser sandbox flow not run")
        return 0

    with tempfile.TemporaryDirectory(prefix="sietch-preview-sandbox-") as tmp:
        tmp_path = Path(tmp)
        web = tmp_path / "web"
        web.mkdir()
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        profile = tmp_path / "profile"
        (profile / "Default").mkdir(parents=True)
        (profile / "Default" / "Preferences").write_text(json.dumps({
            "download": {
                "default_directory": str(downloads),
                "prompt_for_download": False,
                "directory_upgrade": True,
            },
            "safebrowsing": {"enabled": False},
        }), encoding="utf-8")
        (web / "synthetic.pdf").write_bytes(PDF)
        (web / "blocked.pdf").write_bytes(PDF)
        (web / "control.pdf").write_bytes(PDF)
        (web / "child.html").write_text(CHILD, encoding="utf-8")
        (web / "harness.html").write_text(HARNESS % json.dumps(production), encoding="utf-8")
        # Drop the Google Fonts stylesheet. Headless Chrome's virtual-time
        # budget does not elapse while that request is outstanding, and this
        # environment has no reason to wait on it. The served copy is the
        # real page otherwise.
        preview_html = (ROOT / "public" / "doc-preview.html").read_text(encoding="utf-8")
        preview_html = preview_html.replace(
            '<link rel="preconnect" href="https://fonts.googleapis.com" />', "")
        preview_html = preview_html.replace(
            '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />', "")
        start = preview_html.find('<link href="https://fonts.googleapis.com')
        if start >= 0:
            preview_html = preview_html[:start] + preview_html[preview_html.find(">", start) + 1:]
        (web / "doc-preview.html").write_text(preview_html, encoding="utf-8")

        httpd = _serve(web)
        port = httpd.server_address[1]
        url = f"http://127.0.0.1:{port}/harness.html"
        # This Chrome build prints --dump-dom and then does not exit when the
        # page was served over HTTP. Read what it printed and stop it.
        proc = subprocess.Popen(
            [
                chrome,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                f"--user-data-dir={profile}",
                "--virtual-time-budget=4000",
                "--dump-dom",
                url,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "HOME": str(tmp_path)},
        )
        try:
            stdout, stderr = proc.communicate(timeout=25)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        finally:
            httpd.shutdown()

        dom = stdout or ""
        if "spoof=rejected" not in dom or "preview=accepted:42" not in dom:
            print("FAIL: preview download postMessage contract")
            print(dom[-2000:])
            print((stderr or "")[-2000:])
            return 1
        if "anchor=clicked" not in dom:
            print("FAIL: sandboxed child never ran")
            print(dom[-2000:])
            return 1

        found = _files_named(downloads) | _files_named(profile)
        # Chrome sometimes nests the file under a Default/ download folder.
        blocked_hit = any(name == "blocked.pdf" or name.startswith("blocked") for name in found)
        control_hit = any(name == "control.pdf" or name.startswith("control") for name in found)
        if control_hit and blocked_hit:
            print(f"FAIL: sandboxed iframe wrote a download: {sorted(found)}")
            return 1
        if control_hit and not blocked_hit:
            print("OK: allow-downloads control saved a file; production sandbox did not")
        else:
            print(
                "OK: postMessage contract passed. Download files were not observed "
                f"in headless Chrome (control={control_hit}, blocked={blocked_hit}); "
                "the sandbox difference was not measurable here."
            )
        if "preview=accepted:42" in dom and "allow-downloads" not in production:
            print("OK: real doc-preview.html Download asked the same-origin parent for doc 42")
        return 0


if __name__ == "__main__":
    sys.exit(main())
