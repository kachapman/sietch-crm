# Migration Progress & Issues

## Overview

This document tracks the progress of migrating data from OnlyOffice CRM to Sietch CRM v3.0.

---

## Migration Components

### 1. Full Migration Script (`migrate_from_onlyoffice.py`)
- **Status:** EXISTS (902 lines)
- **What it does:** One-time migration from OnlyOffice CRM API to PostgreSQL
- **What it migrates:** Users, Stages, Tags, Custom Fields, Contacts, Opportunities (with tags, custom fields, history, attachments), Tasks, User Profiles
- **Does NOT migrate:** Email bodies (filehandler broken)

### 2. Admin Sync Backend
- **Status:** IMPLEMENTED (2026-08-05)
- **Endpoints:**
  - `POST /api/v2/admin/sync/test` — Test OnlyOffice connection
  - `POST /api/v2/admin/sync/pull-tags` — Pull tags from OnlyOffice
  - `POST /api/v2/admin/sync/pull-tasks` — Pull tasks from OnlyOffice
  - `POST /api/v2/admin/sync/full-reconcile` — Full read-only reconcile
- **UI:** Admin "Import Sync" tab with progress log

### 3. Email Import (`import_email.py`)
- **Status:** EXISTS but uses wrong table names
- **Current:** Expects `mail_messages`, `mail_accounts`, `mail_deal_links` (PostgreSQL)
- **Actual OnlyOffice tables:** `mail_mail`, `mail_mailbox`, `mail_chain_x_crm_entity` (MySQL)
- **Email bodies:** NOT in MySQL (filehandler broken)

### 4. Full Migration Wrapper (`full_migration.py`)
- **Status:** NOT YET CREATED
- **Purpose:** Orchestrate all migration steps automatically

---

## Known Issues

### Issue 1: Filehandler Broken
- **Symptom:** `GET /Products/CRM/HttpHandlers/filehandler.ashx?action=mailmessage&id=X` returns "marker file" message
- **Impact:** Cannot extract email bodies from OnlyOffice
- **Workaround:** Migrate metadata only, rely on IMAP sync for new emails
- **Status:** External issue (OnlyOffice server configuration)

### Issue 2: Email Table Names Mismatch
- **Current `import_email.py`:** Expects `mail_messages`, `mail_accounts`, `mail_deal_links`
- **Actual OnlyOffice MySQL tables:** `mail_mail`, `mail_mailbox`, `mail_chain_x_crm_entity`
- **Impact:** Email import script needs rewriting for MySQL

### Issue 3: Chain-to-Message Mapping
- **Problem:** `mail_chain_x_crm_entity` links chains to opportunities via `id_chain`
- **Chain IDs:** Long hash strings (not integers)
- **Need:** Map chain IDs to individual message IDs in `mail_mail`
- **Solution:** Query `mail_mail WHERE chain_id = X` to find messages in a chain

---

## Data Counts (from OnlyOffice MySQL)

| Table | Count | Notes |
|-------|-------|-------|
| `mail_mail` | 29,662 | Email messages (metadata only, no bodies) |
| `mail_chain_x_crm_entity` | 7,527 (type=3), 5 (type=1) | Deal-linked chains |
| `mail_tag` | 6 | Email tags |
| `opportunities` | ~1,191 | Deals/projects |
| `history_events` | ~38,898 | Timeline events |

---

## Migration Execution Plan

### Tonight's Migration (Automated)

**Step 1: Clear existing data**
```sql
TRUNCATE TABLE mail_tag_assignments CASCADE;
-- ... (full list in full_migration.py)
```

**Step 2: API Migration**
```bash
python migrate_from_onlyoffice.py \
  --portal-url https://office.publicadjustermidwest.com \
  --email bot@vanguardadj.com \
  --password FRi3tz4yWXrMTEZ \
  --db-host 127.0.0.1 \
  --db-name sietch_crm \
  --db-user sietch \
  --db-password local_dev_password
```

**Step 3: Email Metadata + Deal Links (MySQL)**
- Query `mail_chain_x_crm_entity` for deal-linked chains
- Query `mail_mail` for email metadata
- Store in PostgreSQL with deal links

**Step 4: User Profile Remap**
```bash
python migrate_dashboard_data.py
```

---

## Post-Migration Tasks

1. **Verify record counts** match expectations
2. **Spot-check opportunities** with custom fields and tags
3. **Enable ML training** toggle
4. **Test IMAP sync** for new emails
5. **Update history events** with email metadata + OnlyOffice URLs

---

## Timeline

| Phase | Status | Estimated Time |
|-------|--------|----------------|
| Full migration script | ✅ EXISTS | — |
| Admin sync backend | ✅ IMPLEMENTED | — |
| Email import rewrite | ⏳ PENDING | 2-3 hours |
| Full migration wrapper | ⏳ PENDING | 2-3 hours |
| Tonight's migration | ⏳ PENDING | 8-12 hours |
| Verification | ⏳ PENDING | 1 hour |

---

## Notes

- The OnlyOffice CRM server is at `68.183.130.39` (`office.publicadjustermidwest.com`)
- Bot credentials: `bot@vanguardadj.com` / `FRi3tz4yWXrMTEZ`
- MySQL is NOT exposed on host port — requires `docker exec` on CRM droplet
- Filehandler is broken — email bodies cannot be extracted via API
- IMAP sync will handle new emails going forward
