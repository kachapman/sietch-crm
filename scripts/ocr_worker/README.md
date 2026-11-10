# Sietch OCR worker (host-agnostic)

Runs PaddleOCR **PP-StructureV3** (`paddleocr==3.7.0`) on files that the Sietch
server could not read with `pdftotext` (scans). The server never runs OCR.

Everything host-specific — URL, token, temp dir, device, memory limits — is
configuration. Moving the worker to another machine is an env change, not a
code change.

```bash
# 1) install (see the full guide)
python3 -m venv .venv && . .venv/bin/activate
python -m pip install paddlepaddle-gpu            # use Paddle's official selector
python -m pip install -U "paddleocr==3.7.0" "paddlex>=3.7.0,<3.8.0"

# 2) configure a 600-mode env file (never commit it)
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/sietch-ocr"
cp scripts/ocr_worker/worker.env.example \
   "${XDG_CONFIG_HOME:-$HOME/.config}/sietch-ocr/worker.env"
chmod 600 "${XDG_CONFIG_HOME:-$HOME/.config}/sietch-ocr/worker.env"
$EDITOR "${XDG_CONFIG_HOME:-$HOME/.config}/sietch-ocr/worker.env"

# 3) run (transient systemd unit; MemoryMax=12G, MemorySwapMax=0)
scripts/ocr_worker/run.sh --once
scripts/ocr_worker/run.sh --poll-seconds 15

# 4) stop
systemctl --user stop "${SIETCH_OCR_UNIT:-sietch-ocr-worker}"
```

**Do not** enable a lingering unit or install services until the machine's
owner says so. This directory is written, not installed.

Full setup, token scope, 8 GB VRAM notes and troubleshooting:
[`docs/OCR_WORKER.md`](../../docs/OCR_WORKER.md).
