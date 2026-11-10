"""IMAP mail scanner for Sietch CRM.

Polls configured IMAP mailboxes, classifies incoming emails, and
performs actions (link to deals, create tasks, post notes, apply tags).

Uses imap_tools for IMAP access and stores all email data in the
Sietch CRM PostgreSQL database via db.py.
"""

from __future__ import annotations

import functools
import hashlib
import html
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl  # POSIX-only cross-process file lock
    _HAVE_FCNTL = True
except ImportError:
    _HAVE_FCNTL = False  # Windows: in-process lock only

from imap_tools import A, MailBox, MailMessage, MailBoxFolderManager, UidRange

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db
from event_bus import event_bus
from user_profile_store import load_user_profile, save_user_profile
import oauth_providers
import mail_oauth
import mail_identity
import mail_dates
import infra_log

logger = logging.getLogger("sietch.mail_scanner")

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "mail_scanner"
DATA_DIR.mkdir(parents=True, exist_ok=True)

PROCESSED_IDS_FILE = DATA_DIR / "processed_ids.json"
LOG_FILE = DATA_DIR / "log.jsonl"
CONTRACTORS_FILE = DATA_DIR / "contractors.json"
CACHED_TAGS_FILE = DATA_DIR / "cached_tags.json"
FEEDBACK_FILE = DATA_DIR / "feedback.jsonl"

DEFAULT_CONTRACTORS = {
    "contractors": [
        {
            "id": "default",
            "name": "Default (All Inboxes)",
            "imap_account": "",
            "folder": "INBOX",
            "action": "link_only",
            "responsible": "",
        },
    ],
    "scanner_behavior": {
        "auto_link_project_id": {"enabled": True, "dry_run": False, "accounts": "all"},
        "auto_link_claim_code": {"enabled": False, "dry_run": True, "accounts": "all"},
        "auto_link_by_content": {"enabled": False, "dry_run": True, "accounts": "all"},
        "create_deals": {"enabled": False, "dry_run": True, "accounts": []},
        "create_tasks": {"enabled": False, "dry_run": True, "accounts": []},
        "post_notes": {"enabled": False, "dry_run": True, "accounts": []},
        "notify_users": {"enabled": False, "dry_run": True, "accounts": []},
    },
    "action_toggles": {
        "create_deals": False,
        "create_tasks": False,
        "post_notes": False,
        "notify_users": False,
    },
    "strong_custom_field_ids": [11, 26],
    "sending_domains": [],
}

IMAP_PORT_SSL = 993
IMAP_PORT_STARTTLS = 143

# ── Mail attachment storage settings (bug fix 2026-11-03) ───────────────────
# Attachment BYTES are stored on disk at sync time so that linking an email
# to a project can copy the files into the project's own storage WITHOUT a
# live mailbox round-trip (which is what silently dropped files in prod).
#
#   MAIL_ATTACHMENT_MAX_BYTES — per-file size limit for the local store.
#     Default 25 MB. Files above it are NOT stored locally: the metadata row
#     is still written (so the attachment is still listed and still
#     downloadable at link time via the mailbox fallback), but no bytes are
#     kept on disk. Each skip is logged.
#   MAIL_ATTACHMENT_STORAGE_ENABLED — "false" disables local storage
#     entirely (metadata only, the old behaviour).
#   MAIL_ATTACHMENT_FETCH_RETRIES — how many attempts the link-time mailbox
#     download makes (default 3).
#   MAIL_ATTACHMENT_FETCH_BACKOFF — base seconds for the retry backoff
#     (default 2.0; attempt N waits N * backoff).
#   MAIL_ATTACHMENT_DROP_STORED_ON_PURGE — when the mail retention job
#     purges a mail message, also delete its stored attachment bytes.
#     DEFAULT **false** (keep the files): the retention/backup policy for
#     data/mail-attachments/ is Ken's call and is NOT decided here. The
#     setting exists so the policy can be flipped without another code
#     change.
MAIL_ATTACHMENT_MAX_BYTES = int(os.environ.get("MAIL_ATTACHMENT_MAX_BYTES", str(25 * 1024 * 1024)))
MAIL_ATTACHMENT_STORAGE_ENABLED = os.environ.get("MAIL_ATTACHMENT_STORAGE_ENABLED", "true").lower() in ("true", "1", "yes")
MAIL_ATTACHMENT_FETCH_RETRIES = max(1, int(os.environ.get("MAIL_ATTACHMENT_FETCH_RETRIES", "3")))
MAIL_ATTACHMENT_FETCH_BACKOFF = float(os.environ.get("MAIL_ATTACHMENT_FETCH_BACKOFF", "2.0"))
MAIL_ATTACHMENT_DROP_STORED_ON_PURGE = os.environ.get(
    "MAIL_ATTACHMENT_DROP_STORED_ON_PURGE", "false").lower() in ("true", "1", "yes")

# How long a staging folder from a crashed run is kept before it is swept.
# A folder younger than this belongs to a RUNNING sync (accounts poll
# concurrently) and is never touched.
MAIL_ATTACHMENT_STAGING_MAX_AGE = int(
    os.environ.get("MAIL_ATTACHMENT_STAGING_MAX_AGE", "3600"))

# Hard cap on the stored MIME-part identifier. A fingerprint is ~40 chars;
# this is a belt-and-braces limit so no code path can ever write a large
# blob into remote_part_id / attachments_json.
MAX_PART_ID_LEN = 200

# Where the scanner keeps attachment bytes. Same layout the link-time copy
# already reads as "a local file": data/mail-attachments/<message_id>/<file>.
MAIL_ATTACHMENT_DIR = "data/mail-attachments"

# ── Optional unique indexes (blocker B1) ────────────────────────────────────
# Both indexes below are created by a migration, and a migration can leave
# them absent:
#   * migration 044 deliberately SKIPS uq_project_documents_opp_file_path
#     when project_documents already holds duplicates;
#   * migration 043's uq_history_attachments_mail_attachment is absent on a
#     database where that migration never ran.
#
# ⚠ An `ON CONFLICT (<cols>) WHERE <predicate> DO NOTHING` statement is
# REJECTED BY POSTGRES when no matching unique index exists:
#
#     ERROR:  there is no unique or exclusion constraint matching the
#             ON CONFLICT specification
#
# It is a hard error (not a "no conflict happened" case), so the copy step
# must check for the index and use a SELECT-then-INSERT path when it is
# missing. The result is cached — the schema does not change while the
# process runs (migrations are applied at boot).
#
# Mirrors server.py's PROJECT_DOCUMENTS_UNIQUE_INDEX / _unique_index_exists
# (the scanner cannot import server — server imports the scanner), so the
# names must be kept in sync by hand.
PROJECT_DOCUMENTS_UNIQUE_INDEX = "uq_project_documents_opp_file_path"
HISTORY_ATTACHMENTS_UNIQUE_INDEX = "uq_history_attachments_mail_attachment"

_INDEX_EXISTS_CACHE: dict[str, bool] = {}


def _unique_index_exists(index_name: str) -> bool:
    """Does the named unique index exist? (cached)

    Mirrors server.py `_unique_index_exists`. When the check itself fails we
    assume the index is ABSENT, because the SELECT-then-INSERT fallback is
    the path that cannot raise.
    """
    if index_name in _INDEX_EXISTS_CACHE:
        return _INDEX_EXISTS_CACHE[index_name]
    exists = False
    try:
        exists = bool(db.query_one(
            "SELECT 1 AS ok FROM pg_indexes WHERE schemaname = current_schema() "
            "AND indexname = %s", (index_name,)))
    except Exception:
        logger.exception("Could not check for the index %s", index_name)
        exists = False
    _INDEX_EXISTS_CACHE[index_name] = exists
    return exists


def _reset_index_cache() -> None:
    """Forget the cached index existence flags (tests / after a migration)."""
    _INDEX_EXISTS_CACHE.clear()


_CRM_ACCOUNT_IDS: set[int] | None = None


def _get_crm_account_ids() -> set[int]:
    """Return IDs of mail_accounts flagged as CRM mail. Cached per process."""
    global _CRM_ACCOUNT_IDS
    if _CRM_ACCOUNT_IDS is not None:
        return _CRM_ACCOUNT_IDS
    try:
        rows = db.query_dicts("SELECT id FROM mail_accounts WHERE is_crm_mail = TRUE")
        _CRM_ACCOUNT_IDS = {r["id"] for r in rows}
    except Exception:
        _CRM_ACCOUNT_IDS = set()
    return _CRM_ACCOUNT_IDS


DEAL_LINK_RE = re.compile(r"\[#(\d+)\]", re.IGNORECASE)
CLAIM_CODE_RE = re.compile(r"^[A-Za-z0-9\-]{5,20}$")
DEAL_ID_RE = re.compile(r"\[#DEAL-(\d+)\]", re.IGNORECASE)

ML_ENABLED = False
ml_model = None
ml_vectorizer = None

# In-memory deal field index, refreshed each poll cycle
_deal_index: dict[str, Any] = {}
_deal_index_built_at: float = 0

# Address normalization
_ADDRESS_ABBREVS = {
    "street": "st", "avenue": "ave", "boulevard": "blvd", "drive": "dr",
    "lane": "ln", "court": "ct", "place": "pl", "road": "rd",
    "circle": "cir", "terrace": "ter", "highway": "hwy", "parkway": "pkwy",
}


def _normalize_behavior_config(config: dict[str, Any]) -> dict[str, Any]:
    """Convert old boolean behavior format to new object format."""
    behavior = config.get("scanner_behavior", {})
    normalized = {}
    for key, value in behavior.items():
        if isinstance(value, bool):
            normalized[key] = {"enabled": value, "dry_run": False, "accounts": "all"}
        elif isinstance(value, dict):
            normalized[key] = value
        else:
            normalized[key] = {"enabled": bool(value), "dry_run": False, "accounts": "all"}
    config["scanner_behavior"] = normalized
    # Normalize null/missing accounts to "crm" (matches UI default of CRM Only)
    for cfg in config.get("scanner_behavior", {}).values():
        if isinstance(cfg, dict) and cfg.get("accounts") is None:
            cfg["accounts"] = "crm"
    # Ensure custom_behaviors is a list
    if "custom_behaviors" not in config:
        config["custom_behaviors"] = []
    return config


def _is_behavior_enabled(behavior_cfg: dict[str, Any], account_id: int | None = None) -> bool:
    """Check if a behavior task is enabled for a given account.

    accounts: "all" = every account, "crm" = CRM-only accounts, list = explicit IDs.
    A null/missing value is treated as "crm" to match the UI default of "CRM Only".
    """
    if not behavior_cfg.get("enabled", False):
        return False
    accounts = behavior_cfg.get("accounts", "crm")
    if accounts is None:
        accounts = "crm"
    if accounts == "all":
        return True
    if accounts == "crm":
        if account_id is None:
            return False
        return account_id in _get_crm_account_ids()
    if isinstance(accounts, list) and account_id is not None:
        return account_id in accounts
    return False


def _link_toggle_allows(classification: dict[str, Any] | None, toggles: dict[str, Any] | None,
                        account_id: int | None = None) -> tuple[bool, bool]:
    """Decide whether a classified link may execute: (allowed, dry_run).

    The toggle is chosen by the classification's link_method — "project_id"
    ([#123]) vs "claim_code" (claim # matching) — so each checkbox governs
    exactly its own matches. Classifications without a method (e.g. custom
    rules) fall back to the project-ID toggle, preserving historical behavior.
    """
    method = ((classification or {}).get("link_method") or "project_id")
    key = "auto_link_project_id" if method == "project_id" else "auto_link_claim_code"
    cfg = (toggles or {}).get(key) or {}
    if not _is_behavior_enabled(cfg, account_id):
        return (False, False)
    return (True, bool(_is_dry_run(cfg, account_id)))


def _is_dry_run(behavior_cfg: dict[str, Any], account_id: int | None = None) -> bool:
    """Check if a behavior task is in dry-run mode for a given account."""
    if not behavior_cfg.get("dry_run", False):
        return False
    accounts = behavior_cfg.get("accounts", "crm")
    if accounts is None:
        accounts = "crm"
    if accounts == "all":
        return True
    if accounts == "crm":
        if account_id is None:
            return False
        return account_id in _get_crm_account_ids()
    if isinstance(accounts, list) and account_id is not None:
        return account_id in accounts
    return False


def _normalize_address(addr: str) -> str:
    """Normalize address for matching: lowercase, remove periods, abbreviate."""
    addr = addr.lower().strip()
    addr = addr.replace(".", "")
    addr = re.sub(r"\s+", " ", addr)
    for full, abbr in _ADDRESS_ABBREVS.items():
        addr = addr.replace(full, abbr)
    return addr


def _render_template(template: str, context: dict[str, Any]) -> str:
    """Replace {variable} placeholders in template with context values."""
    def replacer(match):
        key = match.group(1)
        return str(context.get(key, match.group(0)))
    return re.sub(r"\{(\w+)\}", replacer, template)


def _build_template_context(msg: dict[str, Any], deal: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build template variable context from message and deal."""
    body_text = msg.get("body_text") or ""
    return {
        "subject": msg.get("subject", ""),
        "from": msg.get("from", ""),
        "from_name": msg.get("from_name", ""),
        "project": deal.get("title", "") if deal else "",
        "project_id": str(deal["id"]) if deal else "",
        "claim_number": "",
        "date": str(msg.get("date", "")),
        "body_preview": body_text[:200],
    }


def _build_graph_token_refresher():
    """Returns a closure (account_id) -> (graph_token, error) used by
    `mail_send.try_send_for_account` to obtain a fresh Graph access token
    when the scanner sends via Graph. The closure refreshes and persists
    the token via `db.execute`, mirroring server.py's
    `_refresh_graph_token_if_needed`."""
    from datetime import datetime, timezone
    import time

    def _refresh(account_id: int) -> tuple[str | None, str | None]:
        row = db.query_one(
            "SELECT email, oauth_graph_access_token, oauth_graph_refresh_token, oauth_graph_token_expires "
            "FROM mail_accounts WHERE id = %s",
            (account_id,),
        )
        if not row or not row.get("oauth_graph_access_token"):
            return None, None
        expires = row.get("oauth_graph_token_expires")
        token = row.get("oauth_graph_access_token", "")
        if expires and isinstance(expires, datetime) and expires.timestamp() > datetime.now(timezone.utc).timestamp():
            return token, None
        if not row.get("oauth_graph_refresh_token"):
            return None, "Graph token expired and no refresh token stored — reconnect from mail account settings"
        acct_email = (row.get("email") or "").strip().lower()
        try:
            import oauth_providers
            tok = oauth_providers.MicrosoftProvider.graph_refresh_token(row["oauth_graph_refresh_token"])
            new_token = tok.get("access_token", "")
            new_refresh = tok.get("refresh_token", row["oauth_graph_refresh_token"])
            new_expires_ts = time.time() + tok.get("expires_in", 3600)
            if not new_token:
                return None, "Graph token refresh returned no access token"
            # Verify the refreshed token's identity (live bug 2026-10-29):
            # a refresh token is bound to the granting mailbox, so the
            # id_token in the refresh response is authoritative. A mismatch
            # means this account holds another mailbox's token.
            ms_email = (tok.get("email") or "").strip().lower()
            # Fail CLOSED (live bug 2026-11-09): a token whose identity cannot
            # be read is a refusal, not a pass. The old `if ms_email and ...`
            # let a mis-bound grant keep sending as another mailbox.
            import mail_oauth_binding
            state_kind, detail = mail_oauth_binding.identity_state(ms_email, acct_email)
            if state_kind != mail_oauth_binding.IDENTITY_OK:
                if state_kind == mail_oauth_binding.IDENTITY_MISMATCH:
                    err = mail_oauth_binding.mismatch_message(detail, acct_email, kind="graph")
                else:
                    err = (
                        "The Graph refresh did not report which mailbox it belongs to, so it "
                        f"cannot be verified for {acct_email}. Disconnect Graph and reconnect "
                        f"while signed in as {acct_email}."
                    )
                logger.warning("Graph token identity refused on refresh for account %s: %s", account_id, err)
                try:
                    db.execute(
                        "UPDATE mail_accounts SET oauth_graph_last_error = %s, oauth_graph_last_error_at = NOW() WHERE id = %s",
                        (err[:300], account_id),
                    )
                except Exception:
                    pass
                return None, err
            new_refresh = mail_oauth_binding.refresh_token_to_store(
                tok.get("refresh_token"), row["oauth_graph_refresh_token"])
            db.execute(
                "UPDATE mail_accounts SET oauth_graph_access_token = %s, oauth_graph_refresh_token = %s, "
                "oauth_graph_token_expires = to_timestamp(%s), oauth_graph_identity = %s "
                "WHERE id = %s",
                (new_token, new_refresh, new_expires_ts, ms_email, account_id),
            )
            return new_token, None
        except Exception as e:
            logger.exception("Graph token refresh failed for account %d", account_id)
            return None, f"Graph token refresh failed: {e}"

    return _refresh


def _execute_custom_behavior(
    behavior: dict[str, Any],
    msg: dict[str, Any],
    msg_id: int | None,
    deal_id: int | None,
    account_id: int | None,
) -> dict[str, Any]:
    """Execute a single custom behavior. Returns log entry updates."""
    result: dict[str, Any] = {}
    behavior_type = behavior.get("type", "")
    config = behavior.get("config", {})
    dry = behavior.get("dry_run", False)

    deal = None
    if deal_id:
        deal = _find_opportunity_by_deal_id(deal_id)

    ctx = _build_template_context(msg, deal)

    try:
        if behavior_type == "create_task":
            assignee_id = config.get("assignee_user_id")
            title = _render_template(config.get("task_title_template", "Email: {subject}"), ctx)
            description = _render_template(config.get("task_description_template", ""), ctx)
            if dry:
                result["dry_run_action"] = "create_task"
                result["dry_run_detail"] = f"Task '{title}' for user {assignee_id}"
            else:
                # Insert into tasks table
                db.execute(
                    """INSERT INTO tasks (title, description, responsible_user_id, owner_user_id, opportunity_id, is_closed, created_at)
                       VALUES (%s, %s, %s, %s, %s, FALSE, NOW())""",
                    (title, description, assignee_id, assignee_id, deal_id),
                )
                result["task_created"] = True

        elif behavior_type == "notify_users":
            user_ids = config.get("notify_user_ids", [])
            method = config.get("notification_method", "in_app")
            title = _render_template("Email: {subject}", ctx)
            if dry:
                result["dry_run_action"] = "notify_users"
                result["dry_run_detail"] = f"Notify {len(user_ids)} users via {method}"
            else:
                # Insert notifications for each user
                # Fetch project_number for link construction
                project_number = None
                if deal_id:
                    try:
                        pn_row = db.query_one("SELECT project_number FROM opportunities WHERE id = %s", (deal_id,))
                        if pn_row:
                            project_number = pn_row.get("project_number") or pn_row[0] if isinstance(pn_row, dict) else pn_row[0]
                    except Exception:
                        pass
                link_id = project_number or deal_id
                for uid in user_ids:
                    payload = {"title": title, "link": f"/project/{link_id}" if link_id else None}
                    db.execute(
                        """INSERT INTO notifications (user_id, message, type, payload, created_at)
                           VALUES (%s, %s, 'email_linked', %s::jsonb, NOW())""",
                        (uid, f"New email linked to {ctx['project']}", json.dumps(payload)),
                    )
                    event_bus.publish("notification:new", {
                        "type": "email_linked",
                        "message": f"New email linked to {ctx['project']}",
                        "payload": payload,
                    }, user_ids=[str(uid)])
                result["users_notified"] = len(user_ids)

        elif behavior_type == "create_deal":
            title = _render_template(config.get("deal_title_template", "{from} - {subject}"), ctx)
            stage_id = config.get("stage_id")
            if dry:
                result["dry_run_action"] = "create_deal"
                result["dry_run_detail"] = f"Deal '{title}'"
            else:
                # Insert into opportunities
                stage_val = stage_id if stage_id else None
                new_deal = db.query_one(
                    """INSERT INTO opportunities (title, stage_id, created_at)
                       VALUES (%s, %s, NOW()) RETURNING id""",
                    (title, stage_val),
                )
                if new_deal:
                    result["deal_created"] = True
                    result["new_deal_id"] = new_deal["id"]
                    # Auto-create task if configured
                    if config.get("auto_create_task"):
                        task_assignee = config.get("task_assignee_id")
                        task_title = f"Follow up: {title}"
                        db.execute(
                            """INSERT INTO tasks (title, responsible_user_id, owner_user_id, opportunity_id, is_closed, created_at)
                               VALUES (%s, %s, %s, %s, FALSE, NOW())""",
                            (task_title, task_assignee, task_assignee, new_deal["id"]),
                        )
                    # Notify user if configured
                    notify_uid = config.get("notify_user_id")
                    if notify_uid:
                        deal_pn = new_deal.get("project_number") or new_deal["id"]
                        payload = {"title": f"New deal created: {title}", "link": f"/project/{deal_pn}"}
                        db.execute(
                            """INSERT INTO notifications (user_id, message, type, payload, created_at)
                               VALUES (%s, %s, 'deal_created', %s::jsonb, NOW())""",
                            (notify_uid, f"Auto-created from email '{ctx['subject']}'", json.dumps(payload)),
                        )
                        event_bus.publish("notification:new", {
                            "type": "deal_created",
                            "message": f"New deal created: {title}",
                            "payload": payload,
                        }, user_ids=[str(notify_uid)])

        elif behavior_type == "add_email_tags":
            tag_ids = config.get("tag_ids", [])
            if dry:
                result["dry_run_action"] = "add_email_tags"
                result["dry_run_detail"] = f"Add {len(tag_ids)} tags to email"
            elif msg_id:
                for tag_id in tag_ids:
                    db.execute(
                        "INSERT INTO mail_tag_assignments (message_id, tag_id, assigned_at) VALUES (%s, %s, NOW()) ON CONFLICT DO NOTHING",
                        (msg_id, tag_id),
                    )
                result["tags_added"] = len(tag_ids)

        elif behavior_type == "add_project_tags":
            tag_titles = config.get("tag_titles", [])
            if dry:
                result["dry_run_action"] = "add_project_tags"
                result["dry_run_detail"] = f"Add {len(tag_titles)} tags to project"
            elif deal_id:
                for title in tag_titles:
                    # Ensure tag exists
                    tag = db.query_one("SELECT id FROM tag_definitions WHERE title = %s", (title,))
                    if not tag:
                        tag = db.query_one("INSERT INTO tag_definitions (title) VALUES (%s) RETURNING id", (title,))
                    if tag:
                        db.execute(
                            "INSERT INTO opportunity_tags (opportunity_id, tag_id, assigned_at) VALUES (%s, %s, NOW()) ON CONFLICT DO NOTHING",
                            (deal_id, tag["id"]),
                        )
                result["project_tags_added"] = len(tag_titles)

        elif behavior_type == "change_project_stage":
            target_stage_id = config.get("stage_id")
            condition = config.get("condition", "always")
            condition_stage_id = config.get("condition_stage_id")
            if not target_stage_id or not deal_id:
                result["skipped"] = True
                return result
            if dry:
                result["dry_run_action"] = "change_project_stage"
                result["dry_run_detail"] = f"Move project to stage {target_stage_id}"
            else:
                # Check condition
                if condition == "only_if" and condition_stage_id:
                    current = db.query_one("SELECT stage_id FROM opportunities WHERE id = %s", (deal_id,))
                    if current and current["stage_id"] != condition_stage_id:
                        result["skipped"] = True
                        return result
                db.execute("UPDATE opportunities SET stage_id = %s WHERE id = %s", (target_stage_id, deal_id))
                result["stage_changed"] = True

        elif behavior_type == "reply_to_email":
            action = config.get("action", "create_draft")
            reply_template = config.get("reply_template", "")
            reply_body = _render_template(reply_template, ctx)
            if dry:
                result["dry_run_action"] = "reply_to_email"
                result["dry_run_detail"] = f"{'Send' if action == 'send_reply' else 'Draft'} reply to {ctx['from']}"
            else:
                if action == "create_draft":
                    # Store as draft in mail_messages
                    db.execute(
                        """INSERT INTO mail_messages (account_id, imap_uid, message_id, from_addr, to_addr,
                           subject, body_html, date_received, folder, is_read)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), 'Drafts', TRUE)""",
                        (account_id, f"draft:auto:{msg_id or 'new'}", f"<auto-reply-{msg_id}>",
                         ctx["from"], ctx["from"], f"Re: {ctx['subject']}", reply_body),
                    )
                    result["draft_created"] = True
                elif action == "send_reply":
                    # Send via Graph (preferred for MS OAuth accounts when a
                    # Graph token is configured) or SMTP fallback. The
                    # Graph path bypasses the 5.7.139 SMTP AUTH disable.
                    try:
                        acct = db.query_one("SELECT * FROM mail_accounts WHERE id = %s", (account_id,))
                        if acct:
                            from mail_send import try_send_for_account
                            refresh_graph = _build_graph_token_refresher()
                            ok, err, _send_method = try_send_for_account(
                                acct,
                                to_addr=ctx["from"],
                                subject=f"Re: {ctx['subject']}",
                                html_body=reply_body,
                                text_body=reply_body,
                                refresh_graph_token=refresh_graph,
                            )
                            result["reply_sent"] = ok
                            if not ok:
                                result["reply_error"] = err
                    except Exception as e:
                        result["reply_error"] = str(e)

    except Exception as e:
        logger.error("Custom behavior %s failed: %s", behavior_type, e)
        result["error"] = str(e)

    return result


def _execute_custom_behaviors(
    msg: dict[str, Any],
    msg_id: int | None,
    deal_id: int | None,
    account_id: int | None,
    classification: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Execute all enabled custom behaviors for a message."""
    results = []
    config = get_contractors()
    custom_behaviors = config.get("custom_behaviors", [])
    classification = classification or {}

    # Sort by order
    sorted_behaviors = sorted(custom_behaviors, key=lambda b: b.get("order", 999))

    for behavior in sorted_behaviors:
        if not behavior.get("enabled", False):
            continue
        if not _is_behavior_enabled(behavior, account_id):
            continue

        # Check triggers
        triggers = behavior.get("triggers", [])
        if triggers:
            # Check if triggers have deal conditions but no deal linked
            has_deal_cond = any(_has_deal_conditions(t) for t in triggers)
            if has_deal_cond and not deal_id:
                # Skip but log
                results.append({
                    "behavior_type": behavior.get("type", ""),
                    "dry_run": behavior.get("dry_run", False),
                    "skipped": True,
                    "skip_reason": "No deal linked (deal-based trigger required)",
                })
                continue

            if not _evaluate_triggers(triggers, msg, classification):
                continue

        result = _execute_custom_behavior(behavior, msg, msg_id, deal_id, account_id)
        result["behavior_type"] = behavior.get("type", "")
        result["dry_run"] = behavior.get("dry_run", False)
        results.append(result)

    return results


# ── Trigger Evaluation Engine ────────────────────────────────────────────────

# Sentinel pattern returned for bad regex (compile failure). Matches nothing,
# so _evaluate_condition treats it as "no match" instead of raising every poll.
_NEVER_MATCH_RE: re.Pattern = re.compile(r"(?!)")

# Bounded regex cache. Bounded so admin-supplied classification rules can't
# grow memory unboundedly across the lifetime of the process. lru_cache also
# makes bad-regex patterns safe to look up — the first call captures the
# failure and every subsequent call is O(1) and never raises.
@functools.lru_cache(maxsize=1024)
def _get_regex(pattern: str) -> re.Pattern:
    """Compile and cache a regex pattern. Returns a never-match sentinel on
    compile failure so callers can treat bad patterns as 'no match' without
    needing their own try/except."""
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return _NEVER_MATCH_RE


def _is_valid_regex(pattern: str) -> bool:
    """Used by the rule-save endpoints to reject malformed patterns at write
    time (instead of silently no-matching at scan time)."""
    try:
        re.compile(pattern, re.IGNORECASE)
        return True
    except re.error:
        return False


def _evaluate_triggers(triggers: list, msg: dict, classification: dict) -> bool:
    """Evaluate all triggers (OR between triggers)."""
    if not triggers:
        return True  # No triggers = always execute
    return any(_evaluate_trigger_group(t, msg, classification) for t in triggers)


def _evaluate_trigger_group(trigger: dict, msg: dict, classification: dict) -> bool:
    """Evaluate a trigger group (AND/OR within group)."""
    logic = trigger.get("logic", "and")
    conditions = trigger.get("conditions", [])
    if not conditions:
        return True
    results = [_evaluate_condition(c, msg, classification) for c in conditions]
    return all(results) if logic == "and" else any(results)


def _has_deal_conditions(trigger: dict) -> bool:
    """Check if a trigger has any deal-based conditions."""
    deal_types = {"linked_to_deal", "deal_stage_id", "deal_stage_is_not",
                  "deal_value_above", "deal_value_below", "deal_has_tag", "deal_field_contains"}
    for cond in trigger.get("conditions", []):
        if cond.get("type") in deal_types:
            return True
    return False


def _evaluate_condition(condition: dict, msg: dict, classification: dict) -> bool:
    """Evaluate a single trigger condition."""
    ctype = condition.get("type", "")
    value = condition.get("value")
    negate = condition.get("negate", False)
    result = False

    subject = (msg.get("subject", "") or "").lower()
    body = ((msg.get("body_text") or "")[:5000]).lower()
    sender = (msg.get("from", "") or "").lower()
    sender_name = (msg.get("from_name", "") or "").lower()

    # ── Text-based conditions ──
    if ctype == "subject_contains":
        result = value.lower() in subject if value else False
    elif ctype == "subject_regex":
        # _get_regex returns a never-match sentinel on re.error, so this is safe
        # without a try/except (and a try/except per call would be expensive on hot path).
        result = bool(_get_regex(value).search(subject)) if value else False
    elif ctype == "body_contains":
        result = value.lower() in body if value else False
    elif ctype == "body_regex":
        result = bool(_get_regex(value).search(body)) if value else False
    elif ctype == "sender_email":
        result = value.lower() == sender if value else False
    elif ctype == "sender_domain":
        domain = sender.split("@")[-1] if "@" in sender else ""
        result = value.lower() in domain if value else False
    elif ctype == "sender_name":
        result = value.lower() in sender_name if value else False

    # ── Deal-based conditions ──
    elif ctype == "linked_to_deal":
        result = bool(classification.get("linked_deal_id"))
    elif ctype in ("deal_stage_id", "deal_stage_is_not"):
        deal_id = classification.get("linked_deal_id")
        if deal_id:
            deal = _find_opportunity_by_deal_id(deal_id)
            result = deal and deal.get("stage_id") == value
            if ctype == "deal_stage_is_not":
                result = not result
        else:
            return not negate  # Skip = condition not met
    elif ctype == "deal_value_above":
        deal_id = classification.get("linked_deal_id")
        if deal_id:
            deal = _find_opportunity_by_deal_id(deal_id)
            result = deal and (deal.get("bid_value") or 0) > float(value or 0)
        else:
            return not negate
    elif ctype == "deal_value_below":
        deal_id = classification.get("linked_deal_id")
        if deal_id:
            deal = _find_opportunity_by_deal_id(deal_id)
            result = deal and (deal.get("bid_value") or 0) < float(value or 0)
        else:
            return not negate
    elif ctype == "deal_has_tag":
        deal_id = classification.get("linked_deal_id")
        if deal_id and value:
            try:
                tag_match = db.query_one(
                    "SELECT id FROM opportunity_tags WHERE opportunity_id = %s AND tag_id = %s",
                    (deal_id, int(value))
                )
                result = bool(tag_match)
            except Exception:
                result = False
        else:
            return not negate
    elif ctype == "deal_field_contains":
        deal_id = classification.get("linked_deal_id")
        if deal_id and value and ":" in value:
            field_key, search_val = value.split(":", 1)
            try:
                field_match = db.query_one(
                    """SELECT id FROM opportunity_custom_field_values
                       WHERE opportunity_id = %s AND field_id = (
                           SELECT id FROM custom_field_definitions WHERE field_key = %s
                       ) AND field_value ILIKE %s""",
                    (deal_id, field_key, f"%{search_val}%")
                )
                result = bool(field_match)
            except Exception:
                result = False
        else:
            return not negate

    # ── Email-based conditions ──
    elif ctype == "has_attachment":
        result = bool(msg.get("attachments"))
    elif ctype == "attachment_count_above":
        result = len(msg.get("attachments") or []) > int(value or 0)
    elif ctype == "is_reply":
        result = bool(re.match(r"^re:", subject, re.IGNORECASE))
    elif ctype == "is_forward":
        result = bool(re.match(r"^fwd?:", subject, re.IGNORECASE))
    elif ctype == "has_cc":
        result = bool(msg.get("cc"))
    elif ctype == "has_bcc":
        result = bool(msg.get("bcc"))

    return not result if negate else result


def _build_deal_index() -> dict[str, Any]:
    """Build in-memory index of deal fields for content matching."""
    global _deal_index, _deal_index_built_at

    index: dict[str, Any] = {
        "crm_ids": {},      # crm_id_string -> deal_id
        "claims": {},       # claim_code -> deal_id
        "policies": {},     # policy_number -> deal_id
        "addresses": {},    # normalized_address -> deal_id
        "contact_ids": {},  # contact_id -> deal_id
    }

    try:
        # Get all opportunities (open, closed, lost)
        opps = db.query_dicts("SELECT id, title FROM opportunities")
        if not opps:
            return index

        # Get custom field definitions for claim and policy
        claim_field_id = None
        policy_field_id = None
        address_field_id = None
        fields = db.query_dicts("SELECT id, field_key FROM custom_field_definitions")
        for f in fields:
            if f["field_key"] == "field_11":
                claim_field_id = f["id"]
            elif f["field_key"] == "field_26":
                policy_field_id = f["id"]
            elif "address" in (f["field_key"] or "").lower():
                address_field_id = f["id"]

        # Index opportunity IDs
        for opp in opps:
            index["crm_ids"][str(opp["id"])] = opp["id"]

        # Index claim codes
        if claim_field_id:
            claim_rows = db.query_dicts(
                "SELECT field_value, opportunity_id FROM opportunity_custom_field_values WHERE field_id = %s",
                (claim_field_id,),
            )
            for row in (claim_rows or []):
                if row.get("field_value"):
                    index["claims"][row["field_value"].strip().lower()] = row["opportunity_id"]

        # Index policy numbers
        if policy_field_id:
            policy_rows = db.query_dicts(
                "SELECT field_value, opportunity_id FROM opportunity_custom_field_values WHERE field_id = %s",
                (policy_field_id,),
            )
            for row in (policy_rows or []):
                if row.get("field_value"):
                    index["policies"][row["field_value"].strip().lower()] = row["opportunity_id"]

        # Index addresses
        if address_field_id:
            addr_rows = db.query_dicts(
                "SELECT field_value, opportunity_id FROM opportunity_custom_field_values WHERE field_id = %s",
                (address_field_id,),
            )
            for row in (addr_rows or []):
                if row.get("field_value"):
                    normalized = _normalize_address(row["field_value"])
                    index["addresses"][normalized] = row["opportunity_id"]

        # Index contact IDs
        try:
            contacts = db.query_dicts("SELECT id FROM contacts")
            for c in (contacts or []):
                index["contact_ids"][str(c["id"])] = c["id"]
        except Exception:
            pass

        _deal_index = index
        _deal_index_built_at = time.time()
        logger.info("Built deal index: %d crm_ids, %d claims, %d policies, %d addresses, %d contact_ids",
                     len(index["crm_ids"]), len(index["claims"]), len(index["policies"]),
                     len(index["addresses"]), len(index["contact_ids"]))
    except Exception as e:
        logger.error("Failed to build deal index: %s", e)

    return index


def _match_by_content(subject: str, body: str, deal_index: dict[str, Any]) -> list[dict[str, Any]]:
    """Match email content against deal field index."""
    matches = []

    # Combine subject + first 5000 chars of body
    text = f"{subject}\n{body[:5000]}".lower()

    # Search each field type
    for field_type, field_dict in deal_index.items():
        for value, deal_id in field_dict.items():
            if len(value) < 3:
                continue  # Skip very short values
            if value in text:
                matches.append({
                    "deal_id": deal_id,
                    "field_type": field_type,
                    "matched_value": value,
                    "confidence": "strong",
                })

    # Deduplicate by deal_id, keep first match
    seen = {}
    for m in matches:
        if m["deal_id"] not in seen:
            seen[m["deal_id"]] = m
    return list(seen.values())


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default if default is not None else {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


# Audit #6: same locking pattern as record_feedback_candidate. The scanner
# log is appended from the scanner background thread and read by
# get_scanner_log (admin UI + scanner_service admin endpoint).
_LOG_LOCK = threading.Lock()


def _append_log(entry: dict[str, Any]) -> None:
    line = json.dumps(entry, default=str) + "\n"
    try:
        with _LOG_LOCK:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                # fileno() may not exist on test fakes; skip flock cleanly.
                if _HAVE_FCNTL and hasattr(f, "fileno"):
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    except (OSError, ValueError):
                        pass
                f.write(line)
                f.flush()
                if hasattr(f, "fileno"):
                    try:
                        os.fsync(f.fileno())
                    except (OSError, AttributeError):
                        pass
                if _HAVE_FCNTL and hasattr(f, "fileno"):
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    except (OSError, ValueError):
                        pass
    except Exception:
        logger.exception("Failed to append scanner log entry to %s", LOG_FILE)


def _now_et() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_html(html_body: str) -> str:
    if not html_body:
        return ""
    cleaned = re.sub(r"<script[^>]*>.*?</script>", "", html_body, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<style[^>]*>.*?</style>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", cleaned)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:8000]


def _sanitize_body(html_body: str) -> str:
    text = _sanitize_html(html_body)
    return text


def _email_body_for_note(msg: dict[str, Any]) -> str:
    body = msg.get("body_html") or msg.get("htmlBody") or ""
    text = msg.get("body_text") or msg.get("textBody") or ""
    if body:
        plain = _sanitize_html(body)
        if plain:
            return plain
    if text:
        return text[:4000]
    return ""


def _most_recent_body(msg: dict[str, Any]) -> str:
    body = msg.get("body_html") or msg.get("htmlBody") or ""
    intro = msg.get("introduction") or ""
    base = intro or _sanitize_body(body)
    if not base:
        return ""
    for marker in (
        r"[-_= \t]*forwarded message[-_= \t]*",
        r"[-_= \t]*original message[-_= \t]*",
        r"^On .*wrote:$",
        r"^From:.*$",
    ):
        m = re.search(marker, base, re.IGNORECASE | re.MULTILINE)
        if m:
            base = base[: m.start()].strip()
            break
    lines = base.splitlines()
    clean_lines = []
    for ln in lines:
        if ln.strip().startswith(">"):
            break
        clean_lines.append(ln)
    base = "\n".join(clean_lines).strip()
    return base[:4000]


def _extract_claimant_from_body(body_text: str) -> str:
    lines = [l.strip() for l in (body_text or "").split("\n") if l.strip()]
    if lines and not lines[0].startswith("From:") and not lines[0].startswith("Subject:"):
        return lines[0]
    return ""


def _init_ml() -> bool:
    global ml_model, ml_vectorizer, ML_ENABLED
    if ML_ENABLED and ml_model is None:
        try:
            import pickle
            from sentence_transformers import SentenceTransformer

            model_path = DATA_DIR / "ml_models" / "sietch-classifier-head.pkl"
            if model_path.exists():
                with open(model_path, "rb") as f:
                    ml_model = pickle.load(f)
                ml_vectorizer = SentenceTransformer("all-MiniLM-L6-v2")
                ML_ENABLED = True
                logger.info("ML head loaded from %s (labels: %s)", model_path, ml_model.get("labels"))
            else:
                logger.info("No trained model at %s — ML disabled", model_path)
                ML_ENABLED = False
        except Exception as e:
            logger.warning("ML init failed: %s", e)
            ML_ENABLED = False
    return ML_ENABLED


def _ml_embed(text: str) -> list[float] | None:
    global ml_vectorizer
    if not ML_ENABLED or ml_vectorizer is None:
        return None
    try:
        emb = ml_vectorizer.encode(text[:500], convert_to_numpy=True)
        return emb.tolist()
    except Exception:
        return None


def _ml_classify(subject: str, body: str) -> dict[str, Any] | None:
    if not ML_ENABLED or ml_model is None:
        return None
    try:
        emb = _ml_embed(body or subject or "")
        if emb is None:
            return None
        import numpy as np
        X = np.array([emb])
        logistic_pred = ml_model["logistic"].predict(X)[0]
        knn_pred = ml_model["knn"].predict(X)[0]
        return {"logistic": logistic_pred, "knn": knn_pred, "used_ml": True}
    except Exception:
        return None


def _apply_ml_override(
    log_entry: dict[str, Any],
    classification: str,
    match_strength: str,
    ml_result: dict[str, Any] | None,
) -> str:
    if ml_result is None or match_strength != "weak":
        return classification
    logistic = ml_result.get("logistic")
    knn = ml_result.get("knn")
    # Both models agree: this email is likely linked to a deal
    if logistic == knn and logistic == "linked":
        log_entry["ml_applied"] = True
        return "ml_suggested_link"
    return classification


def _load_processed_ids() -> set[str]:
    return set(_read_json(PROCESSED_IDS_FILE, default=[]) or [])


def _save_processed_ids(ids: set[str]) -> None:
    _write_json(PROCESSED_IDS_FILE, sorted(list(ids)))


def _processed_key(account_id, conv_id: str, folder: str | None = None) -> str:
    """The processed-ID key for a message.

    Scoped per account so a deleted account's UIDs can never suppress another
    account's messages, and — since 2026-11-04 — per FOLDER as well: a Sent
    UID 4114 and an INBOX UID 4114 are different messages, so the key is
    `account:folder:uid`.

    BACKWARD COMPATIBILITY: the deployed `processed_ids.json` still holds the
    old `account:uid` keys. Those are honoured too (see `_is_processed`), so
    deploying this fix does not trigger a mass re-import of everything that
    was already processed.
    """
    if folder is None or str(folder).strip() == "":
        # Legacy form — kept so an old caller still produces a valid key.
        return f"{account_id}:{conv_id}"
    return f"{account_id}:{_normalize_folder_name(folder)}:{conv_id}"


def _legacy_processed_key(account_id, conv_id: str) -> str:
    """The pre-2026-11-04 key form (`account:uid`), still honoured on read."""
    return f"{account_id}:{conv_id}"


def _mark_processed(account_id, conv_id: str, folder: str | None = None) -> None:
    ids = _load_processed_ids()
    ids.add(_processed_key(account_id, conv_id, folder))
    _save_processed_ids(ids)


def _is_processed(account_id, conv_id: str, folder: str | None = None) -> bool:
    """True when the message was already processed.

    Checks the current (folder-scoped) key FIRST, then the legacy
    `account:uid` key, so a deploy does not re-import everything that the
    old key set already covered.
    """
    ids = _load_processed_ids()
    if _processed_key(account_id, conv_id, folder) in ids:
        return True
    return _legacy_processed_key(account_id, conv_id) in ids


def _refresh_oauth_token_if_needed(account_id: int, provider_name: str, current_refresh_token: str, expires_at: datetime | None, expected_access: str | None = None) -> tuple[str, str] | None:
    """Refresh OAuth access token if expired. Returns (new_access_token, new_refresh_token) or None.

    Thin wrapper over the shared helper in mail_oauth.py so the scanner and
    server.py refresh tokens the same way and can never overwrite each
    other's freshly-minted token (the save is a compare-and-swap on the
    token that was read).
    """
    return mail_oauth.refresh_access_token(
        account_id, provider_name, current_refresh_token, expires_at,
        expected_access=expected_access,
    )


def _env_mailbox_config() -> dict[str, Any] | None:
    """Single dev-only mailbox from SCANNER_IMAP_* environment variables.

    These credentials are NOT associated with any `mail_accounts` row, so they
    must never be applied to database accounts: before 2026-11-10 this block
    ran FIRST and returned one config for every scan, which meant the env
    mailbox's credentials were used for the whole CRM whenever they were set.
    They are now only used when the database has no mail accounts at all.
    """
    host = os.environ.get("SCANNER_IMAP_HOST", "")
    port = int(os.environ.get("SCANNER_IMAP_PORT", str(IMAP_PORT_SSL)))
    user = os.environ.get("SCANNER_IMAP_USER", "")
    password = os.environ.get("SCANNER_IMAP_PASSWORD", "")
    inbox = os.environ.get("SCANNER_INBOX", "INBOX")
    if not (host and user and password):
        return None
    return {"host": host, "port": port, "user": user, "password": password,
            "inbox": inbox, "use_ssl": port == IMAP_PORT_SSL, "account_id": None,
            "email": user, "auth_type": "password", "identity_state": "env"}


def _mailbox_cfg_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Build one scanner config from a mail_accounts row, or None to skip it.

    The row's id is carried into the config and every later credential use is
    keyed by it. `mail_identity.sync_gate` refuses an account whose stored
    credentials belong to a different mailbox (or cannot be proven to belong
    to this one) BEFORE a single IMAP command is sent.
    """
    if not row.get("imap_host") or not row.get("email"):
        logger.warning("Skipping mail account %s (%s): missing host or email",
                       row.get("id"), row.get("email") or "?")
        return None
    allowed, why = mail_identity.sync_gate(row, login_user=row.get("email"))
    if not allowed:
        logger.warning("Skipping mail account %s (%s): %s",
                       row.get("id"), row.get("email") or "?", why)
        mail_identity.record_identity_refusal(row.get("id"), why)
        return None
    raw = (row.get("monitored_folders") or "").strip()
    folders = [f.strip() for f in raw.split(",") if f.strip()] if raw else ["INBOX"]
    oauth_provider = row.get("oauth_provider")
    if oauth_provider:
        token_refreshed = _refresh_oauth_token_if_needed(
            row["id"], oauth_provider,
            row.get("oauth_refresh_token") or "",
            row.get("oauth_token_expires"),
            expected_access=row.get("oauth_access_token") or None,
        )
        if token_refreshed:
            access_token = token_refreshed[0]
        else:
            # The refresh failed, or another refresher (the server) won the
            # race and stored a newer token. Re-read the row so we do NOT
            # keep using the stale token we read before the attempt.
            fresh = db.query_one(
                "SELECT oauth_access_token FROM mail_accounts WHERE id = %s",
                (row["id"],),
            )
            access_token = ((fresh or {}).get("oauth_access_token")
                            or row.get("oauth_access_token") or "")
        if not access_token:
            logger.warning("No access token for OAuth account %d (%s), skipping",
                           row["id"], row["email"])
            return None
        return {
            "host": row["imap_host"],
            "port": int(row.get("imap_port", 993)),
            "user": row["email"],
            "email": row["email"],
            "oauth_token": access_token,
            "inbox": folders[0],
            "folders": folders,
            "use_ssl": int(row.get("imap_port", 993)) == IMAP_PORT_SSL,
            "account_id": row["id"],
            "auth_type": "xoauth2",
            "identity_state": "verified",
            "retention_days": int(row.get("retention_days") or 0),
        }
    pwd = row.get("password_encrypted", "")
    if not pwd:
        logger.warning("Skipping mail account %s (%s): no password stored",
                       row.get("id"), row.get("email") or "?")
        return None
    return {
        "host": row["imap_host"],
        "port": int(row.get("imap_port", 993)),
        "user": row["email"],
        "email": row["email"],
        "password": pwd,
        "inbox": folders[0],
        "folders": folders,
        "use_ssl": int(row.get("imap_port", 993)) == IMAP_PORT_SSL,
        "account_id": row["id"],
        "auth_type": "password",
        "identity_state": "login-user",
        "retention_days": int(row.get("retention_days") or 0),
    }


def _get_mailboxes() -> list[dict[str, Any]]:
    # Database accounts first: they are the only per-account credentials.
    try:
        rows = db.query_dicts(
            """SELECT id, email, imap_host, imap_port, password_encrypted,
                      oauth_provider, oauth_access_token, oauth_refresh_token,
                      oauth_token_expires, oauth_identity, sync_enabled,
                      monitored_folders, retention_days
               FROM mail_accounts WHERE sync_enabled = TRUE"""
        )
    except Exception as e:
        logger.error("Failed to load mail accounts: %s", e)
        rows = []
    if not rows:
        env_cfg = _env_mailbox_config()
        if env_cfg:
            logger.warning(
                "No mail_accounts rows: using SCANNER_IMAP_* environment credentials for a "
                "single mailbox. These are never applied to database accounts.")
            return [env_cfg]
        return []

    results = []
    for row in rows:
        cfg = _mailbox_cfg_from_row(row)
        if cfg:
            results.append(cfg)
    return results


class MailboxIdentityError(RuntimeError):
    """Raised before an IMAP login when the config does not belong to one account."""


def _login_mailbox(mailbox_cfg: dict[str, Any]):
    """Open + authenticate one IMAP connection (shared by fetch and STATUS).

    Every credential use goes through here, so the identity assertion lives
    here too (2026-11-10): the login user must be the account's OWN address
    and an XOAUTH2 login must carry a verified identity. A config that fails
    either check is refused before a socket is opened, so one account's
    credentials can never authenticate another account's mailbox.
    """
    user = mailbox_cfg.get("user") or ""
    email = mailbox_cfg.get("email")
    if email and mail_identity.normalize_email(user) != mail_identity.normalize_email(email):
        raise MailboxIdentityError(
            "IMAP login user is not this account's address "
            f"({mail_identity.normalize_email(user)} != {mail_identity.normalize_email(email)})")
    if mailbox_cfg.get("auth_type") == "xoauth2":
        if not mailbox_cfg.get("oauth_token"):
            raise MailboxIdentityError("no OAuth access token for this mailbox")
        if mailbox_cfg.get("identity_state") not in ("verified", "env"):
            raise MailboxIdentityError(
                "the OAuth token's mailbox identity was not verified for this account")
    mailbox = MailBox(mailbox_cfg["host"], port=mailbox_cfg["port"])
    if mailbox_cfg.get("auth_type") == "xoauth2":
        mailbox.xoauth2(mailbox_cfg["user"], mailbox_cfg["oauth_token"])
    else:
        mailbox.login(mailbox_cfg["user"], mailbox_cfg["password"])
    return mailbox


def _get_folder_watermark(account_id: int | None, folder: str) -> tuple[int | None, int | None]:
    """Return (uid_validity, last_seen_uid) for incremental fetch, or (None, None)
    to force the legacy full-window fetch (first run, unknown validity, or
    pre-migration schema — the SELECT fails safe)."""
    if account_id is None:
        return None, None
    try:
        row = db.query_one(
            "SELECT uid_validity, last_seen_uid FROM mail_folders "
            "WHERE imap_account_id = %s AND (imap_path = %s OR name = %s)",
            (account_id, folder, folder),
        )
        if row and row.get("last_seen_uid") is not None:
            return row.get("uid_validity"), int(row["last_seen_uid"])
    except Exception:
        logger.exception("Folder watermark read failed for account %s folder %s", account_id, folder)
    return None, None


def _save_folder_watermark(account_id: int | None, folder: str, validity: int | None, last_uid: int) -> None:
    if account_id is None:
        return
    try:
        row = db.query_one(
            "SELECT id FROM mail_folders WHERE imap_account_id = %s AND (imap_path = %s OR name = %s)",
            (account_id, folder, folder),
        )
        if row:
            db.execute(
                "UPDATE mail_folders SET last_seen_uid = %s, uid_validity = %s, last_sync = NOW() WHERE id = %s",
                (int(last_uid), validity, row["id"]),
            )
        else:
            db.execute(
                "INSERT INTO mail_folders (name, imap_account_id, imap_path, last_seen_uid, uid_validity, last_sync) "
                "VALUES (%s, %s, %s, %s, %s, NOW())",
                (folder, account_id, folder, int(last_uid), validity),
            )
    except Exception:
        logger.exception("Folder watermark save failed for account %s folder %s", account_id, folder)


def _read_folder_status(mailbox_cfg: dict[str, Any], folder: str) -> tuple[int | None, int | None]:
    """Cheap STATUS call: returns (UIDVALIDITY, UIDNEXT) or (None, None)."""
    try:
        mailbox = _login_mailbox(mailbox_cfg)
        try:
            mailbox.folder.set(folder)
            st = mailbox.folder.status()
        finally:
            try:
                mailbox.logout()
            except Exception:
                pass
        return st.get("UIDVALIDITY"), st.get("UIDNEXT")
    except Exception:
        logger.warning("Folder STATUS failed for %s folder %s", mailbox_cfg.get("user"), folder)
        return None, None


def _staging_root() -> Path:
    """The root of the mail-attachment staging area.

    Each sync run gets its OWN subfolder under this root (see
    `_new_staging_run_id`), because accounts poll concurrently.
    """
    return Path(__file__).resolve().parent.parent / MAIL_ATTACHMENT_DIR / ".staging"


def _new_staging_run_id(mailbox_cfg: dict[str, Any] | None = None) -> str:
    """A unique id for this sync run's staging subfolder.

    The account id keeps the folders traceable; the random suffix keeps
    two CONCURRENT runs of the same account (a manual poll racing the
    scheduled cycle) apart.
    """
    account_id = 0
    try:
        account_id = int((mailbox_cfg or {}).get("account_id") or 0)
    except (TypeError, ValueError):
        account_id = 0
    suffix = uuid.uuid4().hex[:12]
    return f"acct{account_id}-{suffix}"


def _sweep_stale_staging_dirs(own_dir: Path) -> None:
    """Remove leftover staging folders from crashed runs.

    Only folders OLDER than `MAIL_ATTACHMENT_STAGING_MAX_AGE` seconds are
    touched, and NEVER the folder of a running sync — a concurrent run's
    folder is young, so it cannot be swept. The run's OWN folder is also
    left alone (it was just created).
    """
    root = _staging_root()
    try:
        now = time.time()
        for entry in list(root.iterdir()):
            if not entry.is_dir():
                continue
            try:
                if entry.resolve() == own_dir.resolve():
                    continue
                age = now - entry.stat().st_mtime
            except Exception:
                continue
            if age < MAIL_ATTACHMENT_STAGING_MAX_AGE:
                continue
            try:
                for stale in entry.iterdir():
                    try:
                        stale.unlink()
                    except Exception:
                        pass
                entry.rmdir()
                logger.info("Removed stale mail attachment staging folder %s (age %.0fs)",
                            entry.name, age)
            except Exception:
                logger.exception("Could not remove stale staging folder %s", entry)
    except Exception:
        logger.exception("Could not sweep stale mail attachment staging folders")


def _remove_staging_dir(staging_dir: Path) -> None:
    """Remove this run's staging folder (best effort)."""
    try:
        if not staging_dir.exists():
            return
        for stale in staging_dir.iterdir():
            try:
                stale.unlink()
            except Exception:
                pass
        staging_dir.rmdir()
    except Exception:
        logger.exception("Could not remove the mail attachment staging folder %s",
                         staging_dir)


def _fetch_messages(mailbox_cfg: dict[str, Any], folder: str | None = None,
                    since: datetime | None = None,
                    uid_min: int | None = None) -> Any:
    """Fetch messages, newest handling unchanged. When uid_min is given, only
    UID > uid_min are fetched (incremental path — seconds, not minutes).

    ⚠ IMAP `n:*` ALWAYS returns the mailbox's highest UID, even when that UID
    is BELOW n (that is the RFC 3501 rule for a `*` range end). So the range
    start is `uid_min + 1` (strictly newer than the watermark) AND every
    returned UID is filtered client-side against `uid_min`. Without the
    client-side filter the overlap row comes back on EVERY poll, which used to
    be dropped by _process_message's already_processed guard — but retention
    deletes that guard's key when it purges the message, so the row was
    re-imported (attachments re-downloaded) every cycle. The filter is
    therefore the primary guard; the processed-ID set is the backstop.

    ⚠ GENERATOR (bug fix 2026-11-03, review round 2).

    This used to build a LIST of every message in the folder, each carrying
    its attachment BYTES. On a first sync, a mailbox renumbering, or a
    watermark reset, that is every message in the sync window in RAM at
    once — a folder full of PDFs could take the CRM down.

    It is now a generator: one message is materialised at a time, and the
    caller (`_poll_account`) processes it immediately. To keep the memory
    bounded, the attachment bytes of a message are written to a STAGING file
    on disk as the message is yielded; the dict then carries only the
    staging path, never the bytes. `_store_attachments` moves the staged
    file into its final place.

    The staging directory is `data/mail-attachments/.staging/<run-id>/`, where
    the run id is unique per sync run (account id + a random suffix). Accounts
    poll CONCURRENTLY (a ThreadPoolExecutor in `_poll_mailboxes`, plus the
    manual per-account workers), so a shared staging folder would let one run
    delete or overwrite another run's staged files. Each run therefore:
      * creates its OWN subfolder,
      * clears ONLY its own subfolder,
      * removes its own subfolder when the run is done.
    A crashed run leaks its own subfolder only; the next run for the same
    account uses a NEW random suffix, so it never touches the leaked one
    (the leftovers are swept by age, see `_sweep_stale_staging_dirs`).

    Newest-first ordering is preserved: IMAP returns UID-ascending, so the
    generator yields in DESCENDING UID order, matching the old
    `messages.sort(key=_uid_key, reverse=True)`.
    """
    # Staging area for attachment bytes that are on their way to disk.
    # OWN subfolder for this run — never shared with a concurrent run.
    run_id = _new_staging_run_id(mailbox_cfg)
    staging_dir = _staging_root() / run_id
    try:
        staging_dir.mkdir(parents=True, exist_ok=True)
        # Sweep leftovers from a PREVIOUS run of the same account (this run's
        # own folder is brand new, so nothing else can be in it).
        _sweep_stale_staging_dirs(staging_dir)
    except Exception:
        logger.exception("Could not prepare the mail attachment staging directory")

    # Collect the UIDs first (cheap: no bodies, no attachments) so the
    # generator can yield newest-first while only ONE message's payload is
    # ever in memory.
    uids: list[int] = []
    mailbox = None
    try:
        mailbox = _login_mailbox(mailbox_cfg)
        folder = _normalize_folder_name(folder or mailbox_cfg.get("inbox", "INBOX"))
        mailbox.folder.set(folder)
        if uid_min is not None:
            # Strictly newer than the watermark: `last_seen + 1`.
            criterion = A(uid=UidRange(str(int(uid_min) + 1), "*"))
        else:
            criterion = A() if since is None else A(date_gte=since.date())
        for msg in mailbox.fetch(criteria=criterion, mark_seen=False, bulk=False):
            try:
                uids.append(int(msg.uid))
            except (TypeError, ValueError):
                continue
        if uid_min is not None:
            # Client-side filter (see the docstring): IMAP's `n:*` returns the
            # HIGHEST UID in the folder even when it is below n, so the server
            # range alone does NOT guarantee "only newer than uid_min".
            # Filtering here happens BEFORE the per-message fetch, so no
            # attachment bytes are downloaded/staged for a stale row.
            before = len(uids)
            uids = [u for u in uids if u > int(uid_min)]
            dropped = before - len(uids)
            if dropped:
                logger.info("Dropped %d message(s) with UID <= %d (IMAP n:* overlap) for folder %s",
                            dropped, int(uid_min), folder)
        uids.sort(reverse=True)
    except Exception as e:
        logger.error("IMAP fetch error: %s", e)
        return
    finally:
        if mailbox is not None:
            try:
                mailbox.logout()
            except Exception:
                pass

    try:
        for uid in uids:
            msg = None
            try:
                msg = _fetch_single_message(mailbox_cfg, folder, uid, staging_dir)
            except Exception:
                logger.exception("Failed to fetch message UID %s", uid)
                continue
            if not msg:
                continue
            yield msg
            # Drop the reference so the payload is released before the next
            # message is fetched.
            del msg
    finally:
        # The run is done (or the caller abandoned it) — remove THIS run's
        # staging folder, and only this run's. A concurrent run's folder is
        # untouched: the folders are separate and the sweep only touches
        # folders older than MAIL_ATTACHMENT_STAGING_MAX_AGE.
        _remove_staging_dir(staging_dir)


def _fetch_single_message(mailbox_cfg: dict[str, Any], folder: str, uid: int,
                          staging_dir: Path | None = None) -> dict[str, Any] | None:
    """Fetch ONE message and stage its attachment bytes on disk.

    `staging_dir` is THIS run's private staging folder (see
    `_new_staging_run_id`). When it is None a fresh private folder is
    created for the call.

    Returns the message dict (attachment entries carry a `staged_path`, not
    the bytes) or None when the message cannot be fetched.
    """
    mailbox = None
    try:
        mailbox = _login_mailbox(mailbox_cfg)
        folder = _normalize_folder_name(folder or mailbox_cfg.get("inbox", "INBOX"))
        mailbox.folder.set(folder)
        msgs = list(mailbox.fetch(criteria=A(uid=str(uid)), mark_seen=False, bulk=False))
        if not msgs:
            return None
        msg = msgs[0]
        attachments = []
        if staging_dir is None:
            staging_dir = _staging_root() / _new_staging_run_id(mailbox_cfg)
        if msg.attachments:
            for att in msg.attachments:
                payload = att.payload if att.payload else b""
                size = len(payload)
                entry = {
                    "filename": att.filename or "unnamed",
                    "mime_type": att.content_type or "application/octet/octet-stream",
                    "size_bytes": size,
                    # imap_tools: att.part is the raw MIME part object (not JSON-serializable);
                    # use content_id which matches the server-side download lookup key.
                    "part_id": att.content_id or "",
                    # A SHORT, stable identifier for the MIME part (NOT str(att.part)
                    # — that is the whole MIME part including the base64 payload).
                    "remote_part_id": _imap_part_id_str(att),
                    # The bytes are staged on disk right away so they never
                    # accumulate in RAM across the folder.
                    "content": b"",
                    "staged_path": "",
                }
                if (MAIL_ATTACHMENT_STORAGE_ENABLED and size and size <= MAIL_ATTACHMENT_MAX_BYTES):
                    try:
                        staging_dir.mkdir(parents=True, exist_ok=True)
                        # The file name is unique within THIS run's folder
                        # (the uid + a per-run token + the index). The folder
                        # itself is already unique per run, so two concurrent
                        # accounts can never collide.
                        staged = staging_dir / f"{uid}_{uuid.uuid4().hex[:8]}_{len(attachments)}_{_safe_attachment_filename(entry['filename'])}"
                        staged.write_bytes(payload)
                        entry["staged_path"] = str(staged)
                    except Exception:
                        logger.exception("Could not stage attachment %r for UID %s",
                                         entry["filename"], uid)
                        entry["staged_path"] = ""
                attachments.append(entry)
        # Build raw headers string from imap_tools headers object
        raw_headers = ""
        message_id = ""
        if hasattr(msg, "headers") and msg.headers:
            mid_val = msg.headers.get("message-id")
            if mid_val:
                if isinstance(mid_val, (tuple, list)):
                    mid_val = ", ".join(str(v) for v in mid_val)
                message_id = str(mid_val).strip("<>")
            header_lines = []
            for key in ("from", "to", "cc", "subject", "date", "message-id", "in-reply-to", "references", "content-type"):
                val = msg.headers.get(key)
                if val:
                    if isinstance(val, (tuple, list)):
                        val = ", ".join(str(v) for v in val)
                    header_lines.append(f"{key}: {val}")
            raw_headers = "\n".join(header_lines)

        return {
            "uid": msg.uid,
            "message_id": message_id,
            "from": msg.from_values.email if msg.from_values else "",
            "from_name": msg.from_values.name if msg.from_values else "",
            "to": msg.to_values[0].email if msg.to_values else "",
            "subject": msg.subject or "",
            # Canonical UTC ISO-8601 with an explicit `Z`. The old value was
            # `msg.date.isoformat()` — an offset-carrying string handed to a
            # timezone-less DB column, which made the stored instant depend on
            # the database session timezone (live bug 2026-11-09).
            "date": mail_dates.iso_utc_z(msg.date) if msg.date else "",
            "body_text": msg.text or "",
            "body_html": msg.html or "",
            "flags": list(msg.flags) if msg.flags else [],
            "folder": folder,
            "attachments": attachments,
            "raw_headers": raw_headers,
        }
    except Exception as e:
        logger.error("IMAP fetch error for UID %s: %s", uid, e)
        return None
    finally:
        if mailbox is not None:
            try:
                mailbox.logout()
            except Exception:
                pass


def _list_imap_folders(mailbox_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    folders = []
    try:
        mailbox = MailBox(mailbox_cfg["host"], port=mailbox_cfg["port"])
        if mailbox_cfg.get("auth_type") == "xoauth2":
            mailbox.xoauth2(mailbox_cfg["user"], mailbox_cfg["oauth_token"])
        else:
            mailbox.login(mailbox_cfg["user"], mailbox_cfg["password"])
        for folder_info in mailbox.folder.list():
            folders.append({
                "name": folder_info.name,
                "flags": list(folder_info.flags) if hasattr(folder_info, 'flags') else [],
            })
        mailbox.logout()
    except Exception as e:
        logger.error("IMAP folder list error: %s", e)
    return folders


_SYSTEM_FOLDER_NORMALIZE = {
    "inbox": "INBOX",
    "sent": "Sent",
    "drafts": "Drafts",
    "trash": "Trash",
    "spam": "Spam",
    "junk": "Junk",
    "archive": "Archive",
    "deleted": "Deleted",
}


def _normalize_folder_name(name: str) -> str:
    """Normalize IMAP folder names so case variants don't create duplicates."""
    lower = name.lower()
    if lower in _SYSTEM_FOLDER_NORMALIZE:
        return _SYSTEM_FOLDER_NORMALIZE[lower]
    return name


_EXCHANGE_DIAGNOSTIC_PREFIXES = ("sync issues", "conversation history", "outbox")


def _is_exchange_diagnostic_folder(name: str) -> bool:
    """Return True for Exchange/Outlook diagnostic folders that should be hidden."""
    lower = name.lower().strip()
    for prefix in _EXCHANGE_DIAGNOSTIC_PREFIXES:
        if lower == prefix or lower.startswith(prefix + "/"):
            return True
    return False


def _sync_imap_folders(mailbox_cfg: dict[str, Any]) -> int:
    imap_folders = _list_imap_folders(mailbox_cfg)
    account_id = mailbox_cfg.get("account_id")
    if not imap_folders or not account_id:
        return 0
    count = 0
    for f in imap_folders:
        raw_name = f["name"]
        if _is_exchange_diagnostic_folder(raw_name):
            continue
        name = _normalize_folder_name(raw_name)
        flags = f.get("flags", [])
        icon = "folder"
        if "\\Sent" in str(flags): icon = "send"
        elif "\\Drafts" in str(flags): icon = "file"
        elif "\\Trash" in str(flags): icon = "trash"
        elif "\\Junk" in str(flags): icon = "alert-triangle"
        elif "\\Archive" in str(flags): icon = "archive"
        existing = db.query_one(
            "SELECT id FROM mail_folders WHERE imap_account_id = %s AND (imap_path = %s OR name = %s)",
            (account_id, raw_name, name),
        )
        if existing:
            db.execute("UPDATE mail_folders SET last_sync = NOW(), icon = %s, name = %s, imap_path = %s WHERE id = %s",
                       (icon, name, raw_name, existing["id"]))
        else:
            db.execute(
                "INSERT INTO mail_folders (name, icon, folder_type, imap_account_id, imap_path) "
                "VALUES (%s, %s, 'imap', %s, %s)",
                (name, icon, account_id, raw_name),
            )
        count += 1
    return count


def _ensure_tag(tag_title: str, created_by: int | None = None) -> int | None:
    existing = db.query_one(
        "SELECT id FROM mail_tags WHERE title = %s",
        (tag_title,),
    )
    if existing:
        return existing["id"]
    result = db.query_one(
        "INSERT INTO mail_tags (title, created_by) VALUES (%s, %s) RETURNING id",
        (tag_title, created_by),
    )
    return result["id"] if result else None


# Claim # custom field is field_key "field_11"; resolve its actual id dynamically so we
# query the real column (field_id) instead of the non-existent "custom_field_id".
_CLAIM_FIELD_KEY = "field_11"


def _claim_field_id() -> int | None:
    try:
        row = db.query_one(
            "SELECT id FROM custom_field_definitions WHERE field_key = %s",
            (_CLAIM_FIELD_KEY,),
        )
        return row["id"] if row else None
    except Exception as e:
        logger.error("Failed to resolve Claim # field id: %s", e)
        return None


def _find_opportunity_by_claim_code(claim_code: str) -> dict[str, Any] | None:
    field_id = _claim_field_id()
    if field_id is None:
        return None
    try:
        rows = db.query_dicts(
            "SELECT id, title FROM opportunities WHERE id IN "
            "(SELECT opportunity_id FROM opportunity_custom_field_values WHERE field_id = %s AND field_value = %s)",
            (field_id, claim_code),
        )
    except Exception as e:
        logger.error("Failed to look up opportunity by claim code: %s", e)
        return None
    if rows:
        return rows[0]
    return None


def _find_opportunity_by_deal_id(deal_id: int) -> dict[str, Any] | None:
    # The number users write in [#NNNN] is the CRM project number shown in the
    # UI header, NOT the internal row id. Resolve by project_number first,
    # then fall back to id. (server.py _resolve_project_id does the same.)
    rows = db.query_dicts(
        "SELECT id, title FROM opportunities WHERE project_number = %s",
        (deal_id,),
    )
    if not rows:
        rows = db.query_dicts(
            "SELECT id, title FROM opportunities WHERE id = %s",
            (deal_id,),
        )
    return rows[0] if rows else None


def _link_email_to_deal(message_id: int, opp_id: int, linked_by: int | None = None) -> bool:
    try:
        existing = db.query_one(
            "SELECT id FROM mail_deal_links WHERE message_id = %s AND opportunity_id = %s",
            (message_id, opp_id),
        )
        if existing:
            return False
        db.execute(
            "INSERT INTO mail_deal_links (message_id, opportunity_id, linked_by_user_id) VALUES (%s, %s, %s)",
            (message_id, opp_id, linked_by),
        )
        return True
    except Exception as e:
        logger.error("Failed to link email %d to deal %d: %s", message_id, opp_id, e)
        return False


def _add_tag_to_message(message_id: int, tag_title: str, assigned_by: int | None = None) -> bool:
    tag_id = _ensure_tag(tag_title, assigned_by)
    if tag_id is None:
        return False
    try:
        existing = db.query_one(
            "SELECT id FROM mail_tag_assignments WHERE message_id = %s AND tag_id = %s",
            (message_id, tag_id),
        )
        if existing:
            return False
        db.execute(
            "INSERT INTO mail_tag_assignments (message_id, tag_id, assigned_by) VALUES (%s, %s, %s)",
            (message_id, tag_id, assigned_by),
        )
        return True
    except Exception as e:
        logger.error("Failed to tag message %d with %s: %s", message_id, tag_title, e)
        return False


# Mirrors server.py:EMAIL_HISTORY_CATEGORY_ID. The scanner cannot import
# server (server imports this module), so the two constants must be kept in
# sync by hand. Both are the "Email" history category.
EMAIL_HISTORY_CATEGORY_ID = 13


def _copy_mail_attachments_to_project(opp_id: int, event_id: int, message_id: int) -> int:
    """Copy a linked email's attachments into the PROJECT's own storage.

    OnlyOffice parity (live requirement 2026-10-29): linking an email to a
    project must MATERIALIZE the email and its files in the project, so
    removing the mail account later does not remove access to them, and every
    user who can open the project can read them — whether or not the account
    is in their mail modal.

    Before this, the timeline only REFERENCED the mail attachments: they were
    resolved live from mail_attachments at read time and served through the
    mail download route (gated by mail-account access). Deleting the mail
    account cascaded mail_attachments away and the project's files vanished.

    Source of the bytes, in order:
      1. the local store (data/mail-attachments/<message_id>/<file>) — the
         scanner now writes the bytes here at SYNC time, so the common case
         needs no mailbox round-trip at all
      2. a live mailbox download — fallback for attachments that were never
         stored locally (sent mail mirrored differently, oversized files that
         were skipped, mail that predates the sync-time storage)

    A failure to copy one file never fails the link, but it is now LOGGED
    (message id, attachment id, filename, reason) instead of vanishing.

    Idempotent (bug fix 2026-11-03): a `history_attachments` row carries the
    source `mail_attachment_id`, and the unique index on
    (event_id, mail_attachment_id) makes a second copy of the same
    attachment a no-op. Relinking or re-running the backfill therefore
    never doubles a file in the project's Documents tab.

    Returns the number of files copied (see
    `copy_mail_attachments_to_project_detailed` for the full report).
    """
    report = copy_mail_attachments_to_project_detailed(opp_id, event_id, message_id)
    return report["copied"]


def _find_existing_project_copy(opp_id: int, event_id: int, att_id: Any,
                                filename: str, size_bytes: Any) -> dict | None:
    """Is this attachment already copied into the project? (dedupe)

    Bug fix 2026-11-03 (review round 2): the original dedupe only looked at
    `history_attachments.mail_attachment_id`, which is NULL for every file
    copied BEFORE migration 043. A relink or a backfill therefore copied
    those files a second time — the project ended up with 2 timeline rows
    and 2 Documents rows for the same PDF.

    The check now recognises a copy by any of:
      1. `mail_attachment_id` — the exact key (post-043 copies);
      2. the same filename + size in this event — a legacy copy;
      3. the same filename + size in another event of the SAME project —
         the mail was linked to this project before.

    Returns the matching row, or None.
    """
    if att_id is not None:
        try:
            row = db.query_one(
                "SELECT id FROM history_attachments WHERE event_id = %s AND mail_attachment_id = %s",
                (event_id, att_id),
            )
            if row:
                return row
        except Exception:
            # Column missing (migration 043 not applied yet) — fall through
            # to the filename/size checks below.
            pass
    # Legacy copy: same filename AND same size. The size matters — a file
    # with the same name but a different size is a DIFFERENT file and must
    # still be copied.
    try:
        if size_bytes is not None:
            row = db.query_one(
                "SELECT id FROM history_attachments WHERE event_id = %s AND filename = %s "
                "AND file_size = %s",
                (event_id, filename, int(size_bytes)),
            )
            if row:
                return row
            # Another event of the same project may already hold this file.
            row = db.query_one(
                "SELECT ha.id FROM history_attachments ha "
                "JOIN history_events he ON he.id = ha.event_id "
                "WHERE he.opportunity_id = %s AND ha.event_id <> %s "
                "AND ha.filename = %s AND ha.file_size = %s LIMIT 1",
                (opp_id, event_id, filename, int(size_bytes)),
            )
            if row:
                return row
        else:
            # No size known — fall back to the filename alone.
            row = db.query_one(
                "SELECT id FROM history_attachments WHERE event_id = %s AND filename = %s",
                (event_id, filename),
            )
            if row:
                return row
    except Exception:
        logger.exception("Could not check for an existing project copy of %r", filename)
        return None
    return None


def copy_mail_attachments_to_project_detailed(opp_id: int, event_id: int,
                                               message_id: int) -> dict[str, Any]:
    """Copy a linked email's attachments into the project (detailed report).

    Returns a dict:
        copied   — files newly written into the project
        total    — attachments considered for this message
        skipped  — files that could NOT be copied (with reasons)
        existing — files already present (idempotent no-op)
    """
    report: dict[str, Any] = {"copied": 0, "total": 0, "skipped": [],
                              "existing": 0, "message_id": message_id,
                              "opportunity_id": opp_id, "event_id": event_id}
    try:
        att_rows = db.query_dicts(
            "SELECT id, filename, mime_type, size_bytes, stored_path, content_hash "
            "FROM mail_attachments WHERE message_id = %s ORDER BY filename",
            (message_id,),
        )
    except Exception:
        logger.exception("Failed to list attachments for message %d", message_id)
        report["skipped"].append({"attachment_id": None, "filename": "",
                                  "reason": "could not list attachments for message"})
        return report
    report["total"] = len(att_rows)
    if not att_rows:
        return report

    root = Path(__file__).resolve().parent.parent
    dest_dir = root / "data" / "history-attachments" / str(opp_id) / str(event_id)
    for att in att_rows:
        att_id = att.get("id")
        filename = att.get("filename") or "attachment"
        # ── Idempotency ────────────────────────────────────────────────────
        # Three ways an attachment can already be in this event:
        #   1. the row carries `mail_attachment_id` (copied after migration
        #      043) — the exact key;
        #   2. a LEGACY copy (written before 043) has no
        #      `mail_attachment_id`; it must still be recognised, or a
        #      relink/backfill would duplicate every old file;
        #   3. the same file copied into this project from a different
        #      timeline event (e.g. the mail was linked twice) — matched on
        #      filename + size.
        existing = _find_existing_project_copy(opp_id, event_id, att_id, filename,
                                                att.get("size_bytes"))
        if existing:
            report["existing"] += 1
            continue

        data: bytes | None = None
        reason = ""
        stored = (att.get("stored_path") or "").strip()
        if stored:
            full = root / stored
            if full.is_file():
                try:
                    data = full.read_bytes()
                except Exception as exc:
                    reason = f"local file unreadable: {exc}"
            else:
                reason = "local file missing on disk"
        if data is None:
            # Fallback: pull the bytes from the mailbox.
            data, reason = _fetch_mail_attachment_bytes(message_id, att_id)
        if not data:
            logger.warning(
                "Mail attachment skipped: message=%s attachment=%s filename=%r reason=%s",
                message_id, att_id, filename, reason or "unknown")
            report["skipped"].append({"attachment_id": att_id, "filename": filename,
                                      "reason": reason or "attachment bytes unavailable"})
            continue
        safe = _safe_attachment_filename(filename)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{int(att_id)}_{safe}"
            dest.write_bytes(data)
            rel = dest.relative_to(root)
            rel_str = str(rel)
            mime = att.get("mime_type") or "application/octet-stream"
            # `mail_attachment_id` is the dedupe key (migration 043), and a
            # double-click (two concurrent link requests) must not create a
            # second row.
            #
            # ⚠ `ON CONFLICT (event_id, mail_attachment_id) WHERE …` is
            # REJECTED BY POSTGRES ("no unique or exclusion constraint
            # matching the ON CONFLICT specification") when migration 043
            # never created uq_history_attachments_mail_attachment. That is a
            # hard error — not a "no conflict happened" case — so the
            # statement must not be sent at all when the index is absent
            # (blocker B1). In that state we check for an existing row first.
            inserted = None
            if _unique_index_exists(HISTORY_ATTACHMENTS_UNIQUE_INDEX):
                inserted = db.execute(
                    "INSERT INTO history_attachments (event_id, filename, file_path, file_size, "
                    "mime_type, uploaded_by, mail_attachment_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (event_id, mail_attachment_id) WHERE mail_attachment_id IS NOT NULL "
                    "DO NOTHING",
                    (event_id, filename, rel_str, len(data), mime, None, att_id),
                )
            else:
                existing = db.query_one(
                    "SELECT id FROM history_attachments WHERE event_id = %s "
                    "AND mail_attachment_id = %s",
                    (event_id, att_id))
                if not existing:
                    inserted = db.execute(
                        "INSERT INTO history_attachments (event_id, filename, file_path, file_size, "
                        "mime_type, uploaded_by, mail_attachment_id) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (event_id, filename, rel_str, len(data), mime, None, att_id),
                    )
            if not inserted:
                # Another copy won the race (or the row was already there) —
                # do not add a second document.
                logger.info("Mail attachment %s for message %s was already copied "
                            "into event %s — skipping the duplicate",
                            att_id, message_id, event_id)
                try:
                    dest.unlink(missing_ok=True)
                except Exception:
                    pass
                report["existing"] += 1
                continue
            # Also register the file as a project document so it opens in the
            # document editor and appears in the project's Documents tab —
            # the same shape the imported attachments have.
            #
            # ⚠ Same B1 rule as above: migration 044 SKIPS
            # uq_project_documents_opp_file_path when project_documents
            # already holds duplicates, and an `ON CONFLICT (opportunity_id,
            # file_path) WHERE …` statement is rejected outright in that
            # state. Use the index only when it exists; otherwise check for
            # an existing active row first.
            try:
                if _unique_index_exists(PROJECT_DOCUMENTS_UNIQUE_INDEX):
                    db.execute(
                        "INSERT INTO project_documents (opportunity_id, title, file_path, file_size, mime_type, uploaded_by) "
                        "VALUES (%s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (opportunity_id, file_path) WHERE is_deleted = FALSE DO NOTHING",
                        (opp_id, filename, rel_str, len(data), mime, None),
                    )
                else:
                    already_doc = db.query_one(
                        "SELECT id FROM project_documents WHERE opportunity_id = %s "
                        "AND file_path = %s AND is_deleted = FALSE LIMIT 1",
                        (opp_id, rel_str))
                    if not already_doc:
                        db.execute(
                            "INSERT INTO project_documents (opportunity_id, title, file_path, file_size, mime_type, uploaded_by) "
                            "VALUES (%s, %s, %s, %s, %s, %s)",
                            (opp_id, filename, rel_str, len(data), mime, None),
                        )
            except Exception:
                logger.exception("Failed to register project document for %s", filename)
            report["copied"] += 1
        except Exception as exc:
            logger.exception("Failed to copy mail attachment %s into project %s",
                             filename, opp_id)
            logger.warning(
                "Mail attachment skipped: message=%s attachment=%s filename=%r reason=copy failed: %s",
                message_id, att_id, filename, exc)
            report["skipped"].append({"attachment_id": att_id, "filename": filename,
                                      "reason": f"copy failed: {exc}"})
    return report


def _refresh_mail_account_token(account_id: int) -> None:
    """Refresh the mailbox OAuth token the same way the scanner's poll loop does.

    Bug fix 2026-11-03: the link-time mailbox download used a STALE
    `oauth_access_token`, so on 2026-11-02 every download for Outlook
    account 10 failed (expired / throttled token) and the linked emails
    silently lost their files.

    Never logs the token itself.
    """
    try:
        row = db.query_one(
            "SELECT id, email, oauth_provider, oauth_access_token, oauth_refresh_token, "
            "oauth_token_expires FROM mail_accounts WHERE id = %s",
            (account_id,),
        )
    except Exception:
        logger.exception("Could not load mail account %s for token refresh", account_id)
        return
    if not row:
        return
    provider_name = row.get("oauth_provider")
    if not provider_name:
        return
    try:
        refreshed = _refresh_oauth_token_if_needed(
            row["id"], provider_name,
            row.get("oauth_refresh_token") or "",
            row.get("oauth_token_expires"),
        )
    except Exception:
        logger.exception("Token refresh raised for mail account %s", account_id)
        return
    if refreshed:
        logger.info("Refreshed mailbox token for account %s (%s)", account_id,
                    row.get("email") or "?")
    else:
        logger.warning("Could not refresh mailbox token for account %s (%s) — "
                       "the link-time download will use the existing token",
                       account_id, row.get("email") or "?")


def _fetch_mail_attachment_bytes(message_id: int, attachment_id: int | None) -> tuple[bytes | None, str]:
    """Pull attachment bytes straight from the mailbox (live IMAP fetch).

    Fallback for attachments that are not in the local store (mail synced
    before the sync-time storage existed, or files above the size limit).

    Returns (bytes, reason). `bytes` is None on failure and `reason` explains
    why — the caller logs it. A reason never contains a token or password.

    Retries with backoff (MAIL_ATTACHMENT_FETCH_RETRIES attempts,
    MAIL_ATTACHMENT_FETCH_BACKOFF base seconds) because the prod failure was
    transient Graph/IMAP throttling.

    The mailbox OAuth token is refreshed BEFORE THE FIRST ATTEMPT too, not
    only before the retries: the 2026-11-02 prod failure was a stale token,
    so waiting for a retry to discover that would waste an attempt (and the
    first attempt's failure was the one the user saw).

    A `Retry-After` hint from the server (429 / 503) is honoured instead of
    the plain backoff.

    The scanner cannot import server (server imports this module), so the
    IMAP connection is made here from the same mail_accounts row.
    """
    last_reason = "attachment bytes unavailable"
    # Refresh the token BEFORE the first attempt.
    _refresh_token_for_message(message_id)
    for attempt in range(1, MAIL_ATTACHMENT_FETCH_RETRIES + 1):
        if attempt > 1:
            delay = MAIL_ATTACHMENT_FETCH_BACKOFF * attempt
            logger.info("Retrying mailbox attachment fetch for message %s attachment %s "
                        "in %.1fs (attempt %d/%d)",
                        message_id, attachment_id, delay, attempt, MAIL_ATTACHMENT_FETCH_RETRIES)
            time.sleep(delay)
            # Refresh the token before retrying — the usual cause of a
            # sudden "everything fails" on one account.
            _refresh_token_for_message(message_id)
        data, reason, retry_after = _fetch_mail_attachment_bytes_once(message_id, attachment_id)
        if data:
            return data, ""
        last_reason = reason
        # Honour the server's Retry-After (429 / 503) instead of the plain
        # backoff — it is the authoritative wait time.
        if retry_after:
            try:
                wait = float(retry_after)
            except (TypeError, ValueError):
                wait = 0.0
            if wait > 0:
                logger.info("Mailbox asked to retry after %.1fs (message %s attachment %s)",
                            wait, message_id, attachment_id)
                time.sleep(min(wait, 120.0))
    return None, last_reason


def _refresh_token_for_message(message_id: int) -> None:
    """Refresh the mailbox token for the account that owns this message."""
    try:
        msg = db.query_one(
            "SELECT account_id FROM mail_messages WHERE id = %s", (message_id,))
        if msg and msg.get("account_id"):
            _refresh_mail_account_token(int(msg["account_id"]))
    except Exception:
        logger.exception("Token refresh before fetch failed for message %s", message_id)


def _fetch_mail_attachment_bytes_once(message_id: int,
                                      attachment_id: int | None) -> tuple[bytes | None, str, float | None]:
    """One mailbox fetch attempt for a single attachment.

    Returns (bytes, reason, retry_after). `retry_after` is the server's
    Retry-After hint in seconds when the failure was a 429/503, else None.
    """
    try:
        msg = db.query_one(
            "SELECT m.imap_uid, m.folder, m.account_id FROM mail_messages m WHERE m.id = %s",
            (message_id,),
        )
        if not msg:
            return None, "message not found", None
        att = db.query_one(
            "SELECT filename, imap_part_id, remote_part_id FROM mail_attachments WHERE id = %s",
            (attachment_id,),
        )
        if not att:
            return None, "attachment row not found", None
        acct = db.query_one(
            "SELECT id, email, imap_host, imap_port, password_encrypted, oauth_provider, "
            "oauth_access_token, oauth_identity, oauth_graph_identity, smtp_user "
            "FROM mail_accounts WHERE id = %s",
            (msg["account_id"],),
        )
        if not acct or not acct.get("imap_host"):
            return None, "mail account has no IMAP host configured", None
        # Same identity gate as the poll loop: a message row's account id
        # selects the credentials, and those credentials must belong to that
        # mailbox before we authenticate with them.
        allowed, why = mail_identity.sync_gate(acct, login_user=acct.get("email"))
        if not allowed:
            mail_identity.record_identity_refusal(acct.get("id"), why)
            return None, why, None
        from imap_tools import A, MailBox
        port = int(acct.get("imap_port") or 993)
        mailbox = MailBox(acct["imap_host"], port=port)
        try:
            if acct.get("oauth_provider"):
                token = acct.get("oauth_access_token") or ""
                if not token:
                    return None, "mail account has no OAuth access token", None
                mailbox.xoauth2(acct["email"], token)
            else:
                pwd = acct.get("password_encrypted") or ""
                if not pwd:
                    return None, "mail account has no password stored", None
                mailbox.login(acct["email"], pwd)
            mailbox.folder.set(msg["folder"] or "INBOX")
            uid = str(msg["imap_uid"])
            msgs = list(mailbox.fetch(criteria=A(uid=uid), mark_seen=False, bulk=False))
            if not msgs:
                return None, f"message {uid} not found in folder {msg['folder'] or 'INBOX'}", None
            imap_msg = msgs[0]
            # The short MIME-part identifier (remote_part_id) selects the
            # right part. `imap_part_id` holds a Content-ID, which never
            # matches `att_obj.part` — that was the side bug fixed on
            # 2026-11-03.
            part_id = str(att.get("remote_part_id") or "").strip()
            content_id = str(att.get("imap_part_id") or "").strip()
            filename = att.get("filename") or ""
            fallback_payload = None
            for att_obj in (imap_msg.attachments or []):
                obj_part = _imap_part_id_str(att_obj)
                if part_id and obj_part and obj_part == part_id:
                    return att_obj.payload, "", None
                # Last-resort: match on the Content-ID we recorded at sync time.
                if content_id:
                    obj_cid = str(getattr(att_obj, "content_id", "") or "").strip()
                    if obj_cid and obj_cid == content_id:
                        return att_obj.payload, "", None
                # Last-resort: filename.
                if filename and att_obj.filename == filename and fallback_payload is None:
                    fallback_payload = att_obj.payload
            if fallback_payload is not None:
                return fallback_payload, "", None
            return None, "attachment part not present in the mailbox copy", None
        finally:
            try:
                mailbox.logout()
            except Exception:
                pass
    except Exception as exc:
        logger.exception("Live mailbox fetch failed for message %d attachment %s",
                         message_id, attachment_id)
        return None, f"mailbox fetch failed: {exc}", _retry_after_from_exception(exc)


def _retry_after_from_exception(exc: BaseException) -> float | None:
    """The Retry-After hint (seconds) from a 429/503 error, if any.

    imap_tools raises its own exceptions; when the server sent a
    Retry-After header the value is on the exception (or on its `response`).
    Anything unrecognised returns None (the caller then uses the plain
    backoff).
    """
    for attr in ("retry_after", "retry_after_seconds"):
        val = getattr(exc, attr, None)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        try:
            raw = headers.get("Retry-After") or headers.get("retry-after")
            if raw:
                return float(raw)
        except Exception:
            pass
    text = str(exc)
    # imap_tools' IMAP4 response errors carry the server response text.
    if "429" in text or "503" in text or "TOOBUSY" in text.upper():
        # No explicit Retry-After — signal "throttled" with a small hint so
        # the caller waits a little longer than the plain backoff.
        return 0.0
    return None


def create_email_history_snapshot(opp_id: int, message_id: int, user_id: int | None = None) -> bool:
    """Create the project-timeline event for a linked email (idempotent).

    Shared by the server's manual-link path and every scanner auto-link path
    (live gap 2026-10-28: auto-links wrote only mail_deal_links, so linked
    mail never appeared in project history). Returns True when an event now
    exists (created or already present).

    The event carries the full email body inline (so every project user can
    read it without the mail account) AND copies the attachments into the
    project's own storage (so the files survive the mail account going away).

    Bug fix 2026-11-03: when the event ALREADY existed the function returned
    early and the attachment copy never ran — so relinking an email whose
    files were missing (the 2026-11-02 Outlook failures) could not repair
    anything. The copy now runs on BOTH paths; it is idempotent, so an
    already-copied file is a no-op.
    """
    try:
        existing = db.query_one(
            # ⚠ EXACT match: a LIKE '%"message_id": 12%' also matches
            # message_id 120 and 1200 (the prefix is a substring). The regex
            # requires a delimiter (',' or '}') right after the id.
            "SELECT id FROM history_events WHERE opportunity_id = %s "
            "AND content ~ %s",
            (opp_id, '"message_id": ' + str(int(message_id)) + '([,}])'),
        )
        if existing:
            # Repair path: the timeline entry exists, but its files may not
            # (a previous copy attempt failed). The copy is idempotent.
            try:
                _copy_mail_attachments_to_project(opp_id, int(existing["id"]), message_id)
            except Exception:
                logger.exception("Failed to repair attachments for email %d in project %s",
                                 message_id, opp_id)
            return True
        msg = db.query_one(
            "SELECT subject, from_addr, to_addr, cc_addr, body_html, body_text, date_received, account_id FROM mail_messages WHERE id = %s",
            (message_id,),
        )
        if not msg:
            return False
        try:
            att_rows = db.query_dicts(
                "SELECT filename, size_bytes, mime_type FROM mail_attachments WHERE message_id = %s",
                (message_id,),
            )
            att_snapshot = [
                {"filename": a["filename"], "size_bytes": a["size_bytes"] or 0, "mime_type": a["mime_type"] or ""}
                for a in att_rows
            ]
        except Exception:
            att_snapshot = []
        acct_row = (db.query_one(
            "SELECT email FROM mail_accounts WHERE id = %s", (msg.get("account_id"),))
            if msg.get("account_id") else None)
        snapshot = {
            "type": "email_snapshot",
            "message_id": message_id,
            "account_id": msg.get("account_id"),
            "account_email": (acct_row or {}).get("email") or "",
            "from": msg["from_addr"] or "",
            "to": msg["to_addr"] or "",
            "cc": msg["cc_addr"] or "",
            "subject": msg["subject"] or "",
            "date_sent": mail_dates.iso_utc(msg["date_received"]),
            "body_html": msg["body_html"] or "",
            "body_text": msg["body_text"] or "",
            "attachments": att_snapshot,
        }
        event_id = db.insert_returning(
            "INSERT INTO history_events (opportunity_id, category_id, content, created_by) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (opp_id, EMAIL_HISTORY_CATEGORY_ID, json.dumps(snapshot), user_id),
        )
        if event_id:
            # Materialize the files in the project (see the helper docstring).
            try:
                _copy_mail_attachments_to_project(opp_id, event_id, message_id)
            except Exception:
                logger.exception("Failed to copy attachments for email %d into project %s",
                                 message_id, opp_id)
        return True
    except Exception:
        logger.exception("Failed to create history event for email %d", message_id)
        return False


def create_email_history_snapshot_detailed(opp_id: int, message_id: int,
                                           user_id: int | None = None) -> dict[str, Any]:
    """Same as `create_email_history_snapshot` but returns the copy report.

    The link API needs to tell the user how many of the email's files made
    it into the project ("3 of 4 files copied"), because a partial copy used
    to be reported as plain success.
    """
    result: dict[str, Any] = {
        "history_created": False, "copied": 0, "total": 0, "existing": 0,
        "skipped": [], "event_id": None,
    }
    try:
        existing = db.query_one(
            # ⚠ EXACT match: a LIKE '%"message_id": 12%' also matches
            # message_id 120 and 1200 (the prefix is a substring). The regex
            # requires a delimiter (',' or '}') right after the id.
            "SELECT id FROM history_events WHERE opportunity_id = %s "
            "AND content ~ %s",
            (opp_id, '"message_id": ' + str(int(message_id)) + '([,}])'),
        )
        event_id = int(existing["id"]) if existing else None
        if not event_id:
            msg = db.query_one(
                "SELECT subject, from_addr, to_addr, cc_addr, body_html, body_text, date_received "
                "FROM mail_messages WHERE id = %s",
                (message_id,),
            )
            if not msg:
                return result
            try:
                att_rows = db.query_dicts(
                    "SELECT filename, size_bytes, mime_type FROM mail_attachments WHERE message_id = %s",
                    (message_id,),
                )
                att_snapshot = [
                    {"filename": a["filename"], "size_bytes": a["size_bytes"] or 0,
                     "mime_type": a["mime_type"] or ""}
                    for a in att_rows
                ]
            except Exception:
                att_snapshot = []
            snapshot = {
                "type": "email_snapshot",
                "message_id": message_id,
                "from": msg["from_addr"] or "",
                "to": msg["to_addr"] or "",
                "cc": msg["cc_addr"] or "",
                "subject": msg["subject"] or "",
                "date_sent": mail_dates.iso_utc(msg["date_received"]),
                "body_html": msg["body_html"] or "",
                "body_text": msg["body_text"] or "",
                "attachments": att_snapshot,
            }
            event_id = db.insert_returning(
                "INSERT INTO history_events (opportunity_id, category_id, content, created_by) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (opp_id, EMAIL_HISTORY_CATEGORY_ID, json.dumps(snapshot), user_id),
            )
            result["history_created"] = bool(event_id)
        if not event_id:
            return result
        result["event_id"] = int(event_id)
        report = copy_mail_attachments_to_project_detailed(opp_id, int(event_id), message_id)
        result["copied"] = report["copied"]
        result["total"] = report["total"]
        result["existing"] = report["existing"]
        result["skipped"] = report["skipped"]
        return result
    except Exception:
        logger.exception("Failed to create history event for email %d", message_id)
        return result


def _post_note_to_deal(opp_id: int, content: str, notify_users: list[str] | None = None, category_id: int = 16) -> bool:
    try:
        db.execute(
            "INSERT INTO history_events (opportunity_id, category_id, content, created_by) "
            "VALUES (%s, %s, %s, %s)",
            (opp_id, category_id, content, None),
        )
        return True
    except Exception as e:
        logger.error("Failed to post note to deal %d: %s", opp_id, e)
        return False


def _evaluate_classification_rules(subject: str, body: str, from_email: str) -> dict[str, Any] | None:
    """Evaluate user-defined classification rules from DB. Returns first matching rule or None."""
    try:
        rules = db.query_dicts(
            "SELECT * FROM mail_classification_rules WHERE enabled = TRUE ORDER BY priority DESC, id"
        )
    except Exception:
        return None
    for rule in rules:
        rule_type = rule.get("rule_type", "")
        pattern = rule.get("pattern", "")
        action = rule.get("action", "tag")
        action_target = rule.get("action_target", "")
        matched = False
        try:
            if rule_type == "subject_regex":
                matched = bool(re.search(pattern, subject, re.IGNORECASE))
            elif rule_type == "sender_domain":
                sender_domain = from_email.split("@")[-1].lower() if "@" in from_email else ""
                matched = pattern.lower() in sender_domain
            elif rule_type == "body_regex":
                matched = bool(re.search(pattern, body[:5000], re.IGNORECASE))
        except re.error:
            logger.warning("Invalid regex in classification rule %s: %s", rule.get("id"), pattern)
            continue
        if matched:
            result = {
                "classification": f"rule_{rule.get('id')}",
                "action": action,
                "rule_id": rule.get("id"),
                "rule_name": rule.get("rule_name"),
            }
            if action == "link" and action_target:
                try:
                    deal_id = int(action_target)
                    result["linked_deal_id"] = deal_id
                except ValueError:
                    pass
            return result
    return None


def _classify_message(msg: dict[str, Any]) -> dict[str, Any]:
    subject = msg.get("subject") or ""
    body_text = msg.get("body_text") or ""
    body_html = msg.get("body_html") or ""
    body = body_text or _sanitize_body(body_html)
    from_email = msg.get("from") or ""
    from_name = msg.get("from_name") or ""
    uid = msg.get("uid") or ""

    classification = "uncertain"
    match_strength = "weak"
    action = "link_only"
    linked_deal_id: int | None = None
    # Which sub-pipeline produced the link: "project_id" ([#123]) or
    # "claim_code" (claim # matching). The link-application step honors the
    # matching toggle — a checked [#PROJECTID] box must never license claim
    # matching and vice versa.
    link_method: str | None = None
    deal = None

    # Evaluate user-defined classification rules first
    rule_result = _evaluate_classification_rules(subject, body, from_email)
    if rule_result:
        classification = rule_result["classification"]
        match_strength = "rule_match"
        action = rule_result["action"]
        if rule_result.get("linked_deal_id"):
            linked_deal_id = rule_result["linked_deal_id"]

    # Check auto-link toggle. toggles.get(...) returns a config DICT which is
    # always truthy regardless of `enabled`; we must read the key explicitly.
    config = get_contractors()
    toggles = config.get("scanner_behavior", {})
    auto_link_enabled = bool(
        (toggles.get("auto_link_project_id") or {}).get("enabled", False)
    )
    # Claim-code matching is a SEPARATE behavior with its own toggle
    # (auto_link_claim_code, default off). It must never ride along with the
    # [#PROJECTID] box.
    claim_link_enabled = bool(
        (toggles.get("auto_link_claim_code") or {}).get("enabled", False)
    )

    # Try [#PROJECTID] format first (DEAL_LINK_RE)
    if auto_link_enabled and classification == "uncertain":
        link_match = DEAL_LINK_RE.search(subject)
        if not link_match:
            link_match = DEAL_LINK_RE.search(body[:5000])
        if link_match:
            project_id = int(link_match.group(1))
            deal = _find_opportunity_by_deal_id(project_id)
            if deal:
                classification = "project_id_deal_link"
                match_strength = "strong"
                action = "link_deal"
                linked_deal_id = deal["id"]
                link_method = "project_id"

    # Try [#DEAL-NNN] format (DEAL_ID_RE) — claim-code behavior only.
    if claim_link_enabled and classification == "uncertain":
        claim_match = None
        for m in DEAL_ID_RE.finditer(subject):
            claim_match = m.group(1)
            break

        if claim_match:
            deal = _find_opportunity_by_claim_code(claim_match)
            if deal:
                classification = "claim_code_deal_link"
                match_strength = "strong"
                action = "link_deal"
                linked_deal_id = deal["id"]
                link_method = "claim_code"
            else:
                for m in DEAL_ID_RE.finditer(body):
                    deal = _find_opportunity_by_claim_code(m.group(1))
                    if deal:
                        classification = "claim_code_deal_link"
                        match_strength = "strong"
                        action = "link_deal"
                        linked_deal_id = deal["id"]
                        link_method = "claim_code"
                        break

    # These two fallthroughs previously ran with NO toggle gate at all — any
    # bare claim-like subject linked whenever ANY link behavior was on. They
    # belong to the claim-code behavior.
    if claim_link_enabled and classification == "uncertain" and DEAL_ID_RE.search(subject):
        classification = "claim_code_deal_link"
        match_strength = "medium"
        action = "link_deal"
        link_method = "claim_code"

    if claim_link_enabled and classification == "uncertain" and CLAIM_CODE_RE.match(subject.strip()):
        deal = _find_opportunity_by_claim_code(subject.strip())
        if deal:
            classification = "claim_code_deal_link"
            match_strength = "strong"
            action = "link_deal"
            linked_deal_id = deal["id"]
            link_method = "claim_code"
        else:
            classification = "claim_code_no_deal"
            match_strength = "medium"
            action = "link_only"

    ml_result = None
    ml_applied = False
    if classification == "uncertain":
        _init_ml()
        ml_result = _ml_classify(subject, body)
        if ml_result and ml_result.get("used_ml"):
            # Apply ML override if models agree
            logistic = ml_result.get("logistic")
            knn = ml_result.get("knn")
            if logistic == knn and logistic == "linked":
                classification = "ml_suggested_link"
                ml_applied = True

    return {
        "classification": classification,
        "match_strength": match_strength,
        "action": action,
        "linked_deal_id": linked_deal_id,
        "link_method": link_method,
        "ml_result": ml_result,
        "ml_applied": ml_applied,
        "subject": subject,
        "from_email": from_email,
        "from_name": from_name,
        "uid": uid,
        "body_preview": body[:500],
    }


def _clean_nul_str(value: Any) -> Any:
    """Strip NUL bytes: Postgres text cannot store \\x00 and the whole INSERT
    dies on a single contaminated field (live case: crm@ messages silently
    skipped every cycle). Non-strings pass through untouched."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    return value


def _store_message(msg: dict[str, Any], mailbox_cfg: dict[str, Any]) -> int | None:
    try:
        account_id = mailbox_cfg.get("account_id")
        uid_key = str(msg.get("uid") or "")
        folder = _normalize_folder_name(msg.get("folder") or "INBOX")
        message_id_val = (msg.get("message_id") or "").strip()
        if account_id is not None and message_id_val:
            # Message-ID reconciliation (2026-11-10). UIDVALIDITY changes
            # renumber UIDs, so a re-import must key on the stable Message-ID
            # instead of creating a second copy of the same email.
            prior = db.query_one(
                "SELECT id FROM mail_messages WHERE account_id = %s AND message_id = %s LIMIT 1",
                (account_id, message_id_val),
            )
            if prior:
                try:
                    db.execute(
                        "UPDATE mail_messages SET imap_uid = %s, folder = %s WHERE id = %s",
                        (uid_key, folder, prior["id"]),
                    )
                except Exception:
                    logger.debug("Could not refresh UID for message %s", prior["id"])
                return prior["id"]
        if account_id is not None and folder == "Sent":
            # Skip the IMAP copy of a message the CRM stored when it was sent
            # (synthetic uid sent:outgoing:*). The CRM copy has a generated
            # Message-ID, so subject+recipient+time is the stable join.
            try:
                sent_date = mail_dates.parse_email_date(msg.get("date"))
                if sent_date is not None:
                    dupe = db.query_one(
                        """SELECT id FROM mail_messages
                            WHERE account_id = %s AND folder = 'Sent'
                              AND imap_uid LIKE 'sent:outgoing:%%'
                              AND lower(coalesce(subject, '')) = lower(%s)
                              AND lower(coalesce(to_addr, '')) = lower(%s)
                              AND date_received BETWEEN %s - interval '10 minutes'
                                                    AND %s + interval '10 minutes'
                            LIMIT 1""",
                        (account_id, msg.get("subject") or "", msg.get("to") or "",
                         sent_date, sent_date),
                    )
                    if dupe:
                        return dupe["id"]
            except Exception:
                logger.debug("Sent-copy reconciliation failed", exc_info=True)
        if account_id is not None:
            existing = db.query_one(
                "SELECT id FROM mail_messages WHERE imap_uid = %s AND folder = %s AND account_id = %s",
                (uid_key, folder, account_id),
            )
        else:
            existing = db.query_one(
                "SELECT id FROM mail_messages WHERE imap_uid = %s AND folder = %s AND account_id IS NULL",
                (uid_key, folder),
            )
        if existing:
            return existing["id"]

        attachments = msg.get("attachments", [])
        # The attachment payloads (bytes) must NOT go into attachments_json:
        # they are not JSON-serializable, and the column only ever held
        # metadata. The bytes are written to disk by `_store_attachments`
        # right after the message row is created.
        attachments_json = json.dumps(
            [{k: v for k, v in a.items() if k != "content"} for a in attachments]
        ) if attachments else None

        # Check IMAP \Seen flag — respect server-side read status
        flags = msg.get("flags") or []
        is_read = "\\Seen" in flags or "Seen" in flags

        result = db.query_one(
            """INSERT INTO mail_messages
               (account_id, imap_uid, message_id, from_addr, to_addr, subject,
                body_text, body_html, date_received, folder, is_read, is_archived,
                attachments_json, raw_headers)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (
                account_id,
                _clean_nul_str(str(msg.get("uid") or "")),
                _clean_nul_str(msg.get("message_id") or ""),
                _clean_nul_str(msg.get("from") or ""),
                _clean_nul_str(msg.get("to") or ""),
                _clean_nul_str(msg.get("subject") or ""),
                _clean_nul_str(msg.get("body_text") or ""),
                _clean_nul_str(msg.get("body_html") or ""),
                # Always an aware UTC datetime (never the raw offset string):
                # date_received is `timestamptz` since migration 048, so a
                # naive value would be re-interpreted in the session timezone.
                mail_dates.parse_email_date(msg.get("date")),
                folder,
                is_read,
                False,
                _clean_nul_str(attachments_json) if attachments_json else None,
                _clean_nul_str(msg.get("raw_headers")),
            ),
        )
        msg_id = result["id"] if result else None
        if msg_id:
            _mark_processed(account_id, str(msg.get("uid") or msg_id), folder)
            # Store attachment records and optionally download files
            if attachments and account_id:
                _store_attachments(msg_id, account_id, attachments, msg)
        return msg_id
    except Exception as e:
        logger.error("Failed to store message: %s", e)
        return None


def _safe_attachment_filename(name: str) -> str:
    """Filesystem-safe name for a stored attachment file."""
    out = re.sub(r"[^\w.\- ]+", "_", name or "").strip("._")
    return out or "attachment"


def _imap_part_id_str(att: Any) -> str:
    """A short, stable identifier for a MIME part.

    ⚠ NEVER return `str(att.part)`. In imap_tools 1.14.0, `att.part` is the
    whole `email.message.EmailMessage` — its string form is the complete
    MIME part INCLUDING the base64 payload (a 300 KB PDF becomes a 405,411
    character string). Storing that in `mail_attachments.remote_part_id`
    (and in `mail_messages.attachments_json`) would put megabytes of base64
    into the DB and into every inbox/message API response.

    imap_tools' `MailAttachment` exposes NO part id at all (`content_id` is
    empty unless the message carries a Content-ID header), so the
    identifier is derived here:

      1. `cid:<Content-ID>` when the part has one — short, stable, and the
         thing a real MIME part is normally addressed by;
      2. otherwise `sha256:<first 16 hex chars>:<filename>:<size>` — derived
         from the payload, so it identifies the exact bytes, and short.

    The result is capped at MAX_PART_ID_LEN and never contains payload text.
    """
    if att is None:
        return ""
    cid = ""
    try:
        cid = (getattr(att, "content_id", "") or "").strip()
    except Exception:
        cid = ""
    if cid:
        return ("cid:" + cid)[:MAX_PART_ID_LEN]
    # Fall back to a content fingerprint. `payload` is the decoded bytes.
    payload = b""
    try:
        payload = getattr(att, "payload", None) or b""
    except Exception:
        payload = b""
    if not isinstance(payload, (bytes, bytearray)):
        payload = b""
    filename = ""
    try:
        filename = (getattr(att, "filename", "") or "").strip()
    except Exception:
        filename = ""
    digest = hashlib.sha256(bytes(payload)).hexdigest()[:16]
    return (f"sha256:{digest}:{filename}:{len(payload)}")[:MAX_PART_ID_LEN]


def _hash_file(path: Path) -> tuple[str, int]:
    """(sha256 hex, size in bytes) of a file, read in chunks.

    The bytes are never loaded into memory as a whole.
    """
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            h.update(chunk)
    return h.hexdigest(), size


def _store_staged_attachment_bytes(message_id: int, attachment_id: int, filename: str,
                                   staged_path: str) -> tuple[str | None, str | None]:
    """Move a staged attachment file into its final place in the local store.

    The staged file (written by `_fetch_single_message` when the message was
    fetched) is MOVED, not read into memory — that is what keeps the sync
    loop's memory bounded over a whole folder of attachments.

    Returns (relative_path, error); `relative_path` is None on failure.
    """
    if not MAIL_ATTACHMENT_STORAGE_ENABLED:
        return None, "local attachment storage disabled (MAIL_ATTACHMENT_STORAGE_ENABLED=false)"
    staged = Path(staged_path)
    try:
        size = staged.stat().st_size
    except Exception as exc:
        return None, f"staged attachment file unreadable: {exc}"
    if size > MAIL_ATTACHMENT_MAX_BYTES:
        try:
            staged.unlink(missing_ok=True)
        except Exception:
            pass
        return None, (f"file is {size} bytes, above the {MAIL_ATTACHMENT_MAX_BYTES} byte "
                      f"local-store limit (MAIL_ATTACHMENT_MAX_BYTES)")
    try:
        root = Path(__file__).resolve().parent.parent
        rel_dir = Path(MAIL_ATTACHMENT_DIR) / str(int(message_id))
        dest_dir = root / rel_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{int(attachment_id)}_{_safe_attachment_filename(filename)}"
        # Move within the same filesystem where possible (a rename), else
        # copy. A rename is atomic, so a crash never leaves a half-written
        # file that would later look like a valid local copy.
        try:
            staged.replace(dest)
        except OSError:
            dest.write_bytes(staged.read_bytes())
            try:
                staged.unlink(missing_ok=True)
            except Exception:
                pass
        return str(rel_dir / dest.name), None
    except Exception as exc:
        return None, f"could not store attachment on disk: {exc}"


def _store_attachment_bytes(message_id: int, attachment_id: int, filename: str,
                            content: bytes) -> tuple[str | None, str | None]:
    """Write attachment bytes to the local store.

    Returns (relative_path, error). `relative_path` is None when the bytes
    could NOT be stored (error explains why). The path is relative to the
    repo root, e.g. "data/mail-attachments/42/invoice.pdf".

    A file above MAIL_ATTACHMENT_MAX_BYTES is skipped (the metadata row
    still exists; the link-time mailbox fallback can still fetch it).
    """
    if not MAIL_ATTACHMENT_STORAGE_ENABLED:
        return None, "local attachment storage disabled (MAIL_ATTACHMENT_STORAGE_ENABLED=false)"
    size = len(content or b"")
    if size > MAIL_ATTACHMENT_MAX_BYTES:
        return None, (f"file is {size} bytes, above the {MAIL_ATTACHMENT_MAX_BYTES} byte "
                      f"local-store limit (MAIL_ATTACHMENT_MAX_BYTES)")
    try:
        root = Path(__file__).resolve().parent.parent
        rel_dir = Path(MAIL_ATTACHMENT_DIR) / str(int(message_id))
        dest_dir = root / rel_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{int(attachment_id)}_{_safe_attachment_filename(filename)}"
        # Write to a temp file then rename, so a crash never leaves a
        # half-written file that later looks like a valid local copy.
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(content)
        tmp.replace(dest)
        return str(rel_dir / dest.name), None
    except Exception as exc:
        return None, f"could not write attachment to disk: {exc}"


def _store_attachments(message_id: int, account_id: int, attachments: list[dict], msg: dict[str, Any]) -> None:
    """Store attachment metadata AND bytes in mail_attachments.

    Bug fix 2026-11-03: this used to store METADATA ONLY. Linking an email
    to a project then needed a live mailbox download to get the bytes, and
    any failure silently dropped the file from the project's Documents tab.
    The bytes are now stored locally at sync time so the link-time copy has
    a local source.

    The bytes arrive as a STAGED FILE on disk (`staged_path`), written by
    `_fetch_single_message` as the message was fetched. The file is MOVED
    into its final place — the bytes are never loaded into RAM, so a folder
    full of PDFs cannot exhaust memory (bug fix 2026-11-03, review round 2).

    Each attachment is stored independently: one oversized or failing file
    must not abort the others, and must not abort the sync loop.
    """
    for att in attachments:
        filename = _clean_nul_str(att.get("filename", "unnamed"))
        mime = _clean_nul_str(att.get("mime_type", "application/octet/octet-stream"))
        staged_path = (att.get("staged_path") or "").strip()
        content = att.get("content")
        try:
            att_id = db.insert_returning(
                "INSERT INTO mail_attachments (message_id, filename, mime_type, size_bytes, imap_part_id) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (message_id, filename, mime,
                 att.get("size_bytes", 0), _clean_nul_str(att.get("part_id", ""))),
            )
        except Exception as e:
            logger.error("Failed to store attachment metadata for message %s (%s): %s",
                         message_id, filename, e)
            continue
        if not att_id:
            continue
        # The short MIME-part identifier (Content-ID when the part has one,
        # otherwise a sha256 fingerprint) — used by the link-time fallback to
        # match the right MIME part.
        part_id = _clean_nul_str(att.get("remote_part_id", "") or "")
        if part_id:
            try:
                db.execute("UPDATE mail_attachments SET remote_part_id = %s WHERE id = %s",
                           (part_id, att_id))
            except Exception:
                logger.exception("Failed to record remote part id for attachment %s", att_id)
        if staged_path:
            # The staged file holds the bytes — move it into place without
            # reading it into memory.
            rel_path, err = _store_staged_attachment_bytes(message_id, att_id,
                                                           filename, staged_path)
            if rel_path:
                try:
                    digest, size = _hash_file(Path(__file__).resolve().parent.parent / rel_path)
                    db.execute(
                        "UPDATE mail_attachments SET stored_path = %s, content_hash = %s, "
                        "stored_size = %s, download_error = NULL WHERE id = %s",
                        (rel_path, digest, size, att_id),
                    )
                except Exception:
                    logger.exception("Failed to record stored path for attachment %s", att_id)
                logger.info("Stored mail attachment %s (%s) for message %s at %s",
                            att_id, filename, message_id, rel_path)
            else:
                logger.warning("Mail attachment %s (%s) for message %s not stored locally: %s",
                               att_id, filename, message_id, err)
                try:
                    db.execute("UPDATE mail_attachments SET download_error = %s WHERE id = %s",
                               (err, att_id))
                except Exception:
                    logger.exception("Failed to record download error for attachment %s", att_id)
            continue
        if not isinstance(content, (bytes, bytearray)) or not content:
            # No payload carried over. Two reasons:
            #   * the file is above MAIL_ATTACHMENT_MAX_BYTES — the fetch
            #     layer deliberately does not stage big payloads, so the
            #     bytes are dropped here and the file is NOT stored locally;
            #   * the scan produced metadata only.
            # Either way the metadata row stays, the attachment is still
            # listed, and the link-time mailbox fallback can still fetch it.
            size = int(att.get("size_bytes") or 0)
            if size > MAIL_ATTACHMENT_MAX_BYTES:
                err = (f"file is {size} bytes, above the {MAIL_ATTACHMENT_MAX_BYTES} byte "
                       f"local-store limit (MAIL_ATTACHMENT_MAX_BYTES)")
                logger.warning("Mail attachment %s (%s) for message %s not stored locally: %s",
                               att_id, filename, message_id, err)
                try:
                    db.execute("UPDATE mail_attachments SET download_error = %s WHERE id = %s",
                               (err, att_id))
                except Exception:
                    logger.exception("Failed to record download error for attachment %s", att_id)
            continue
        content = bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        rel_path, err = _store_attachment_bytes(message_id, att_id, filename, content)
        if rel_path:
            try:
                db.execute(
                    "UPDATE mail_attachments SET stored_path = %s, content_hash = %s, "
                    "stored_size = %s, download_error = NULL WHERE id = %s",
                    (rel_path, digest, len(content), att_id),
                )
            except Exception:
                logger.exception("Failed to record stored path for attachment %s", att_id)
            logger.info("Stored mail attachment %s (%s) for message %s at %s",
                        att_id, filename, message_id, rel_path)
        else:
            # Metadata is still stored; the file is still listed and can
            # still be fetched at link time. Log the skip loudly.
            logger.warning("Mail attachment %s (%s) for message %s not stored locally: %s",
                           att_id, filename, message_id, err)
            try:
                db.execute("UPDATE mail_attachments SET download_error = %s WHERE id = %s",
                           (err, att_id))
            except Exception:
                logger.exception("Failed to record download error for attachment %s", att_id)


def _account_retention_days(mailbox_cfg: dict[str, Any]) -> int:
    """The account's retention window in days (0 = keep forever).

    Read from the mailbox config when present; otherwise fall back to the
    `mail_accounts.retention_days` column. A missing/invalid value means
    "no retention" (keep forever) — the scanner then never skips a message.
    """
    raw = (mailbox_cfg or {}).get("retention_days")
    if raw is None:
        account_id = (mailbox_cfg or {}).get("account_id")
        if account_id is not None:
            try:
                row = db.query_one(
                    "SELECT retention_days FROM mail_accounts WHERE id = %s",
                    (account_id,),
                )
                raw = (row or {}).get("retention_days")
            except Exception:
                logger.exception("Retention: could not read retention_days for account %s",
                                 account_id)
                raw = None
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return 0
    return days if days > 0 else 0


def _retention_cutoff(mailbox_cfg: dict[str, Any]) -> datetime | None:
    """The instant before which a message is too old to import, or None when
    the account keeps mail forever.

    Same cutoff logic as `_enforce_retention` (now - retention_days), so the
    scanner never imports a message that retention would immediately delete.

    The cutoff is tz-aware UTC, matching `mail_messages.date_received` after
    migration 048 (live bug 2026-11-09: the column was a plain TIMESTAMP, so
    the stored instant depended on the DB session timezone and could be hours
    off). Keeping both sides aware means the import-skip and the retention
    purge always agree.
    """
    days = _account_retention_days(mailbox_cfg)
    if days <= 0:
        return None
    return datetime.now(timezone.utc) - timedelta(days=days)


def _parse_message_date(value: Any) -> datetime | None:
    """Parse an IMAP message date into an AWARE UTC datetime, or None.

    Uses the shared `mail_dates.parse_email_date`, which understands both the
    scanner's own ISO strings and a raw RFC 2822 `Date:` header, and treats a
    value with no offset as UTC (never as browser-local / server-local). The
    old version stripped the offset, so a message received at 13:20 -0500 was
    compared and stored as if it were 13:20 UTC.
    """
    return mail_dates.parse_email_date(value)


def _message_older_than_cutoff(msg: dict[str, Any], cutoff: datetime | None) -> bool:
    """True when the message's date is strictly older than the retention cutoff."""
    if cutoff is None:
        return False
    dt = _parse_message_date(msg.get("date"))
    if dt is None:
        # An undated message cannot be proven older than the cutoff — import it.
        return False
    return dt < cutoff


def _process_message(msg: dict[str, Any], mailbox_cfg: dict[str, Any]) -> dict[str, Any]:
    uid = msg.get("uid") or ""
    account_id = mailbox_cfg.get("account_id")
    log_entry: dict[str, Any] = {
        "timestamp": _now_et(),
        "uid": uid,
        "subject": msg.get("subject", ""),
        "from": msg.get("from", ""),
        "folder": msg.get("folder", "INBOX"),
        "mailbox": mailbox_cfg.get("inbox", ""),
        "account_id": account_id,
        "date": msg.get("date", ""),
    }

    if not uid and not msg.get("message_id"):
        log_entry["status"] = "skipped_no_id"
        _append_log(log_entry)
        return log_entry

    if _is_processed(account_id, uid or msg.get("message_id") or "", msg.get("folder", "")):
        log_entry["status"] = "already_processed"
        _append_log(log_entry)
        return log_entry

    # ⚠ Retention guard (bug fix 2026-11-04, "mail retention re-import loop").
    # A message older than the account's retention window would be deleted by
    # `_enforce_retention` in the SAME poll cycle. Importing it first meant
    # the message was re-imported (attachments re-downloaded and stored under
    # a NEW message id) on every poll — 1.6 GB of duplicate files in one day.
    # Skip it BEFORE the message row and its attachments are stored.
    # An explicit manual reprocess (mailbox_cfg["skip_retention_check"]) is
    # exempt: the row already exists in the DB, so it survived retention.
    cutoff = None if mailbox_cfg.get("skip_retention_check") else _retention_cutoff(mailbox_cfg)
    if _message_older_than_cutoff(msg, cutoff):
        log_entry["status"] = "skipped_older_than_retention"
        _append_log(log_entry)
        # Mark it processed so the next poll does not reconsider it. The
        # watermark advances past it either way (see _poll_mailbox_cfg).
        _mark_processed(account_id, uid or msg.get("message_id") or "", msg.get("folder", ""))
        logger.info("Skipped message UID %s (folder %s): older than the %d-day retention window",
                    uid, msg.get("folder", ""), _account_retention_days(mailbox_cfg))
        return log_entry

    msg_id = _store_message(msg, mailbox_cfg)
    if msg_id is None:
        log_entry["status"] = "store_failed"
        _append_log(log_entry)
        return log_entry

    # Publish mail:new event via SSE for real-time inbox updates
    account_id = mailbox_cfg.get("account_id")
    if account_id:
        try:
            acct_row = db.query_one("SELECT owner_user_id FROM mail_accounts WHERE id = %s", (account_id,))
            if acct_row and acct_row.get("owner_user_id"):
                event_bus.publish("mail:new", {
                    "account_id": account_id,
                    "subject": msg.get("subject") or "",
                    "from": msg.get("from") or "",
                }, user_ids=[str(acct_row["owner_user_id"])])
        except Exception:
            pass  # non-fatal

    classification = _classify_message(msg)
    log_entry["classification"] = classification["classification"]
    log_entry["match_strength"] = classification["match_strength"]
    log_entry["action"] = classification["action"]
    if classification.get("ml_applied"):
        log_entry["ml_applied"] = True

    # Read behavior toggles from config
    config = get_contractors()
    toggles = config.get("scanner_behavior", {})

    # --- Auto-link by project ID / claim code ---
    # Each checkbox governs exactly its own matches (link_method): a checked
    # [#PROJECTID] box can never license claim-code links and vice versa.
    # Custom-rule links carry no method and fall back to the project toggle
    # (historical behavior).
    link_allowed, link_dry = _link_toggle_allows(classification, toggles, account_id)
    if (classification["action"] == "link_deal" and classification["linked_deal_id"]
            and link_allowed):
        if link_dry:
            log_entry["status"] = "dry_run"
            log_entry["dry_run_action"] = "link_deal"
            log_entry["dry_run_target"] = classification["linked_deal_id"]
            deal = _find_opportunity_by_deal_id(classification["linked_deal_id"])
            log_entry["dry_run_deal_title"] = deal["title"] if deal else ""
        else:
            linked = _link_email_to_deal(msg_id, classification["linked_deal_id"])
            log_entry["deal_linked"] = linked
            if linked:
                log_entry["history_created"] = create_email_history_snapshot(
                    classification["linked_deal_id"], msg_id)

    # --- Auto-link by content (secondary classifier) ---
    if (classification["linked_deal_id"] is None
            and _is_behavior_enabled(toggles.get("auto_link_by_content", {}), account_id)):
        dry = _is_dry_run(toggles.get("auto_link_by_content", {}), account_id)
        subject = msg.get("subject") or ""
        body_text = msg.get("body_text") or ""
        body_html = msg.get("body_html") or ""
        body = body_text or _sanitize_body(body_html)

        content_matches = _match_by_content(subject, body, _deal_index)
        if content_matches:
            best = content_matches[0]
            if dry:
                log_entry["status"] = "dry_run"
                log_entry["dry_run_action"] = "link_deal_by_content"
                log_entry["dry_run_target"] = best["deal_id"]
                log_entry["dry_run_field_type"] = best["field_type"]
                deal = _find_opportunity_by_deal_id(best["deal_id"])
                log_entry["dry_run_deal_title"] = deal["title"] if deal else ""
            else:
                linked = _link_email_to_deal(msg_id, best["deal_id"])
                log_entry["deal_linked"] = linked
                if linked:
                    log_entry["history_created"] = create_email_history_snapshot(
                        best["deal_id"], msg_id)
                classification["linked_deal_id"] = best["deal_id"]
        elif not classification["linked_deal_id"]:
            log_entry["unlinked_reason"] = "No unique identifier found"

    # --- Post notes to deal ---
    if (classification["linked_deal_id"] and log_entry.get("deal_linked")
            and _is_behavior_enabled(toggles.get("post_notes", {}), account_id)):
        dry = _is_dry_run(toggles.get("post_notes", {}), account_id)
        deal = _find_opportunity_by_deal_id(classification["linked_deal_id"])
        if deal:
            from_addr = msg.get("from") or ""
            to_addr = msg.get("to") or ""
            att_snapshot = [
                {"filename": a.get("filename", ""), "size_bytes": a.get("size_bytes", 0) or 0,
                 "mime_type": a.get("mime_type", "")}
                for a in (msg.get("attachments") or [])
            ]
            snapshot = {
                "type": "email_snapshot",
                "message_id": msg_id,
                "from": from_addr,
                "to": to_addr,
                "cc": msg.get("cc") or "",
                "subject": msg.get("subject", "") or "",
                "date_sent": mail_dates.iso_utc(msg.get("date")),
                "body_html": msg.get("body_html") or "",
                "body_text": msg.get("body_text") or "",
                "attachments": att_snapshot,
            }
            if dry:
                log_entry["dry_run_action"] = "post_note"
                log_entry["dry_run_deal_title"] = deal["title"]
            else:
                _post_note_to_deal(deal["id"], json.dumps(snapshot))
                log_entry["note_posted"] = True

    # --- Execute custom behaviors ---
    linked_deal_id = classification["linked_deal_id"]
    if linked_deal_id:
        custom_results = _execute_custom_behaviors(msg, msg_id, linked_deal_id, account_id, classification)
        for cr in custom_results:
            bt = cr.get("behavior_type", "")
            if cr.get("dry_run"):
                log_entry["status"] = "dry_run"
                log_entry["dry_run_action"] = f"custom:{bt}"
                log_entry["dry_run_detail"] = cr.get("dry_run_detail", "")
            elif cr.get("error"):
                log_entry["custom_error"] = f"{bt}: {cr['error']}"
            else:
                log_entry[f"custom_{bt}"] = True

    # Set final status
    if log_entry.get("status") == "dry_run":
        pass  # Already set
    elif classification["classification"] == "claim_code_no_deal":
        log_entry["status"] = "linked_no_deal"
    elif classification["action"] == "link_deal":
        log_entry["status"] = "processed"
    else:
        log_entry["status"] = "processed"

    # Store linked_deal_id for API response
    if classification["linked_deal_id"]:
        log_entry["linked_deal_id"] = classification["linked_deal_id"]

    _append_log(log_entry)
    return log_entry


def _poll_mailbox_cfg(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Poll a single mailbox config (folders, fetch, process). Extracted from
    _poll_mailboxes so a manual per-account poll reuses the exact same path."""
    results: list[dict[str, Any]] = []
    logger.info("Polling mailbox %s@%s", cfg["user"], cfg["host"])
    try:
        # Sync folder list first
        folder_count = _sync_imap_folders(cfg)
        if folder_count:
            logger.info("Synced %d IMAP folders", folder_count)

        folders = cfg.get("folders", [cfg.get("inbox", "INBOX")])
        # Per-account sync window (retention_days), fallback to global SCANNER_SYNC_DAYS
        acct_retention = cfg.get("retention_days", 0)
        if acct_retention and acct_retention > 0:
            since = datetime.now(timezone.utc) - timedelta(days=acct_retention)
        else:
            try:
                sync_days = int(os.environ.get("SCANNER_SYNC_DAYS", "90"))
            except (TypeError, ValueError):
                sync_days = 90
            since = datetime.now(timezone.utc) - timedelta(days=sync_days) if sync_days and sync_days > 0 else None
        for folder in folders:
            logger.info("Fetching folder %s for %s", folder, cfg["user"])
            # Key the watermark + STATUS on the normalized name — the same
            # mailbox _fetch_messages actually SELECTs.
            imap_folder = _normalize_folder_name(folder)
            # Incremental path: when UIDVALIDITY matches the watermark, fetch
            # only UID > last_seen (seconds). Otherwise the legacy full-window
            # fetch (first run, renumbered mailbox, unknown validity).
            # `uid_min` is the EXCLUSIVE lower bound: `_fetch_messages` asks
            # IMAP for `uid_min+1:*` and drops anything <= uid_min it gets
            # back (IMAP's `n:*` always includes the folder's highest UID).
            uid_min = None
            cur_validity, cur_uidnext = None, None
            stored_validity, last_seen = _get_folder_watermark(cfg.get("account_id"), imap_folder)
            if last_seen is not None:
                cur_validity, cur_uidnext = _read_folder_status(cfg, imap_folder)
                if (cur_validity is not None and stored_validity is not None
                        and cur_validity == stored_validity):
                    uid_min = int(last_seen)
                    logger.info("Incremental fetch UID>%d for %s folder %s", uid_min, cfg["user"], folder)
            # ⚠ STREAMED (bug fix 2026-11-03, review round 2).
            # `_fetch_messages` is a generator: it yields one message at a
            # time, and each message's attachment bytes are staged on disk
            # before the next message is fetched. The old code built a LIST
            # of the whole folder (bytes included), which on a first sync /
            # renumbering / watermark reset could exhaust RAM.
            #
            # The watermark still needs the highest UID before processing, so
            # the UIDs are collected up front by the generator itself; here we
            # simply track the max as the messages arrive.
            msgs = _fetch_messages(cfg, folder, since=since, uid_min=uid_min)
            max_uid = 0
            count = 0
            for msg in msgs:
                try:
                    max_uid = max(max_uid, int(msg.get("uid") or 0))
                except (TypeError, ValueError):
                    pass
                count += 1
                # Process (and store) immediately, so the message's bytes
                # are released before the next one is fetched.
                result = _process_message(msg, cfg)
                results.append(result)
                del msg
            # Advance the watermark: max fetched UID, or UIDNEXT-1 when nothing
            # new (empty folder converges to incremental on the next cycle).
            # Never move it backwards.
            floor = max(int(last_seen or 0),
                        int(cur_uidnext or 1) - 1 if not count and cur_uidnext else 0)
            new_last = max(max_uid, floor)
            _save_folder_watermark(cfg.get("account_id"), imap_folder,
                                   cur_validity if cur_validity is not None else stored_validity,
                                   new_last)
    except MailboxIdentityError as e:
        logger.error("Refusing to poll %s: %s", cfg.get("user"), e)
        if cfg.get("account_id") is not None:
            mail_identity.record_identity_refusal(cfg.get("account_id"), str(e))
        infra_log.log_infra_event(
            "error", f"Mail poll refused for {cfg.get('host')}: {e}", source="mail_scanner")
    except Exception as e:
        logger.error("Polling error for %s: %s", cfg["host"], e)
        infra_log.log_infra_event("error", f"Mail scanner poll failed for {cfg.get('host')}: {e}", source="mail_scanner")
    return results


def _poll_mailboxes() -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    mailboxes = _get_mailboxes()
    if not mailboxes:
        logger.info("No IMAP mailboxes configured")
        return results

    # Refresh deal index for content matching
    config = get_contractors()
    if config.get("scanner_behavior", {}).get("auto_link_by_content", {}).get("enabled", False):
        _build_deal_index()

    # Fault isolation: accounts poll concurrently so one slow Outlook mailbox
    # cannot stall the rest. A hung account is abandoned after the timeout
    # (its daemon worker eventually finishes or dies); the cycle continues.
    import concurrent.futures as _cf
    try:
        timeout_s = int(os.environ.get("SCANNER_ACCOUNT_TIMEOUT", "240"))
    except (TypeError, ValueError):
        timeout_s = 240
    workers = max(1, min(4, len(mailboxes)))
    with _cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mailpoll") as ex:
        futs = {ex.submit(_poll_mailbox_cfg, cfg): cfg for cfg in mailboxes}
        for fut, cfg in futs.items():
            try:
                results.extend(fut.result(timeout=timeout_s))
            except _cf.TimeoutError:
                logger.warning("Mail poll timed out for %s after %ds — continuing with other accounts",
                               cfg.get("user"), timeout_s)
            except Exception as e:
                logger.error("Mail poll failed for %s: %s", cfg.get("user"), e)

    return results


# Manual per-account polls, triggered by the UI refresh button via
# POST /api/v2/mail/accounts/{id}/sync-now. Requests run immediately in a
# dedicated worker thread (never queued behind a running full cycle) AND stay
# queued for the loop as a fallback, so rapid multi-account refreshes are all
# honored and none is lost if the worker hits an error first.
_manual_poll_ids: set[int] = set()
_wakeup = threading.Event()
_manual_lock = threading.Lock()
_manual_in_progress: dict[int, float] = {}
_manual_last_done: dict[int, float] = {}
_manual_last_error: dict[int, str] = {}


def request_manual_poll(account_id: int) -> bool:
    """Queue a one-account poll and start its worker immediately. Returns False
    for a bad id. Duplicate requests for an already-running poll are accepted
    (set semantics) without spawning extra workers."""
    try:
        aid = int(account_id)
    except (TypeError, ValueError):
        return False
    with _manual_lock:
        _manual_poll_ids.add(aid)
        already = aid in _manual_in_progress
    _wakeup.set()
    if not already:
        t = threading.Thread(target=_manual_poll_worker, args=(aid,),
                             daemon=True, name=f"mail-manual-{aid}")
        t.start()
    return True


def _manual_poll_worker(aid: int) -> None:
    with _manual_lock:
        if aid in _manual_in_progress:
            return
        _manual_in_progress[aid] = time.time()
    try:
        cfgs = [c for c in _get_mailboxes() if c.get("account_id") == aid]
        if not cfgs:
            with _manual_lock:
                _manual_last_error[aid] = "account not syncable (disabled or missing credentials)"
            logger.warning("Manual poll: account %d not syncable (disabled or missing credentials)", aid)
            return
        for cfg in cfgs:
            _poll_mailbox_cfg(cfg)
        with _manual_lock:
            _manual_poll_ids.discard(aid)
            _manual_last_done[aid] = time.time()
            _manual_last_error.pop(aid, None)
    except Exception as e:
        logger.exception("Manual poll failed for account %d", aid)
        with _manual_lock:
            _manual_last_error[aid] = str(e)[:300]
    finally:
        with _manual_lock:
            _manual_in_progress.pop(aid, None)


def manual_poll_state(account_id: int) -> str:
    """idle | running | done | error — for the UI status poll."""
    with _manual_lock:
        if account_id in _manual_in_progress:
            return "running"
        if account_id in _manual_last_error:
            return "error"
        if account_id in _manual_last_done:
            return "done"
    return "idle"


def _enforce_retention() -> None:
    """Delete synced messages older than each account's retention_days (0 = keep forever).

    Runs for every account with retention_days > 0, regardless of sync_enabled.
    Messages linked to deals are always kept. Local DB only — never touches IMAP.

    ⚠ The processed-ID keys of the purged messages are KEPT (bug fix
    2026-11-04, "mail retention re-import loop"). They used to be discarded
    here, which removed the guard that stops the scanner from re-importing a
    message: the message was deleted, its key was dropped, and the next poll
    imported it again (attachments re-downloaded) — 1.6 GB of duplicate files
    in one day. The purged UID stays in the processed set so it is never
    imported again. `_process_message` also skips messages that are already
    older than the retention window, so a purged message is not even fetched.

    The stored attachment bytes (`data/mail-attachments/<message_id>/`) are
    only deleted when `MAIL_ATTACHMENT_DROP_STORED_ON_PURGE` is true. The
    DEFAULT is **false** (keep the files): the retention/backup policy for
    that tree is Ken's decision and is NOT made by this code. The setting
    exists so the policy can be flipped without another code change.

    NOTE: the PROJECT's copy (`data/history-attachments/<opp>/<event>/`) is
    never touched by retention — it is the customer's document and must
    outlive the mail account (the Oct 29 2026 bug).
    """
    try:
        rows = db.query_dicts(
            "SELECT id, email, retention_days FROM mail_accounts WHERE retention_days IS NOT NULL AND retention_days > 0"
        )
    except Exception as e:
        logger.error("Retention: failed to fetch accounts: %s", e)
        infra_log.log_infra_event("error", f"Mail retention failed to fetch accounts: {e}", source="mail_scanner")
        return
    for acct in rows:
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=int(acct["retention_days"]))
            # The messages about to be deleted. Their processed-ID keys are
            # KEPT (not discarded) so the scanner never re-imports them —
            # see the function docstring.
            purged_rows = db.query_dicts(
                "SELECT account_id, imap_uid, folder FROM mail_messages WHERE account_id = %s AND date_received < %s AND id NOT IN (SELECT message_id FROM mail_deal_links)",
                (acct["id"], cutoff),
            )
            # Collect the stored attachment paths BEFORE the delete — the
            # DELETE cascades the mail_attachments rows away, and with
            # migration 043's ON DELETE SET NULL the project's timeline rows
            # survive (the file itself must survive too).
            stored_paths: list[str] = []
            if MAIL_ATTACHMENT_DROP_STORED_ON_PURGE:
                try:
                    stored_paths = [
                        r["stored_path"] for r in db.query_dicts(
                            "SELECT ma.stored_path FROM mail_attachments ma "
                            "JOIN mail_messages m ON m.id = ma.message_id "
                            "WHERE m.account_id = %s AND m.date_received < %s "
                            "AND m.id NOT IN (SELECT message_id FROM mail_deal_links) "
                            "AND ma.stored_path IS NOT NULL AND ma.stored_path <> ''",
                            (acct["id"], cutoff),
                        )
                    ]
                except Exception:
                    logger.exception("Retention: could not list stored attachment paths")
            deleted = db.execute(
                """DELETE FROM mail_messages
                   WHERE account_id = %s
                     AND date_received IS NOT NULL
                     AND date_received < %s
                     AND id NOT IN (SELECT message_id FROM mail_deal_links)""",
                (acct["id"], cutoff),
            )
            if deleted:
                logger.info("Retention: deleted %d old message(s) for %s (older than %d days)", deleted, acct["email"], acct["retention_days"])
                # ⚠ Keep the processed-ID keys of the purged messages (bug fix
                # 2026-11-04). They used to be discarded here, which removed
                # the very guard that stops the scanner from re-importing the
                # message on the next poll. Instead, the purged UID is
                # RE-ADDED under the folder-scoped key so it can never be
                # imported again (the legacy `account:uid` key is also kept,
                # so an existing deployment is not re-imported either).
                if purged_rows:
                    ids = _load_processed_ids()
                    for r in purged_rows:
                        uid_val = r.get("imap_uid")
                        if uid_val is None or str(uid_val) == "":
                            continue
                        ids.add(_processed_key(r.get("account_id"), str(uid_val),
                                               r.get("folder") or ""))
                        ids.add(_legacy_processed_key(r.get("account_id"), str(uid_val)))
                    _save_processed_ids(ids)
            if stored_paths:
                removed = 0
                root = Path(__file__).resolve().parent.parent
                for rel in stored_paths:
                    try:
                        full = root / rel
                        # Containment check: only ever delete inside the
                        # mail-attachments tree.
                        base = (root / MAIL_ATTACHMENT_DIR).resolve()
                        if not str(full.resolve()).startswith(str(base) + os.sep):
                            continue
                        if full.is_file():
                            full.unlink()
                            removed += 1
                    except Exception:
                        logger.exception("Retention: could not delete stored attachment %s", rel)
                logger.info("Retention: deleted %d stored attachment file(s) for %s "
                            "(MAIL_ATTACHMENT_DROP_STORED_ON_PURGE=true)",
                            removed, acct["email"])
        except Exception as e:
            logger.error("Retention: failed for %s: %s", acct["email"], e)
            infra_log.log_infra_event("error", f"Mail retention failed for {acct.get('email')}: {e}", source="mail_scanner")


def _scanner_loop() -> None:
    logger.info("Scanner loop started")
    while True:
        try:
            interval = int(os.environ.get("SCANNER_POLL_INTERVAL", "120"))
            env_enabled = os.environ.get("SCANNER_ENABLED", "false").lower() in ("true", "1", "yes")
            db_enabled = False
            try:
                rows = db.query_dicts("SELECT id FROM mail_accounts WHERE sync_enabled = TRUE LIMIT 1")
                db_enabled = len(rows) > 0
            except Exception:
                pass
            if not env_enabled and not db_enabled:
                logger.info("Scanner disabled, sleeping %ds", interval)
            else:
                manual = sorted(_manual_poll_ids)
                _manual_poll_ids.clear()
                if manual:
                    logger.info("Manual poll requested for accounts %s", manual)
                    cfgs = [c for c in _get_mailboxes() if c.get("account_id") in manual]
                    unknown = [a for a in manual if a not in {c.get("account_id") for c in cfgs}]
                    for aid in unknown:
                        logger.warning("Manual poll: account %d not syncable (disabled or missing credentials)", aid)
                    for cfg in cfgs:
                        _poll_mailbox_cfg(cfg)
                    _enforce_retention()
                else:
                    logger.info("Starting poll cycle")
                    _poll_mailboxes()
                    _enforce_retention()
                logger.info("Poll cycle complete")
        except Exception as e:
            logger.error("Scanner loop error: %s", e)
            infra_log.log_infra_event("error", f"Mail scanner loop error: {e}", source="mail_scanner")
        _wakeup.clear()
        _wakeup.wait(int(os.environ.get("SCANNER_POLL_INTERVAL", "120")))


def start_scanner() -> threading.Thread:
    thread = threading.Thread(target=_scanner_loop, daemon=True, name="sietch-mail-scanner")
    thread.start()
    logger.info("Scanner thread started")
    return thread


def get_scanner_status() -> dict[str, Any]:
    """Get scanner status with per-account details and active behaviors."""
    try:
        rows = db.query_dicts(
            """SELECT id, email, imap_host, imap_port, oauth_provider,
                      password_encrypted, last_sync, sync_enabled,
                      oauth_identity, oauth_last_error, oauth_graph_last_error
               FROM mail_accounts
               WHERE sync_enabled = TRUE"""
        )
    except Exception:
        rows = []

    # Get behavior config
    config = get_contractors()
    behavior_map = config.get("scanner_behavior", {})
    custom_behaviors = config.get("custom_behaviors", [])

    account_list = []
    for acct in rows:
        # Determine account status
        if acct.get("oauth_provider"):
            # Check if OAuth token is valid
            token_valid = False
            try:
                token_row = db.query_one(
                    "SELECT oauth_token_expires FROM mail_accounts WHERE id = %s",
                    (acct["id"],),
                )
                if token_row and token_row.get("oauth_token_expires"):
                    expires = token_row["oauth_token_expires"]
                    if isinstance(expires, datetime):
                        # `mail_accounts.oauth_token_expires` is still a plain
                        # TIMESTAMP (naive UTC). Comparing it directly with an
                        # aware `now` raises TypeError, which the surrounding
                        # `except` swallowed — so `token_valid` stayed False and
                        # every OAuth account reported `auth_failed` even while
                        # it was syncing normally (live bug 2026-11-09: 8/9/10
                        # looked dead while fetching mail every poll).
                        token_valid = mail_dates.to_utc(expires) > datetime.now(timezone.utc)
                # Also check if we have a refresh token (can refresh)
                if not token_valid:
                    refresh_row = db.query_one(
                        "SELECT oauth_refresh_token FROM mail_accounts WHERE id = %s",
                        (acct["id"],),
                    )
                    if refresh_row and refresh_row.get("oauth_refresh_token"):
                        token_valid = True  # Can refresh
            except Exception:
                pass
            status = "active" if token_valid else "auth_failed"
            if status == "active" and (acct.get("oauth_last_error")
                                       or acct.get("oauth_graph_last_error")):
                # A refresh we observed failing is not "active" even if a
                # token/refresh token is still on file.
                status = "auth_failed"
        else:
            status = "active" if acct.get("password_encrypted") else "no_password"

        # Determine active behaviors for this account
        active_behaviors = []
        for beh_name, beh_cfg in behavior_map.items():
            if _is_behavior_enabled(beh_cfg, acct["id"]):
                active_behaviors.append(beh_name)
        for cb in custom_behaviors:
            if cb.get("enabled") and _is_behavior_enabled(cb, acct["id"]):
                active_behaviors.append(cb.get("type", "custom"))

        account_list.append({
            "id": acct["id"],
            "email": acct["email"],
            "imap_host": acct["imap_host"],
            "imap_port": acct["imap_port"],
            "status": status,
            "active_behaviors": active_behaviors,
            "last_sync": str(acct.get("last_sync") or ""),
            # Connected identity + last persisted OAuth failure (migration
            # 047). Lets the admin panel and the diagnostic script explain a
            # dead account without reading tokens.
            "oauth_identity": acct.get("oauth_identity"),
            "last_error": acct.get("oauth_last_error") or acct.get("oauth_graph_last_error"),
        })

    enabled = len(account_list) > 0
    if not enabled:
        enabled = os.environ.get("SCANNER_ENABLED", "false").lower() in ("true", "1", "yes")

    return {
        "enabled": enabled,
        "accounts": account_list,
        "poll_interval": int(os.environ.get("SCANNER_POLL_INTERVAL", "120")),
        "processed_count": len(_load_processed_ids()),
        "running": threading.active_count() > 0,
        "manual_in_progress": sorted(_manual_in_progress),
        "manual_last_done": {str(k): v for k, v in _manual_last_done.items()},
        "manual_last_error": {str(k): v for k, v in _manual_last_error.items()},
    }


def get_scanner_log(limit: int = 200, account_id: str | None = None) -> list[dict[str, Any]]:
    if not LOG_FILE.exists():
        return []
    # Defensive default: 0/None used to return the entire log, which is unbounded
    # in production. Cap to 200 (or a caller-supplied positive value) at the
    # helper level so the handler can be sloppy safely.
    if not limit or limit <= 0:
        limit = 200
    try:
        # Audit #6: take the same lock as _append_log so we don't read
        # a half-written line. Use shared (read) flock so multiple
        # readers can proceed in parallel; writers take exclusive.
        with _LOG_LOCK:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                if _HAVE_FCNTL and hasattr(f, "fileno"):
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                    except (OSError, ValueError):
                        pass
                lines = f.readlines()
                if _HAVE_FCNTL and hasattr(f, "fileno"):
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    except (OSError, ValueError):
                        pass
        # Skip malformed lines (defense-in-depth even with locking).
        entries: list[dict[str, Any]] = []
        for l in lines:
            l = l.strip()
            if not l:
                continue
            try:
                entries.append(json.loads(l))
            except json.JSONDecodeError:
                continue
        if account_id:
            # Support "crm" sentinel for all CRM-only accounts or a numeric ID.
            if str(account_id).lower() == "crm":
                crm_ids = _get_crm_account_ids()
                entries = [e for e in entries if e.get("account_id") in crm_ids]
            else:
                entries = [e for e in entries if str(e.get("account_id")) == str(account_id)]
        return entries[-limit:]
    except Exception:
        logger.exception("Failed to read scanner log from %s", LOG_FILE)
        return []


def get_contractors() -> dict[str, Any]:
    """Get contractors from DB, falling back to file-based config."""
    # Read toggles from file (saved by admin UI)
    file_config = _read_json(CONTRACTORS_FILE, default=DEFAULT_CONTRACTORS)
    file_config = _normalize_behavior_config(file_config)
    toggles = file_config.get("scanner_behavior", DEFAULT_CONTRACTORS["scanner_behavior"])
    try:
        rows = db.query_dicts("SELECT * FROM mail_contractors WHERE enabled = TRUE ORDER BY priority DESC, name")
        if rows:
            contractors = []
            for r in rows:
                contractors.append({
                    "id": str(r["id"]),
                    "name": r["name"],
                    "imap_account": r.get("imap_account_id") or "",
                    "folder": r.get("folder") or "INBOX",
                    "action": r.get("action") or "link_only",
                    "responsible": r.get("responsible_user_id") or "",
                })
            return {
                "contractors": contractors,
                "scanner_behavior": toggles,
                "action_toggles": toggles,
            }
    except Exception as e:
        logger.warning("Failed to load contractors from DB, falling back to file: %s", e)
    return file_config


def update_contractors(data: dict[str, Any]) -> dict[str, Any]:
    """Update contractors — writes to file (DB managed via CRM admin API)."""
    _write_json(CONTRACTORS_FILE, data)
    return data


# Audit #6: feedback file locking.
#
# FEEDBACK_FILE is written from two contexts:
#  1. The scanner background thread (auto-logging "did the rule fire?" entries)
#  2. The dashboard request thread (user clicks "correct" in the admin tab)
# and read concurrently by:
#  3. get_feedback_entries (admin UI loads the feedback list)
#  4. train_ml_head.train (the ML retrain job)
#
# CPython's GIL + buffered I/O mean a single `write()` is usually atomic
# within a process, but the reader can still see a half-written line if
# the writer is mid-flush. The previous `except Exception: pass` also
# silently swallowed disk-full / permission errors. Fix both:
#  - threading.Lock to serialize within the dashboard process
#  - fcntl.flock for cross-process safety (scanner container + in-process)
#  - flush + fsync so a sudden kill doesn't lose the entry
#  - logger.exception instead of bare pass

_FEEDBACK_LOCK = threading.Lock()


def record_feedback_candidate(log_entry: dict[str, Any]) -> None:
    line = json.dumps(log_entry, default=str) + "\n"
    try:
        with _FEEDBACK_LOCK:
            with open(FEEDBACK_FILE, "a", encoding="utf-8") as f:
                # fileno() may not exist on test fakes; skip flock cleanly.
                if _HAVE_FCNTL and hasattr(f, "fileno"):
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    except (OSError, ValueError):
                        pass
                f.write(line)
                f.flush()
                if hasattr(f, "fileno"):
                    try:
                        os.fsync(f.fileno())
                    except (OSError, AttributeError):
                        pass
                if _HAVE_FCNTL and hasattr(f, "fileno"):
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    except (OSError, ValueError):
                        pass
    except Exception:
        # Audit #6: was `except Exception: pass`. Now we surface real I/O
        # errors (disk full, permission denied) so the operator knows the
        # training set is incomplete instead of silently losing samples.
        logger.exception("Failed to append feedback candidate to %s", FEEDBACK_FILE)


def store_user_feedback(payload: dict[str, Any]) -> dict[str, Any]:
    record_feedback_candidate(payload)
    return {"ok": True}


def get_feedback_entries(limit: int = 200) -> list[dict[str, Any]]:
    if not FEEDBACK_FILE.exists():
        return []
    # Defensive default (matches get_scanner_log).
    if not limit or limit <= 0:
        limit = 200
    # Audit #6: take the same lock as the writer so we don't read a
    # half-written line. Holding it during readlines() is fine — the
    # critical section is short and writes are infrequent.
    try:
        with _FEEDBACK_LOCK:
            with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
                if _HAVE_FCNTL and hasattr(f, "fileno"):
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                    except (OSError, ValueError):
                        pass
                lines = f.readlines()
                if _HAVE_FCNTL and hasattr(f, "fileno"):
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    except (OSError, ValueError):
                        pass
        # Skip malformed lines (defense-in-depth even with locking).
        out: list[dict[str, Any]] = []
        for l in lines:
            l = l.strip()
            if not l:
                continue
            try:
                out.append(json.loads(l))
            except json.JSONDecodeError:
                continue
        return out[-limit:]
    except Exception:
        logger.exception("Failed to read feedback entries from %s", FEEDBACK_FILE)
        return []


def _reprocess_message_by_uid(account_id: int, uid: str) -> dict[str, Any] | None:
    """Clear the processed ID for a message and immediately reprocess it from the DB.

    Reconstructs the message from mail_messages and runs the classifier pipeline so
    dry-run actions and classifications are logged immediately, not just on the next
    IMAP poll.

    Both the folder-scoped key and the legacy `account:uid` key are cleared,
    so a manual reprocess always works against the current key scheme.
    """
    try:
        row = db.query_one(
            "SELECT id, imap_uid, message_id, from_addr, to_addr, cc_addr, subject, "
            "body_text, body_html, date_received, folder, account_id "
            "FROM mail_messages WHERE account_id = %s AND imap_uid = %s",
            (account_id, uid),
        )
    except Exception:
        row = None

    # Clear the processed keys for BOTH the folder-scoped and the legacy form.
    ids = _load_processed_ids()
    folder = (row or {}).get("folder") or ""
    for key in (_processed_key(account_id, uid, folder),
                _legacy_processed_key(account_id, uid)):
        ids.discard(key)
    _save_processed_ids(ids)

    if not row:
        return {"account_id": account_id, "uid": uid, "reprocessed": True, "immediate": False}

    msg = {
        "uid": row["imap_uid"],
        "message_id": row["message_id"] or "",
        "from": row["from_addr"] or "",
        "to": row["to_addr"] or "",
        "cc": row["cc_addr"] or "",
        "subject": row["subject"] or "",
        "body_text": row["body_text"] or "",
        "body_html": row["body_html"] or "",
        "date": row["date_received"],
        "folder": row["folder"] or "INBOX",
        "attachments": [],
        "raw_headers": "",
    }
    mailbox_cfg = {"account_id": account_id, "inbox": row["folder"] or "INBOX",
                   # The row already exists in the DB, so it survived retention;
                   # an explicit manual reprocess must not be blocked by the
                   # retention skip.
                   "skip_retention_check": True}
    log_entry = _process_message(msg, mailbox_cfg)
    return {"account_id": account_id, "uid": uid, "reprocessed": True, "immediate": True, "log_status": log_entry.get("status")}


def reprocess_conversations(items: list[int] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reprocess selected messages immediately if they are in the DB.

    Accepts either legacy integer IDs (for backward compatibility) or dicts with
    account_id + uid, which is the correct format because processed keys are scoped
    per-account.
    """
    results = []
    for item in items:
        if isinstance(item, dict):
            account_id = item.get("account_id")
            uid = str(item.get("uid", ""))
        else:
            # Legacy integer path: cannot scope to account, so it won't clear real keys.
            account_id = None
            uid = str(item)
        if account_id is not None and uid:
            result = _reprocess_message_by_uid(account_id, uid)
            results.append(result)
    return results


def retrain_classifier_head(mock_samples: int = 0, use_feedback: bool = True) -> dict[str, Any]:
    try:
        from .train_ml_head import train as _train_ml
        result = _train_ml(use_db=True, mock_samples=mock_samples)
        # Reload the model after training
        if result.get("ok"):
            global ml_model, ml_vectorizer
            ml_model = None
            ml_vectorizer = None
            _init_ml()
        return result
    except ImportError as e:
        logger.warning("ML dependencies not available: %s", e)
        return {"ok": False, "message": f"ML dependencies not installed: {e}"}
    except Exception as e:
        logger.exception("ML retraining failed")
        return {"ok": False, "message": str(e)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger.info("Scanner starting in standalone mode")
    start_scanner()
    while True:
        time.sleep(60)