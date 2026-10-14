#!/bin/bash
# proton-drive-failure-notify.sh — invoked by the notify@.service unit
# when the sync .service exits non-zero. Reads the journal to get the
# failing unit + result, then emails the Sietch admin list. Telegram
# notification reuses the same `backup_*` preferences persisted by
# the Sietch admin console.
set -euo pipefail
SVC="${1:-proton-drive-sietch-sync.service}"
ts="$(date -Iseconds)"

# Try to surface a meaningful error message from the previous run.
# `journalctl` requires the unit name without the .service suffix in
# some invocations; strip and try both forms.
short="${SVC%.service}"
err_line="$(journalctl -u "$short" -n 50 --no-pager -q \
  | tail -n 200 | grep -Ei 'error|failed|exception|panic|fatal' \
  | tail -n 5 || true)"
[ -z "$err_line" ] && err_line="(no recent error lines in journal)"

# Email the admin list from the Sietch DB (same source the
# backup_* notifications use) — keeps the on-call list consistent.
python3 - <<EOF
import os, sys, smtplib
from email.mime.text import MIMEText
from email.utils import formatdate
import db
admins = db.query_dicts("SELECT email FROM users WHERE is_admin=TRUE AND is_active=TRUE AND email<>''")
recipients = [a["email"] for a in admins if a.get("email")]
if not recipients:
    sys.exit(0)
body = (
    f"Proton Drive sync failed at {os.environ.get('ts','$ts')}\n\n"
    f"Unit: $SVC\n"
    f"Recent error lines:\n{os.environ.get('err','$err_line')[:2000]}\n\n"
    f"Inspect with: journalctl -u $short -n 100\n"
)
msg = MIMEText(body)
msg["Subject"] = f"[Sietch] Proton Drive sync FAILED"
msg["From"] = "sietch-backup@localhost"
msg["To"] = ", ".join(recipients)
msg["Date"] = formatdate()
try:
    with smtplib.SMTP("localhost") as s:
        s.send_message(msg)
except Exception as e:
    sys.stderr.write(f"notify: smtp failed: {e}\n")
EOF
