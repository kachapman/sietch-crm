#!/bin/bash
# proton-drive-sync.sh — wrapper around `proton-drive-cli sync` with
# safer flags + post-sync failure handling for the Sietch backup
# pipeline. Invoked by the proton-drive-sietch-sync.service systemd
# unit. Non-interactive: the CLI must already be logged in (see the
# runbook for the one-time interactive login).
#
# Exit codes:
#   0  — sync completed (or no changes to push)
#   1  — sync errored (handled by the .service unit for retry/notify)
#   2  — misconfiguration (missing env vars, missing CLI, etc.)
set -euo pipefail

: "${PROTON_DRIVE_SRC:?PROTON_DRIVE_SRC is required (set in /etc/proton-drive/sietch-sync.env)}"
: "${PROTON_DRIVE_DST:?PROTON_DRIVE_DST is required (Proton Drive folder path, e.g. protondrive:/Sietch/Backups)}"
: "${PROTON_DRIVE_HOSTNAME:?PROTON_DRIVE_HOSTNAME is required (used as a sub-folder so multi-host syncs don't clobber each other)}"

if ! command -v proton-drive-cli >/dev/null 2>&1; then
  echo "proton-drive-cli is not installed or not on PATH" >&2
  exit 2
fi

DEST="${PROTON_DRIVE_DST%/}/${PROTON_DRIVE_HOSTNAME}/"
SRC_PATH="$(readlink -f "$PROTON_DRIVE_SRC")"

if [ ! -d "$SRC_PATH" ]; then
  echo "PROTON_DRIVE_SRC ($SRC_PATH) is not a directory" >&2
  exit 2
fi

echo "[proton-drive-sync] src=$SRC_PATH dst=$DEST ts=$(date -Iseconds)"

# `sync` is bidirectional by default — for an append-only backup target
# we want push-only. `--no-track-renames` avoids renaming the source
# if the remote ever renames a file. `--retries 3` survives transient
# network blips. `--transfers 4` saturates a 1 Gbps link.
exec proton-drive-cli sync \
  --no-track-renames \
  --retries 3 \
  --transfers 4 \
  --stats 30s \
  --log-level INFO \
  "$SRC_PATH" \
  "$DEST"
