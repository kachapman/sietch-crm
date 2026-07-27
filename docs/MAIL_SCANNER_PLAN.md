# Mail Scanner — Sietch CRM Phase 3

## Architecture

The mail scanner is a separate Docker container (`sietch-mail-scanner`) that polls configured IMAP mailboxes using `imap_tools`, classifies incoming emails using a rule-based classifier pipeline (adapted from the `email_scanner` branch), and stores all email data in the Sietch CRM PostgreSQL database.

**Separate container** means:
- Updates to the scanner/classifier don't require restarting the dashboard
- ML dependencies (sentence-transformers, torch CPU) are isolated in the scanner container
- The scanner runs on the same `sietch-internal` Docker network as the dashboard

## Scope

### 3A: IMAP Sync Module
- Polls configured IMAP mailboxes via `imap_tools`
- Stores full email data (body_html, body_text, subject, from, to, date) in `mail_messages` table
- UID watermark-based incremental sync for large volumes
- `[#DEAL-ID]` subject auto-link regex matches opportunities
- Per-account folder filtering
- Attachments stored in DB (self-contained)
- Classifier pipeline from `email_scanner` branch adapted for IMAP

### 3B: Scanner Admin Panel UI
- Modal-based (not sidebar)
- IMAP account management (add/edit/delete)
- Inbox configuration and per-inbox filtering
- Tag management
- Template management
- Drafts compose
- Behavior toggles (create deals/tasks/notes/notifications)
- Connection indicator
- Log viewer
- Reprocess endpoint

### 3C: Deal Title Tooltip + Auto-Link
- `project_number` tooltip on deal title in preview modal
- Copy-to-clipboard
- `[#DEAL-ID]` subject auto-link to matching opportunities

### 3D: Universal CRM Inbox UI
- Reuses existing mail inbox modal scaffold in `app.js`
- Modal with minimize-to-sidebar
- Search, select-all, mark read/unread, delete
- Link-to-deal sidebar
- Expand/collapse email detail

## Database Tables

Existing (in `init.sql`):
- `mail_accounts` — IMAP account configurations
- `mail_messages` — full email storage
- `mail_deal_links` — email ↔ deal associations
- `mail_flag_queue` — IMAP flag sync queue

New (Phase 3):
- `mail_tags` — tag definitions (title, color)
- `mail_tag_assignments` — message ↔ tag many-to-many
- `mail_templates` — response templates (title, subject, body)

## Classifier Pipeline

Adapted from `email_scanner` branch `mail_scanner.py`. Rule-based, ordered, first-match-wins:

1. `[#DEAL-ID]` in subject → link to deal
2. Claim code only (regex) → try to find matching deal
3. Insurance carrier email → post note, add tags
4. JobNimbus task/job → create task
5. Acculynx notification → classify supplement/job
6. Needs reconciliation → create task
7. Adjuster action → create task
8. Uncertain → create review task

## ML Head

- `sentence-transformers/all-MiniLM-L6-v2` embeddings
- Logistic regression + kNN classifier (tie-breaker)
- Runs inside scanner container (CPU-only torch)
- Training script: `scanner/train_ml_head.py`
- Model weights persisted in `/app/data/mail_scanner/ml_models/`

## Deployment

```bash
# Local dev (scanner runs in-process with dashboard)
SCANNER_ENABLED=true SCANNER_IMAP_HOST=... ./start.sh

# Production (separate container)
docker compose -f docker-compose.scanner.yml up -d --build
```

## Configuration

Environment variables (in `.env` or `scanner/.env`):
- `SCANNER_ENABLED` — enable/disable polling
- `SCANNER_POLL_INTERVAL` — seconds between polls (default 300)
- `SCANNER_IMAP_HOST` — IMAP server hostname
- `SCANNER_IMAP_PORT` — IMAP port (default 993)
- `SCANNER_IMAP_USER` — IMAP username
- `SCANNER_IMAP_PASSWORD` — IMAP password
- `SCANNER_INBOX` — folder to poll (default INBOX)
- `SCANNER_CREATE_DEALS` — allow scanner to create deals (default false)
- `SCANNER_CREATE_TASKS` — allow scanner to create tasks (default false)
- `SCANNER_POST_NOTES` — allow scanner to post notes (default false)
- `SCANNER_NOTIFY_USERS` — allow scanner to notify users (default false)
- `SCANNER_ADMIN_TOKEN` — admin token for /api/v2/mail/* endpoints
- `SCANNER_SERVICE_URL` — URL of remote scanner service (empty = in-process)

## API Endpoints

All proxied through `server.py` as `/api/v2/mail/*`:

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v2/mail/inbox` | List messages (paginated) |
| GET | `/api/v2/mail/messages` | Search messages |
| GET | `/api/v2/mail/messages/{id}` | Get message detail with tags and links |
| POST | `/api/v2/mail/messages/{id}/link` | Link message to deal |
| PUT | `/api/v2/mail/messages/{id}/read` | Mark message as read |
| PUT | `/api/v2/mail/messages/{id}/unread` | Mark message as unread |
| DELETE | `/api/v2/mail/messages/{id}` | Delete message |
| POST | `/api/v2/mail/messages/{id}/tags` | Add tag to message |
| GET | `/api/v2/mail/tags` | List tags |
| GET | `/api/v2/mail/templates` | List templates |
| GET | `/api/v2/mail/accounts` | List IMAP accounts |
| GET | `/api/v2/mail/status` | Scanner status |
| GET | `/api/v2/mail/log` | Scanner log |
| POST | `/api/v2/mail/reprocess` | Reprocess messages |
| GET | `/api/v2/mail/config` | Get scanner config |
| PUT | `/api/v2/mail/config` | Update scanner config |
