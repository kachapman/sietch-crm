#!/usr/bin/env python3
"""PaddleOCR (PP-OCRv6) worker for the reconciler.

This file is **run as a subprocess by the PaddleOCR virtualenv**, never
imported by the reconciler itself: the main app must not depend on
``paddlepaddle`` / ``paddleocr`` (they are heavy and live only in the OCR
venv).  :mod:`reconciler.ocr` invokes it, e.g.::

    ~/venvs/paddleocr/bin/python3 -m reconciler.ocr_worker \
        --pdf scan.pdf --out /tmp/scan.json --dpi 200

It writes one JSON document to ``--out``::

    {"device": "cpu", "device_reason": "...", "providers": [...],
     "model": "PP-OCRv6_small", "pages": [{"page": 1, "boxes": [...],
     "ms": 1234}, ...]}

A box is ``[x0, y0, x1, y1, text, score]`` in rendered-image pixels.  The
parent (``reconciler.ocr``) rebuilds visual text rows from the boxes, so the
row-clustering logic is unit-testable without PaddleOCR installed.

Never uses Tesseract: PP-OCRv6 only (see the plan's Phase 2 decision).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import List, Optional

MODELS = {
    "small": ("PP-OCRv6_small_det", "PP-OCRv6_small_rec"),
    "medium": ("PP-OCRv6_medium_det", "PP-OCRv6_medium_rec"),
}


def device_info() -> dict:
    """Pick the inference device: GPU when genuinely available, else CPU.

    The check is deliberately cheap.  On chapmanserver the iGPU is an AMD
    Vega 8 (gfx902); ROCm does not support it, Paddle has no AMD backend and
    OpenVINO is Intel-only, so the answer is CPU.  We record *why* so the run
    artifacts can show the choice was made, not assumed.
    """
    info = {
        "device": "cpu",
        "device_reason": "",
        "paddle_version": None,
        "compiled_with_cuda": False,
        "compiled_with_rocm": False,
        "cuda_device_count": 0,
        "providers": [],
        "igpu_render_node": os.path.exists("/dev/dri/renderD128"),
    }
    try:
        import paddle  # type: ignore

        info["paddle_version"] = getattr(paddle, "__version__", None)
        info["compiled_with_cuda"] = bool(paddle.device.is_compiled_with_cuda())
        try:
            info["compiled_with_rocm"] = bool(
                getattr(paddle.device, "is_compiled_with_rocm", lambda: False)()
            )
        except Exception:
            pass
        if info["compiled_with_cuda"]:
            try:
                info["cuda_device_count"] = int(paddle.device.cuda.device_count())
            except Exception:
                info["cuda_device_count"] = 0
        info["device"] = str(paddle.device.get_device() or "cpu")
    except Exception as exc:  # pragma: no cover - defensive
        info["device_reason"] = f"paddle unavailable: {exc}"
    try:
        import onnxruntime as ort  # type: ignore

        info["providers"] = list(ort.get_available_providers())
    except Exception:
        info["providers"] = []

    if info["compiled_with_cuda"] and info["cuda_device_count"] > 0:
        info["device"] = "gpu:0"
        info["device_reason"] = "CUDA build with a visible device"
    elif info["compiled_with_rocm"] and info["igpu_render_node"]:
        info["device"] = "cpu"
        info["device_reason"] = "ROCm build detected but no supported AMD GPU"
    else:
        info["device"] = "cpu"
        info["device_reason"] = (
            "no CUDA runtime in the Paddle build; AMD Vega 8 (gfx902) is not "
            "supported by ROCm/Paddle, OpenVINO is Intel-only"
        )
    return info


def _poly_to_box(poly) -> List[float]:
    pts = list(poly)
    xs = [float(p[0]) for p in pts]
    ys = [float(p[1]) for p in pts]
    return [min(xs), min(ys), max(xs), max(ys)]


def _boxes_from_result(res) -> List[List]:
    """Normalise a PaddleOCR result into ``[x0, y0, x1, y1, text, score]``."""
    data = res if isinstance(res, dict) else getattr(res, "json", res)
    if hasattr(data, "json") and not isinstance(data, dict):
        data = data.json
    if isinstance(data, dict) and "res" in data and isinstance(data["res"], dict):
        data = data["res"]
    texts = list(data.get("rec_texts") or [])
    scores = list(data.get("rec_scores") or [])
    polys = data.get("rec_polys")
    rects = data.get("rec_boxes")
    boxes: List[List] = []
    for i, text in enumerate(texts):
        if not str(text).strip():
            continue
        try:
            if polys is not None and len(polys) > i:
                box = _poly_to_box(polys[i])
            elif rects is not None and len(rects) > i:
                r = rects[i]
                box = [float(r[0]), float(r[1]), float(r[2]), float(r[3])]
            else:
                continue
        except Exception:
            continue
        score = float(scores[i]) if i < len(scores) else 0.0
        boxes.append([box[0], box[1], box[2], box[3], str(text), score])
    return boxes


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="PP-OCRv6 worker (PaddleOCR venv only)")
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--first-page", type=int, default=1)
    ap.add_argument("--last-page", type=int, default=0, help="0 = last page")
    ap.add_argument("--model", choices=sorted(MODELS), default="small")
    ap.add_argument(
        "--det-limit",
        type=int,
        default=1600,
        help="max detection side length (bigger = slower, better on dense tables)",
    )
    args = ap.parse_args(argv)

    info = device_info()

    import numpy as np  # type: ignore
    import pypdfium2 as pdfium  # type: ignore
    from paddleocr import PaddleOCR  # type: ignore

    det_name, rec_name = MODELS[args.model]
    t_init = time.time()
    ocr = PaddleOCR(
        text_detection_model_name=det_name,
        text_recognition_model_name=rec_name,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        text_det_limit_side_len=args.det_limit,
        text_det_limit_type="max",
        # PP-OCRv6's oneDNN path raises ConvertPirAttribute2RuntimeAttribute
        # on this CPU build; plain CPU kernels are correct and fast enough.
        enable_mkldnn=False,
        device=info["device"] if info["device"] != "gpu:0" else "gpu",
    )
    init_s = time.time() - t_init

    pdf = pdfium.PdfDocument(args.pdf)
    total = len(pdf)
    last = args.last_page or total
    last = max(args.first_page, min(last, total))

    pages: List[dict] = []
    for pno in range(args.first_page, last + 1):
        t0 = time.time()
        page = pdf[pno - 1]
        bitmap = page.render(scale=args.dpi / 72.0)
        img = np.asarray(bitmap.to_numpy())
        try:
            result = ocr.predict(img)
        except Exception as exc:
            pages.append({"page": pno, "boxes": [], "error": f"{type(exc).__name__}: {exc}"})
            continue
        boxes = _boxes_from_result(result[0]) if result else []
        pages.append(
            {
                "page": pno,
                "boxes": boxes,
                "ms": int((time.time() - t0) * 1000),
            }
        )

    out = {
        "device": info["device"],
        "device_info": info,
        "model": f"PP-OCRv6_{args.model}",
        "dpi": args.dpi,
        "pdf_pages": total,
        "first_page": args.first_page,
        "last_page": last,
        "init_ms": int(init_s * 1000),
        "pages": pages,
        "engine": "paddleocr",
        "engine_version": _engine_version(),
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f)
    print(
        json.dumps(
            {
                "device": info["device"],
                "pages": len(pages),
                "init_ms": out["init_ms"],
                "total_ms": sum(p.get("ms", 0) for p in pages),
                "out": args.out,
            }
        ),
        file=sys.stderr,
    )
    return 0


def _engine_version() -> Optional[str]:
    try:
        import paddleocr  # type: ignore

        return getattr(paddleocr, "__version__", None)
    except Exception:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
