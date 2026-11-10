#!/usr/bin/env python3
"""Sietch OCR worker — laptop side of the document-read queue.

Runs PaddleOCR **on Ken's laptop only**. The droplet never runs OCR.

Flow
----
1. Poll ``GET /api/v2/ocr/queue?status=pending`` with a token restricted to the
   ``ocr`` resource.
2. Claim the job, download the file, render PDF pages to PNG with ``pdftoppm``.
3. Run PP-StructureV3 per page on ``gpu:0``.
4. POST the per-page text to ``/api/v2/ocr/queue/{id}/pages`` and mark the job
   done (or failed with a short machine error).
5. Delete the downloaded file and every rendered page image in ``finally``.

Secrets
-------
``SIETCH_URL`` and ``SIETCH_OCR_TOKEN`` come from the environment (normally
``--property=EnvironmentFile=`` in run.sh). The token is never logged, never
printed, never included in a repr or an error message.

Usage
-----
    SIETCH_URL=https://crm.publicadjustermidwest.com \
    SIETCH_OCR_TOKEN=sietch_… \
    python worker.py --once

    python worker.py --poll-seconds 15     # loop until Ctrl+C

This file deliberately imports no ML library at module import time; ``engine``
loads PaddleOCR lazily so the unit tests (and ``--help``) run anywhere.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field

import engine as ocr_engine

logger = logging.getLogger("sietch.ocr_worker")

ENGINE_NAME = "pp-structurev3"
ENGINE_VERSION = "paddleocr-3.7.0"
DEFAULT_POLL_SECONDS = 15
HTTP_TIMEOUT_SECONDS = 120
# OCR output is text; a page bigger than this is a bug, not a document.
MAX_PAGE_TEXT_CHARS = 2_000_000


class ConfigError(RuntimeError):
    """Missing/invalid worker configuration (message never contains secrets)."""


class WorkerError(RuntimeError):
    """A job failed; ``code`` is the short machine error stored on the row."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class Config:
    """Everything host-specific is configuration, never a program default.

    Moving the worker to another machine (or to the server that later hosts
    the local model) is an env change: URL, token, temp dir, device and the
    memory limits all come from the environment. The values below are generic
    fallbacks, not a host identity.
    """

    base_url: str
    token: str = field(repr=False)
    poll_seconds: int = DEFAULT_POLL_SECONDS
    tmpdir: str | None = None
    page_dpi: int = ocr_engine.DEFAULT_DPI
    device: str = ocr_engine.DEFAULT_DEVICE

    def __repr__(self) -> str:  # never leak the token
        return (f"Config(base_url={self.base_url!r}, token=<redacted>, "
                f"poll_seconds={self.poll_seconds}, tmpdir={self.tmpdir!r}, "
                f"page_dpi={self.page_dpi}, device={self.device!r})")


def load_config(env: dict | None = None) -> Config:
    env = env if env is not None else os.environ
    base = (env.get("SIETCH_URL") or "").strip().rstrip("/")
    token = (env.get("SIETCH_OCR_TOKEN") or "").strip()
    if not base:
        raise ConfigError("SIETCH_URL is required (e.g. https://crm.example.com)")
    if not token:
        raise ConfigError("SIETCH_OCR_TOKEN is required (Admin → API & MCP)")
    if not base.startswith(("https://", "http://")):
        raise ConfigError("SIETCH_URL must start with https:// (or http:// for a local test)")
    try:
        poll = max(1, int(env.get("SIETCH_OCR_POLL_SECONDS") or DEFAULT_POLL_SECONDS))
    except ValueError:
        poll = DEFAULT_POLL_SECONDS
    try:
        dpi = max(100, min(400, int(env.get("SIETCH_OCR_DPI") or ocr_engine.DEFAULT_DPI)))
    except ValueError:
        dpi = ocr_engine.DEFAULT_DPI
    tmpdir = (env.get("SIETCH_OCR_TMPDIR") or "").strip() or None
    device = (env.get("SIETCH_OCR_DEVICE") or "").strip() or ocr_engine.DEFAULT_DEVICE
    return Config(base, token, poll, tmpdir, dpi, device)


class SietchClient:
    """Minimal HTTP client for the OCR worker endpoints."""

    def __init__(self, base_url: str, token: str, timeout: float = HTTP_TIMEOUT_SECONDS):
        import httpx

        self._base = base_url.rstrip("/")
        self._token = token  # kept private; never logged or repr'd
        self._client = httpx.Client(timeout=timeout)

    def __repr__(self) -> str:
        return f"SietchClient(base={self._base!r}, token=<redacted>)"

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    def _json(self, response):
        try:
            return response.json()
        except Exception:
            return {}

    def list_pending(self, limit: int = 1) -> list:
        response = self._client.get(
            f"{self._base}/api/v2/ocr/queue",
            params={"status": "pending", "limit": max(1, int(limit))},
            headers=self._headers(),
        )
        if response.status_code != 200:
            raise WorkerError(f"queue_http_{response.status_code}")
        return list(self._json(response).get("jobs") or [])

    def claim(self, job_id: int) -> dict:
        response = self._client.post(
            f"{self._base}/api/v2/ocr/queue/{int(job_id)}/claim",
            headers=self._headers(),
        )
        if response.status_code != 200:
            raise WorkerError(f"claim_http_{response.status_code}")
        return self._json(response).get("job") or {}

    def download(self, job_id: int) -> bytes:
        response = self._client.get(
            f"{self._base}/api/v2/ocr/queue/{int(job_id)}/file",
            headers=self._headers(),
        )
        if response.status_code != 200:
            raise WorkerError(f"file_http_{response.status_code}")
        return response.content

    def upload_pages(self, job_id: int, pages: list, engine: str, engine_version: str) -> None:
        response = self._client.post(
            f"{self._base}/api/v2/ocr/queue/{int(job_id)}/pages",
            headers=self._headers(),
            json={"engine": engine, "engine_version": engine_version, "pages": pages},
        )
        if response.status_code != 200:
            raise WorkerError(f"pages_http_{response.status_code}")

    def complete(self, job_id: int, status: str, error: str | None = None) -> None:
        body = {"status": status}
        if error:
            body["error"] = str(error)[:200]
        response = self._client.post(
            f"{self._base}/api/v2/ocr/queue/{int(job_id)}/complete",
            headers=self._headers(),
            json=body,
        )
        if response.status_code != 200:
            raise WorkerError(f"complete_http_{response.status_code}")


def _valid_pages(pages: list) -> list:
    cleaned = []
    for page in pages or []:
        try:
            number = int(page["page"])
        except (KeyError, TypeError, ValueError):
            continue
        text = page.get("text")
        if number < 1 or not isinstance(text, str):
            continue
        cleaned.append({"page": number, "text": text[:MAX_PAGE_TEXT_CHARS]})
    if not cleaned:
        raise WorkerError("no_pages_extracted")
    cleaned.sort(key=lambda row: row["page"])
    return cleaned


def run_once(client: SietchClient, ocr, *, workdir: str | None = None,
             dpi: int = ocr_engine.DEFAULT_DPI) -> bool:
    """Process at most one pending job. Returns True when a job was handled."""
    jobs = client.list_pending(limit=1)
    if not jobs:
        return False
    job = jobs[0]
    job_id = int(job["id"])
    sha = str(job.get("file_sha256") or "")
    claimed = client.claim(job_id)
    if not claimed:
        logger.warning("job %s could not be claimed", job_id)
        return False

    tmp_root = workdir or tempfile.gettempdir()
    tmpdir = tempfile.mkdtemp(prefix=f"sietch-ocr-{job_id}-", dir=tmp_root)
    try:
        data = client.download(job_id)
        source = os.path.join(tmpdir, "source.pdf")
        with open(source, "wb") as fh:
            fh.write(data)
        pages = _valid_pages(ocr.extract_pages(source, tmpdir, dpi=dpi))
        client.upload_pages(job_id, pages, ENGINE_NAME, ENGINE_VERSION)
        client.complete(job_id, "done")
        # ids/hash/page count only — never text.
        logger.info("job %s done: pages=%d sha=%s",
                    job_id, len(pages), sha[:12])
        return True
    except Exception as exc:
        code = getattr(exc, "code", None) or type(exc).__name__
        logger.warning("job %s failed: %s sha=%s", job_id, code, sha[:12])
        try:
            client.complete(job_id, "failed", error=code)
        except Exception as complete_exc:  # pragma: no cover - server down
            logger.warning("job %s could not be marked failed: %s",
                           job_id, type(complete_exc).__name__)
        return False
    finally:
        # Download + every rendered page image live under tmpdir.
        shutil.rmtree(tmpdir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sietch laptop OCR worker (PaddleOCR PP-StructureV3)")
    parser.add_argument("--once", action="store_true", help="process one job (or none) and exit")
    parser.add_argument("--poll-seconds", type=int, default=None,
                        help="idle sleep between polls (default 15; env SIETCH_OCR_POLL_SECONDS)")
    parser.add_argument("--device", default=None,
                        help="Paddle device, e.g. gpu:0 or cpu (env SIETCH_OCR_DEVICE)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.poll_seconds:
        config.poll_seconds = max(1, args.poll_seconds)
    if args.device:
        config.device = args.device

    logger.info("worker starting: %r", config)
    client = SietchClient(config.base_url, config.token)
    try:
        ocr = ocr_engine.OcrEngine(device=config.device, dpi=config.page_dpi)
        while True:
            handled = run_once(client, ocr, workdir=config.tmpdir, dpi=config.page_dpi)
            if args.once:
                return 0 if handled else 0
            if not handled:
                time.sleep(config.poll_seconds)
    except KeyboardInterrupt:
        logger.info("worker stopped by user")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
