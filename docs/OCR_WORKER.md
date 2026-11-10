# OCR worker (PaddleOCR PP-StructureV3)

The Sietch server only extracts with `pdftotext`. Scanned pages are detected,
cached OCR text is served when present, and a queue row asks a separate
**worker** to run OCR. This document is the worker setup guide.

**Status (2026-11-10):** the worker is **written but not installed**. Nothing
here has been run on Ken's laptop, no unit is enabled, and no model was
downloaded by this repository's tests.

- Code: `scripts/ocr_worker/` (`worker.py`, `engine.py`, `run.sh`,
  `worker.env.example`)
- Server-side queue/cache: `ocr_store.py`, `migrations/052_ocr_queue_cache.sql`
- Server tests: `tests/test_document_text.py`, `tests/test_ocr_queue_api.py`,
  `tests/test_ocr_worker.py`

---

## 1. Host-agnostic design (important for the model-host move)

The worker is intended to move from a laptop to whichever server later hosts
the local model. **Moving it is a redeploy with a different environment, not a
code change.**

| Concern | Where it comes from |
|---|---|
| API base URL | `SIETCH_URL` |
| API token | `SIETCH_OCR_TOKEN` (env file only) |
| Work/temp path | `SIETCH_OCR_TMPDIR` (default: system temp dir), `SIETCH_OCR_WORKDIR` (run.sh working directory) |
| GPU / device | `SIETCH_OCR_DEVICE` (generic fallback `gpu:0`) |
| PDF render DPI | `SIETCH_OCR_DPI` (default 200) |
| Poll interval | `SIETCH_OCR_POLL_SECONDS` (default 15) |
| Memory limit | `SIETCH_OCR_MEMORY_MAX` (run.sh, generic fallback `12G`) |
| Swap limit | `SIETCH_OCR_MEMORY_SWAP_MAX` (run.sh, generic fallback `0`) |
| Interpreter | `SIETCH_OCR_PYTHON` (default `<script dir>/.venv/bin/python`) |
| Unit name | `SIETCH_OCR_UNIT` (default `sietch-ocr-worker`) |
| Env file path | `SIETCH_OCR_ENV_FILE` (default `${XDG_CONFIG_HOME:-$HOME/.config}/sietch-ocr/worker.env`) |

No table, schema column or Python module records a hostname, machine id,
username or absolute laptop path. `ocr_queue.requested_by` is a CRM
`users.id`, not a machine. The cache is keyed by file SHA-256 + page number,
so the same scan is OCR'd once regardless of where the worker runs.

---

## 2. Prerequisites

- Linux with systemd user sessions (the wrapper uses `systemd-run --user`).
- `poppler-utils` for `pdftoppm` (page images) and `pdftotext`.
- Python 3.10–3.12 and a virtualenv.
- A GPU host with a working driver, **or** set `SIETCH_OCR_DEVICE=cpu` for a
  slow smoke test.
- PaddlePaddle GPU build matching the installed CUDA/driver. Use Paddle's
  official install selector — do not copy a stale wheel URL from here:

  <https://www.paddlepaddle.org.cn/install/quick>

  Verify the GPU is visible before continuing:

  ```bash
  python -c "import paddle; paddle.utils.run_check()"
  ```

## 3. Install (do this on the worker host, not on the CRM server)

```bash
git clone <this repo> sietch-crm          # or copy the checkout
cd sietch-crm
python3 -m venv .venv
. .venv/bin/activate

# 1) PaddlePaddle GPU wheel for YOUR driver/CUDA (official selector above).
python -m pip install --upgrade pip
# e.g. (version/URL depends on the selector output):
# python -m pip install paddlepaddle-gpu

# 2) Pin the OCR stack used by the server cache rows.
python -m pip install -U "paddleocr==3.7.0" "paddlex>=3.7.0,<3.8.0"

# 3) Verify the pinned version (must print 3.7.0).
python -c "import paddleocr; print(paddleocr.__version__)"
```

Why `3.7.0`: it is the latest `paddleocr` on PyPI as of 2026-11-10 and the
first line whose default text models are **PP-OCRv6**. Ken's "v6" refers to
that model family, not a pip version. If PyPI shows a newer release at install
time, re-check the PP-StructureV3 keyword set before bumping the pin, and
update `ENGINE_VERSION` (it is recorded on every cache row).

### 8 GB VRAM (RTX 4060 class)

Official PP-StructureV3 GPU defaults can exceed **16 GB**. `engine.py` uses a
slim configuration: only text + table recognition, with

`use_doc_orientation_classify=False`, `use_doc_unwarping=False`,
`use_textline_orientation=False`, `use_seal_recognition=False`,
`use_formula_recognition=False`, `use_chart_recognition=False`,
`use_table_recognition=True`, `device=<SIETCH_OCR_DEVICE>`.

If the installed PaddleOCR rejects that keyword set, the worker fails with
`ppstructurev3_slim_config_rejected` instead of quietly using the
memory-hungry defaults.

## 4. Create the API token (Admin → API & MCP)

1. Create a token named e.g. `ocr-worker`.
2. Scope: **full** (a read-only token is GET-only and cannot upload pages).
3. Restrictions:
   - methods: `GET`, `POST`
   - resources: **`ocr` only** (`/api/v2/ocr`)
4. Copy the token once. It cannot read `/api/v2/documents`, `/api/v2/mail` or
   any other CRM surface — only queue endpoints and the file fetch scoped to a
   claimed job.

## 5. Configure

```bash
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/sietch-ocr"
cp scripts/ocr_worker/worker.env.example \
   "${XDG_CONFIG_HOME:-$HOME/.config}/sietch-ocr/worker.env"
chmod 600 "${XDG_CONFIG_HOME:-$HOME/.config}/sietch-ocr/worker.env"
$EDITOR "${XDG_CONFIG_HOME:-$HOME/.config}/sietch-ocr/worker.env"
```

Minimal contents:

```dotenv
SIETCH_URL=https://crm.publicadjustermidwest.com
SIETCH_OCR_TOKEN=sietch_…
```

**Example for Ken's laptop (illustration only — not a program default):**

```dotenv
# Host: ken-proartlaptop, user zionad, RTX 4060 8 GB — EXAMPLE VALUES ONLY.
SIETCH_URL=https://crm.publicadjustermidwest.com
SIETCH_OCR_TOKEN=sietch_…
SIETCH_OCR_DEVICE=gpu:0
SIETCH_OCR_TMPDIR=/dev/shm          # tmpfs; size it for the largest scan
SIETCH_OCR_DPI=200
SIETCH_OCR_MEMORY_MAX=12G
SIETCH_OCR_MEMORY_SWAP_MAX=0
```

The token is read from the environment file by `systemd-run`. It is never
passed on the command line (so it cannot appear in `ps`), never printed by the
worker, and never logged.

## 6. Run and stop

```bash
# Process at most one pending job and exit.
scripts/ocr_worker/run.sh --once

# Poll until stopped (foreground; Ctrl+C).
scripts/ocr_worker/run.sh --poll-seconds 15
```

The wrapper starts a transient systemd user unit with:

```
MemoryMax=12G            (SIETCH_OCR_MEMORY_MAX)
MemorySwapMax=0          (SIETCH_OCR_MEMORY_SWAP_MAX)
EnvironmentFile=<env>    (SIETCH_OCR_ENV_FILE)
```

Stop it with:

```bash
systemctl --user stop "${SIETCH_OCR_UNIT:-sietch-ocr-worker}"
```

**Do not `systemctl --user enable` the unit and do not enable lingering**
(`loginctl enable-linger`) until the machine's owner explicitly asks for an
always-on worker. This task installs nothing.

## 7. What a job does

1. `GET /api/v2/ocr/queue?status=pending` (worker token).
2. `POST /api/v2/ocr/queue/{id}/claim` → `in_progress`.
3. `GET /api/v2/ocr/queue/{id}/file` → raw bytes (job-scoped; not a general
   document download).
4. PDF → `pdftoppm -r <dpi> -png` page images.
5. PP-StructureV3 per page → plain text (includes table text/HTML blocks).
6. `POST /api/v2/ocr/queue/{id}/pages` with `engine="pp-structurev3"`,
   `engine_version="paddleocr-3.7.0"`.
7. `POST /api/v2/ocr/queue/{id}/complete` with `done`, or `failed` + a short
   machine error.
8. `finally`: the downloaded file and every rendered page image are deleted
   (the whole job temp directory is removed).

Only ids, SHA-256, page numbers, status and engine name/version are logged.
Document text is never logged.

## 8. Troubleshooting

| Symptom | Check |
|---|---|
| `queue_http_403` | token restrictions: resource `ocr`, methods GET+POST |
| `paddleocr_not_installed` | venv activated / `SIETCH_OCR_PYTHON` points at it |
| `ppstructurev3_slim_config_rejected` | installed paddleocr is not 3.7.0 (or its API changed) |
| `pdftoppm_failed` | `poppler-utils` installed; PDF is valid |
| CUDA OOM on 8 GB | confirm the slim flags; lower `SIETCH_OCR_DPI`; one job at a time |
| Job stuck `in_progress` | it becomes claimable again after 2h (stale window) |
| `not_stored` on fetch | mail attachment bytes were never mirrored; the worker cannot use IMAP |
