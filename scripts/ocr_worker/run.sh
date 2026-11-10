#!/usr/bin/env bash
# Systemd-run wrapper for the Sietch OCR worker.
#
# HOST-AGNOSTIC BY DESIGN: nothing here names a machine, user or path. The
# worker can move from a laptop to any GPU host by changing environment
# variables (or the env file) — no code or script edit.
#
#   SIETCH_OCR_ENV_FILE        env file with SIETCH_URL + SIETCH_OCR_TOKEN
#                              default: ${XDG_CONFIG_HOME:-$HOME/.config}/sietch-ocr/worker.env
#   SIETCH_OCR_MEMORY_MAX      cgroup MemoryMax (default 12G)
#   SIETCH_OCR_MEMORY_SWAP_MAX cgroup MemorySwapMax (default 0)
#   SIETCH_OCR_WORKDIR         working directory (default: this script's dir)
#   SIETCH_OCR_UNIT            transient unit name (default sietch-ocr-worker)
#   SIETCH_OCR_PYTHON          interpreter (default: <script dir>/.venv/bin/python)
#
# The token lives ONLY in the env file (chmod 600). It is never passed on the
# command line: `ps` must not show it. The env file is handed to systemd via
# EnvironmentFile=, so it stays out of argv too.
#
# Usage:
#   ./run.sh --once
#   ./run.sh --poll-seconds 15
# Stop:  systemctl --user stop "${SIETCH_OCR_UNIT:-sietch-ocr-worker}"
#
# Do NOT `systemctl --user enable` (or `loginctl enable-linger`) without an
# explicit decision by the machine's owner. This wrapper starts an on-demand
# transient unit only.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ENV_FILE="${SIETCH_OCR_ENV_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/sietch-ocr/worker.env}"
MEMORY_MAX="${SIETCH_OCR_MEMORY_MAX:-12G}"
MEMORY_SWAP_MAX="${SIETCH_OCR_MEMORY_SWAP_MAX:-0}"
WORKDIR="${SIETCH_OCR_WORKDIR:-$SCRIPT_DIR}"
UNIT="${SIETCH_OCR_UNIT:-sietch-ocr-worker}"
PYTHON="${SIETCH_OCR_PYTHON:-$SCRIPT_DIR/.venv/bin/python}"

if ! command -v systemd-run >/dev/null 2>&1; then
  echo "error: systemd-run not found (this wrapper targets a systemd user session)" >&2
  exit 2
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "error: interpreter not executable: $PYTHON" >&2
  echo "       create the venv (see docs/OCR_WORKER.md) or set SIETCH_OCR_PYTHON" >&2
  exit 2
fi

if [[ ! -r "$ENV_FILE" ]]; then
  echo "error: env file not readable: $ENV_FILE" >&2
  echo "       copy scripts/ocr_worker/worker.env.example and chmod 600 it" >&2
  exit 2
fi

if [[ ! -d "$WORKDIR" ]]; then
  echo "error: workdir does not exist: $WORKDIR" >&2
  exit 2
fi

# EnvironmentFile= needs an absolute path.
ENV_FILE="$(cd "$(dirname "$ENV_FILE")" && pwd)/$(basename "$ENV_FILE")"

exec systemd-run \
  --user \
  --collect \
  --unit="$UNIT" \
  --working-directory="$WORKDIR" \
  --property=MemoryMax="$MEMORY_MAX" \
  --property=MemorySwapMax="$MEMORY_SWAP_MAX" \
  --property=EnvironmentFile="$ENV_FILE" \
  "$PYTHON" "$SCRIPT_DIR/worker.py" "$@"
