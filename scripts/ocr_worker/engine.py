#!/usr/bin/env python3
"""PP-StructureV3 OCR engine wrapper (lazy import; laptop GPU only).

Pinned stack (2026-11-10):
    paddleocr==3.7.0        # PP-OCRv6 text models are the default here
    paddlex>=3.7.0,<3.8.0   # PP-StructureV3 pipeline
    paddlepaddle-gpu        # install per Paddle's selector for your CUDA

Ken's "v6" is the PP-OCRv6 *model family*, not a pip package version — the pip
package is 3.7.0.

8 GB VRAM (RTX 4060): the official PP-StructureV3 GPU defaults can exceed
16 GB. This wrapper disables seal recognition, formula recognition, chart
recognition, document unwarping, document orientation classification and
text-line orientation classification, keeping only text + tables. If the
installed paddleocr rejects that keyword set, we fail loudly instead of
silently falling back to the memory-hungry defaults.

PDFs are rendered to PNG with ``pdftoppm`` (poppler, same package as
pdftotext) at 200 dpi, one image per page, so page numbers map 1:1.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from glob import glob

logger = logging.getLogger("sietch.ocr_engine")

DEFAULT_DPI = 200
DEFAULT_DEVICE = "gpu:0"
PAGE_RENDER_TIMEOUT = 900
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}

# Keep this in lockstep with ocr_store.ENGINE_NAME / ENGINE_VERSION and with
# worker.ENGINE_* — the cache row records exactly what produced the text.
ENGINE_NAME = "pp-structurev3"
ENGINE_VERSION = "paddleocr-3.7.0"

_PAGE_FILE_RE = re.compile(r"-(\d+)\.png$")


class EngineError(RuntimeError):
    """OCR failed; ``code`` is a short machine code safe to store/log."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def load_pipeline(device: str = "gpu:0"):
    """Build the slim PP-StructureV3 pipeline (imports paddleocr lazily)."""
    try:
        from paddleocr import PPStructureV3  # noqa: PLC0415 (deliberate lazy import)
    except Exception as exc:  # ImportError or a broken CUDA build
        raise EngineError("paddleocr_not_installed") from exc

    slim_kwargs = dict(
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        use_seal_recognition=False,
        use_formula_recognition=False,
        use_chart_recognition=False,
        use_table_recognition=True,
        device=device,
    )
    try:
        return PPStructureV3(**slim_kwargs)
    except TypeError as exc:
        raise EngineError(
            "ppstructurev3_slim_config_rejected: this worker requires "
            "paddleocr==3.7.0 with the slim 8 GB keyword set (see docs/OCR_WORKER.md)"
        ) from exc


def _mapping(result) -> dict:
    """Best-effort dict view of a PaddleOCR result object."""
    if isinstance(result, dict):
        return result
    for attribute in ("json", "res"):
        value = getattr(result, attribute, None)
        if isinstance(value, dict):
            return value
        if callable(value):
            try:
                value = value()
            except Exception:
                continue
            if isinstance(value, dict):
                return value
            if isinstance(value, str):
                try:
                    import json

                    parsed = json.loads(value)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    continue
    return {}


def result_text(result) -> str:
    """Plain text (including table text/HTML) from one PP-StructureV3 result."""
    data = _mapping(result)

    markdown = getattr(result, "markdown", None)
    if markdown is None:
        markdown = data.get("markdown")
    if isinstance(markdown, dict):
        markdown = markdown.get("markdown") or markdown.get("text")
    if isinstance(markdown, str) and markdown.strip():
        return markdown.strip()

    blocks = (data.get("parsing_res_list")
              or data.get("parsing_result_list")
              or data.get("layout_parsing_result")
              or [])
    parts = []
    for block in blocks if isinstance(blocks, list) else []:
        if isinstance(block, dict):
            content = (block.get("block_content") or block.get("content")
                       or block.get("text"))
        else:
            content = (getattr(block, "block_content", None)
                       or getattr(block, "content", None)
                       or getattr(block, "text", None))
        if isinstance(content, str) and content.strip():
            parts.append(content.strip())
    if parts:
        return "\n".join(parts)

    texts = data.get("rec_texts") or []
    if isinstance(texts, str):
        return texts
    return "\n".join(str(t) for t in texts if t)


class OcrEngine:
    """Renders PDF pages and runs PP-StructureV3 on each one."""

    def __init__(self, device: str = DEFAULT_DEVICE, dpi: int = DEFAULT_DPI):
        self.device = device
        self.dpi = dpi
        self._pipeline = None

    @property
    def pipeline(self):
        if self._pipeline is None:
            self._pipeline = load_pipeline(self.device)
        return self._pipeline

    # ── page preparation ───────────────────────────────────────────────
    def _render_pdf(self, path: str, workdir: str, dpi: int) -> list[str]:
        prefix = os.path.join(workdir, "page")
        try:
            proc = subprocess.run(
                ["pdftoppm", "-r", str(int(dpi)), "-png", path, prefix],
                capture_output=True,
                timeout=PAGE_RENDER_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise EngineError("pdftoppm_failed")
        if proc.returncode != 0:
            raise EngineError("pdftoppm_failed")
        files = sorted(
            glob(prefix + "-*.png"),
            key=lambda p: int((_PAGE_FILE_RE.search(p) or [0, "0"])[1]),
        )
        if not files:
            raise EngineError("pdf_has_no_pages")
        return files

    def _page_images(self, path: str, workdir: str, dpi: int) -> tuple[list[str], bool]:
        suffix = os.path.splitext(path)[1].lower()
        if suffix == ".pdf":
            return self._render_pdf(path, workdir, dpi), True
        if suffix in IMAGE_SUFFIXES:
            return [path], False
        # Only PDFs/images can need OCR; the server never queues anything else.
        raise EngineError("unsupported_for_ocr")

    # ── main entry point ───────────────────────────────────────────────
    def extract_pages(self, path: str, workdir: str, dpi: int | None = None) -> list[dict]:
        dpi = int(dpi or self.dpi)
        images, rendered = self._page_images(path, workdir, dpi)
        pages: list[dict] = []
        try:
            # Resolve the pipeline before the loop so a missing/broken
            # paddleocr surfaces its own code, not "predict_failed_…".
            pipeline = self.pipeline
            for index, image in enumerate(images, start=1):
                try:
                    results = pipeline.predict(input=image)
                except Exception as exc:
                    raise EngineError(f"predict_failed_{type(exc).__name__}") from exc
                if isinstance(results, dict):
                    results = [results]
                text = "\n\n".join(
                    part for part in (result_text(r) for r in (results or [])) if part
                )
                pages.append({"page": index, "text": text})
        finally:
            if rendered:
                for image in images:
                    try:
                        os.unlink(image)
                    except OSError:
                        pass
        return pages


def main(argv: list[str] | None = None) -> int:
    """Tiny diagnostic: OCR one local file to stdout (no server, no queue)."""
    import argparse

    parser = argparse.ArgumentParser(description="Run PP-StructureV3 on a local PDF/image")
    parser.add_argument("path")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI)
    parser.add_argument("--device", default=DEFAULT_DEVICE)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        import tempfile

        engine = OcrEngine(device=args.device, dpi=args.dpi)
        with tempfile.TemporaryDirectory(prefix="sietch-ocr-selftest-") as tmp:
            for page in engine.extract_pages(args.path, tmp, dpi=args.dpi):
                print(f"--- page {page['page']} ---")
                print(page["text"])
    except EngineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
