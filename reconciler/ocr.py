"""PaddleOCR (PP-OCRv6) bridge for scanned / obfuscated-text-layer PDFs.

The reconciler itself stays dependency-light: PaddleOCR runs in a **separate
virtualenv** (``~/venvs/paddleocr`` by default, override with
``RECONCILER_OCR_PYTHON``) and is invoked as a subprocess by
:mod:`reconciler.ocr_worker`.  Tesseract is never used - PP-OCRv6 only.

Two pieces live here:

* :func:`ocr_pdf` - run the worker and return the recognised visual rows as
  layout text (the same shape ``pdftotext -layout`` produces), plus the device
  that was used.
* :func:`boxes_to_text` - the deterministic row/column reconstruction.  It is
  pure Python and unit-tested without PaddleOCR installed.

Failure is always clean: a missing venv, a crashed worker or a timeout returns
an :class:`OcrResult` with ``status != "ok"`` and a human-readable ``reason``.
Nothing here raises for a bad PDF.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

__all__ = [
    "OcrResult",
    "ocr_pdf",
    "boxes_to_text",
    "ocr_available",
    "ocr_enabled",
    "ocr_python",
    "OCR_ENGINE",
    "DEFAULT_DPI",
]

OCR_ENGINE = "paddleocr-ppocrv6"
DEFAULT_DPI = 200
DEFAULT_MODEL = "small"
DEFAULT_TIMEOUT = 3600
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_PYTHON = "RECONCILER_OCR_PYTHON"
_ENV_ENABLED = "RECONCILER_OCR"
_ENV_CACHE = "RECONCILER_OCR_CACHE_DIR"

_AVAILABLE_CACHE: Optional[Tuple[bool, str]] = None


@dataclass
class OcrResult:
    status: str  # "ok" | "unavailable" | "error"
    text: str = ""
    reason: str = ""
    device: str = ""
    device_info: Dict = field(default_factory=dict)
    model: str = ""
    engine: str = OCR_ENGINE
    engine_version: str = ""
    dpi: int = DEFAULT_DPI
    pages: int = 0
    elapsed_s: float = 0.0
    page_ms: List[int] = field(default_factory=list)
    cache_hit: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> Dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "device": self.device,
            "device_info": self.device_info,
            "model": self.model,
            "engine": self.engine,
            "engine_version": self.engine_version,
            "dpi": self.dpi,
            "pages": self.pages,
            "elapsed_s": round(self.elapsed_s, 2),
            "page_ms": self.page_ms,
            "cache_hit": self.cache_hit,
        }


# ---------------------------------------------------------------------------
# engine discovery / configuration
# ---------------------------------------------------------------------------
def ocr_python() -> Optional[str]:
    """Path to the PaddleOCR venv's python, or ``None`` when not configured."""
    env = os.environ.get(_ENV_PYTHON)
    if env:
        return env if os.path.exists(env) else None
    for cand in ("~/venvs/paddleocr/bin/python3", "~/venvs/paddleocr/bin/python"):
        path = os.path.expanduser(cand)
        if os.path.exists(path):
            return path
    return None


def ocr_available(refresh: bool = False) -> Tuple[bool, str]:
    """True when the PaddleOCR subprocess can actually import its stack."""
    global _AVAILABLE_CACHE
    if _AVAILABLE_CACHE is not None and not refresh:
        return _AVAILABLE_CACHE
    py = ocr_python()
    if not py:
        _AVAILABLE_CACHE = (
            False,
            f"no PaddleOCR python found (set {_ENV_PYTHON}; expected ~/venvs/paddleocr)",
        )
        return _AVAILABLE_CACHE
    try:
        proc = subprocess.run(
            [py, "-c", "import paddleocr, pypdfium2; print('ok')"],
            capture_output=True,
            timeout=180,
            check=False,
            cwd=_REPO_ROOT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _AVAILABLE_CACHE = (False, f"could not run {py}: {exc}")
        return _AVAILABLE_CACHE
    if proc.returncode != 0 or b"ok" not in (proc.stdout or b""):
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        _AVAILABLE_CACHE = (False, f"PaddleOCR stack not importable in {py}: {err[-300:]}")
        return _AVAILABLE_CACHE
    _AVAILABLE_CACHE = (True, "PaddleOCR PP-OCRv6 available")
    return _AVAILABLE_CACHE


def ocr_enabled(explicit: Optional[bool] = None) -> bool:
    """Whether OCR should be attempted.

    ``explicit`` wins.  Otherwise ``RECONCILER_OCR`` decides; it defaults to
    disabled so the library stays deterministic and fast (the heavy OCR pass is
    an explicit, opt-in step - the caller asks for it).
    """
    if explicit is not None:
        return bool(explicit)
    return os.environ.get(_ENV_ENABLED, "0").strip().lower() in ("1", "true", "yes", "on", "auto")


# ---------------------------------------------------------------------------
# visual row / column reconstruction (pure, unit-tested)
# ---------------------------------------------------------------------------
def _median(values: List[float]) -> float:
    if not values:
        return 1.0
    vals = sorted(values)
    mid = len(vals) // 2
    if len(vals) % 2:
        return float(vals[mid])
    return (float(vals[mid - 1]) + float(vals[mid])) / 2.0


def boxes_to_text(
    pages: Iterable[Dict],
    *,
    gap_ratio: float = 1.1,
    row_overlap: float = 0.45,
) -> str:
    """Rebuild ``pdftotext -layout``-style text from OCR word boxes.

    ``pages`` is an iterable of ``{"page": 1, "boxes": [[x0, y0, x1, y1,
    text, score], ...]}``.  Boxes are clustered into visual rows by vertical
    overlap, then read left to right.  A horizontal gap wider than
    ``gap_ratio`` x the row height becomes multiple spaces, which is what the
    parser's column regexes expect.  Pages are separated by ``\\f``.
    """
    out_pages: List[str] = []
    for page in pages:
        boxes = [b for b in (page.get("boxes") or []) if len(b) >= 5 and str(b[4]).strip()]
        if not boxes:
            out_pages.append("")
            continue
        boxes = sorted(boxes, key=lambda b: (float(b[1]), float(b[0])))
        heights = [max(1.0, float(b[3]) - float(b[1])) for b in boxes]
        unit = _median(heights)
        rows: List[List[List[float]]] = []
        row_bounds: List[Tuple[float, float]] = []
        for b in boxes:
            y0, y1 = float(b[1]), float(b[3])
            placed = False
            for i in range(len(rows) - 1, max(-1, len(rows) - 6), -1):
                ry0, ry1 = row_bounds[i]
                overlap = min(y1, ry1) - max(y0, ry0)
                shorter = max(1.0, min(y1 - y0, ry1 - ry0))
                if overlap / shorter >= row_overlap:
                    rows[i].append(b)
                    row_bounds[i] = (min(ry0, y0), max(ry1, y1))
                    placed = True
                    break
            if not placed:
                rows.append([b])
                row_bounds.append((y0, y1))
        order = sorted(range(len(rows)), key=lambda i: row_bounds[i][0])
        lines: List[str] = []
        for i in order:
            row = sorted(rows[i], key=lambda b: float(b[0]))
            parts: List[str] = []
            prev_x1: Optional[float] = None
            for b in row:
                x0, x1 = float(b[0]), float(b[2])
                text = " ".join(str(b[4]).split())
                if not text:
                    continue
                if prev_x1 is None:
                    parts.append(text)
                else:
                    gap = x0 - prev_x1
                    parts.append(("   " if gap > gap_ratio * unit else " ") + text)
                prev_x1 = x1
            line = "".join(parts).rstrip()
            if line:
                lines.append(line)
        out_pages.append("\n".join(lines))
    return "\f".join(out_pages)


# ---------------------------------------------------------------------------
# cache + subprocess invocation
# ---------------------------------------------------------------------------
def _cache_dir() -> str:
    return os.environ.get(_ENV_CACHE) or os.path.expanduser("~/.cache/sietch-reconciler/ocr")


def _cache_key(path: str, dpi: int, model: str) -> Optional[str]:
    try:
        st = os.stat(path)
        h = hashlib.sha256()
        h.update(os.path.abspath(path).encode("utf-8", errors="replace"))
        h.update(f"{st.st_size}:{st.st_mtime_ns}:{dpi}:{model}".encode("utf-8"))
        return h.hexdigest()[:32]
    except OSError:
        return None


def _read_cache(key: str) -> Optional[Dict]:
    try:
        with open(os.path.join(_cache_dir(), f"{key}.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_cache(key: str, payload: Dict) -> None:
    try:
        d = _cache_dir()
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, f".{key}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, os.path.join(d, f"{key}.json"))
    except OSError:
        pass


def ocr_pdf(
    path: str,
    *,
    dpi: int = DEFAULT_DPI,
    model: str = DEFAULT_MODEL,
    max_pages: int = 0,
    timeout: int = DEFAULT_TIMEOUT,
    use_cache: bool = True,
) -> OcrResult:
    """OCR a PDF with PP-OCRv6 and return layout text + the device used.

    ``max_pages`` > 0 stops after that many pages (a cheap probe).  Results are
    cached on disk by file hash + dpi + model, so repeated runs on the same
    document (tests, re-runs) are free.
    """
    t0 = time.time()
    path = str(path)
    if not os.path.exists(path):
        return OcrResult(status="error", reason=f"file not found: {path}", dpi=dpi, model=model)
    ok, why = ocr_available()
    if not ok:
        return OcrResult(status="unavailable", reason=why, dpi=dpi, model=model)

    key = _cache_key(path, dpi, model) if use_cache else None
    payload = _read_cache(key) if key else None
    cache_hit = payload is not None
    if payload is None:
        py = ocr_python()
        fd, tmp_out = tempfile.mkstemp(prefix="reconciler-ocr-", suffix=".json")
        os.close(fd)
        try:
            proc = subprocess.run(
                [
                    py,
                    "-m",
                    "reconciler.ocr_worker",
                    "--pdf",
                    path,
                    "--out",
                    tmp_out,
                    "--dpi",
                    str(int(dpi)),
                    "--model",
                    model,
                    "--last-page",
                    str(int(max_pages or 0)),
                ],
                capture_output=True,
                timeout=timeout,
                check=False,
                cwd=_REPO_ROOT,
                env={**os.environ, "PYTHONPATH": _REPO_ROOT},
            )
            if proc.returncode != 0:
                err = (proc.stderr or b"").decode("utf-8", errors="replace")[-800:]
                return OcrResult(
                    status="error",
                    reason=f"OCR worker failed (exit {proc.returncode}): {err}",
                    dpi=dpi,
                    model=model,
                )
            try:
                with open(tmp_out, encoding="utf-8") as f:
                    payload = json.load(f)
            except (OSError, ValueError) as exc:
                err = (proc.stderr or b"").decode("utf-8", errors="replace")[-800:]
                return OcrResult(
                    status="error",
                    reason=f"OCR worker produced no JSON ({exc}): {err}",
                    dpi=dpi,
                    model=model,
                )
        except subprocess.TimeoutExpired:
            return OcrResult(
                status="error",
                reason=f"OCR timed out after {timeout}s",
                dpi=dpi,
                model=model,
            )
        finally:
            try:
                os.unlink(tmp_out)
            except OSError:
                pass
        if key and not payload.get("pages"):
            # do not cache an empty result - it is usually a transient failure
            pass
        elif key:
            _write_cache(key, payload)

    pages = payload.get("pages") or []
    text = boxes_to_text(pages)
    if not text.strip():
        return OcrResult(
            status="error",
            reason="OCR produced no text",
            device=payload.get("device", ""),
            device_info=payload.get("device_info") or {},
            model=payload.get("model", model),
            engine_version=payload.get("engine_version") or "",
            dpi=dpi,
            pages=len(pages),
            elapsed_s=time.time() - t0,
            page_ms=[int(p.get("ms") or 0) for p in pages],
            cache_hit=cache_hit,
        )
    return OcrResult(
        status="ok",
        text=text,
        device=payload.get("device", "cpu"),
        device_info=payload.get("device_info") or {},
        model=payload.get("model", model),
        engine_version=payload.get("engine_version") or "",
        dpi=dpi,
        pages=len(pages),
        elapsed_s=time.time() - t0 if not cache_hit else 0.0,
        page_ms=[int(p.get("ms") or 0) for p in pages],
        cache_hit=cache_hit,
    )


def main(argv: Optional[List[str]] = None) -> int:
    """``python3 -m reconciler.ocr FILE.pdf [--dpi N] [--model small]``."""
    import argparse

    ap = argparse.ArgumentParser(description="PP-OCRv6 OCR for the reconciler")
    ap.add_argument("pdf")
    ap.add_argument("--dpi", type=int, default=DEFAULT_DPI)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-pages", type=int, default=0)
    ap.add_argument("--text-out", default="", help="write the layout text here")
    ap.add_argument("--json-out", default="", help="write the run metadata here")
    args = ap.parse_args(argv)

    res = ocr_pdf(args.pdf, dpi=args.dpi, model=args.model, max_pages=args.max_pages)
    print(json.dumps(res.to_dict(), indent=1), file=sys.stderr)
    if not res.ok:
        return 1
    if args.text_out:
        with open(args.text_out, "w", encoding="utf-8") as f:
            f.write(res.text)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(res.to_dict(), f, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
