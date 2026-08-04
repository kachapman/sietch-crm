"""IMAP mail scanner for Sietch CRM.

Polls configured IMAP mailboxes, classifies incoming emails, and
performs actions (link to deals, create tasks, post notes, apply tags).

Uses imap_tools for IMAP access and stores all email data in the
Sietch CRM PostgreSQL database via db.py.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from imap_tools import A, MailBox, MailMessage, MailBoxFolderManager

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db
from user_profile_store import load_user_profile, save_user_profile
import oauth_providers

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
    # Ensure custom_behaviors is a list
    if "custom_behaviors" not in config:
        config["custom_behaviors"] = []
    return config


def _is_behavior_enabled(behavior_cfg: dict[str, Any], account_id: int | None = None) -> bool:
    """Check if a behavior task is enabled for a given account."""
    if not behavior_cfg.get("enabled", False):
        return False
    accounts = behavior_cfg.get("accounts", "all")
    if accounts == "all":
        return True
    if isinstance(accounts, list) and account_id is not None:
        return account_id in accounts
    return False


def _is_dry_run(behavior_cfg: dict[str, Any], account_id: int | None = None) -> bool:
    """Check if a behavior task is in dry-run mode for a given account."""
    if not behavior_cfg.get("dry_run", False):
        return False
    accounts = behavior_cfg.get("accounts", "all")
    if accounts == "all":
        return True
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
                    """INSERT INTO tasks (title, description, assignee_id, opportunity_id, status, created_at)
                       VALUES (%s, %s, %s, %s, 'open', NOW())""",
                    (title, description, assignee_id, deal_id),
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
                for uid in user_ids:
                    db.execute(
                        """INSERT INTO notifications (user_id, title, message, type, link, created_at)
                           VALUES (%s, %s, %s, 'email_linked', %s, NOW())""",
                        (uid, title, f"New email linked to {ctx['project']}", f"/project/{deal_id}" if deal_id else None),
                    )
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
                            """INSERT INTO tasks (title, assignee_id, opportunity_id, status, created_at)
                               VALUES (%s, %s, %s, 'open', NOW())""",
                            (task_title, task_assignee, new_deal["id"]),
                        )
                    # Notify user if configured
                    notify_uid = config.get("notify_user_id")
                    if notify_uid:
                        db.execute(
                            """INSERT INTO notifications (user_id, title, message, type, link, created_at)
                               VALUES (%s, %s, %s, 'deal_created', %s, NOW())""",
                            (notify_uid, f"New deal created: {title}", f"Auto-created from email '{ctx['subject']}'", f"/project/{new_deal['id']}"),
                        )

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
                    tag = db.query_one("SELECT id FROM tags WHERE title = %s", (title,))
                    if not tag:
                        tag = db.query_one("INSERT INTO tags (title) VALUES (%s) RETURNING id", (title,))
                    if tag:
                        db.execute(
                            "INSERT INTO opportunity_tags (opportunity_id, tag_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
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
                    # Send via SMTP (using account settings)
                    try:
                        acct = db.query_one("SELECT * FROM mail_accounts WHERE id = %s", (account_id,))
                        if acct:
                            from smtp_client import send_email_from_account
                            ok, err = send_email_from_account(
                                smtp_host=acct["smtp_host"] or "",
                                smtp_port=int(acct["smtp_port"] or 587),
                                smtp_user=acct["smtp_user"] or acct["email"],
                                smtp_password=acct.get("smtp_password_encrypted") or "",
                                from_name=acct.get("smtp_from_name"),
                                from_addr=acct["email"],
                                to_addr=ctx["from"],
                                subject=f"Re: {ctx['subject']}",
                                html_body=reply_body,
                                text_body=reply_body,
                                use_tls=bool(acct["smtp_use_tls"]),
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
) -> list[dict[str, Any]]:
    """Execute all enabled custom behaviors for a message."""
    results = []
    config = get_contractors()
    custom_behaviors = config.get("custom_behaviors", [])

    # Sort by order
    sorted_behaviors = sorted(custom_behaviors, key=lambda b: b.get("order", 999))

    for behavior in sorted_behaviors:
        if not behavior.get("enabled", False):
            continue
        if not _is_behavior_enabled(behavior, account_id):
            continue
        result = _execute_custom_behavior(behavior, msg, msg_id, deal_id, account_id)
        result["behavior_type"] = behavior.get("type", "")
        result["dry_run"] = behavior.get("dry_run", False)
        results.append(result)

    return results


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


def _append_log(entry: dict[str, Any]) -> None:
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


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
            import numpy as np
            from sklearn.linear_model import LogisticRegression
            from sklearn.neighbors import KNeighborsClassifier
            from sentence_transformers import SentenceTransformer

            ml_vectorizer = SentenceTransformer("all-MiniLM-L6-v2")
            ml_model = {
                "logistic": LogisticRegression(max_iter=1000),
                "knn": KNeighborsClassifier(n_neighbors=5),
            }
            ML_ENABLED = True
            logger.info("ML head initialized")
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
    if ml_result is None:
        return classification
    if match_strength != "weak":
        return classification
    if ml_result.get("used_ml"):
        log_entry["ml_applied"] = True
    return classification


def _load_processed_ids() -> set[str]:
    return set(_read_json(PROCESSED_IDS_FILE, default=[]) or [])


def _save_processed_ids(ids: set[str]) -> None:
    _write_json(PROCESSED_IDS_FILE, sorted(list(ids)))


def _processed_key(account_id, conv_id: str) -> str:
    # Scope processed-UID tracking per account so a deleted account's UIDs can
    # never suppress another account's messages.
    return f"{account_id}:{conv_id}"


def _mark_processed(account_id, conv_id: str) -> None:
    ids = _load_processed_ids()
    ids.add(_processed_key(account_id, conv_id))
    _save_processed_ids(ids)


def _is_processed(account_id, conv_id: str) -> bool:
    return _processed_key(account_id, conv_id) in _load_processed_ids()


def _refresh_oauth_token_if_needed(account_id: int, provider_name: str, current_refresh_token: str, expires_at: datetime | None) -> tuple[str, str] | None:
    """Refresh OAuth access token if expired. Returns (new_access_token, new_refresh_token) or None."""
    now_ts = time.time()
    if expires_at and expires_at.timestamp() > now_ts + 300:
        return None
    if not current_refresh_token:
        logger.warning("No refresh token for account %d (%s), cannot refresh", account_id, provider_name)
        return None
    prov = oauth_providers.get_provider(provider_name)
    if not prov:
        logger.warning("Unknown provider %s for account %d", provider_name, account_id)
        return None
    try:
        token_data = prov.refresh_token(current_refresh_token)
        new_access = token_data.get("access_token", "")
        new_refresh = token_data.get("refresh_token", current_refresh_token)
        new_expires = time.time() + token_data.get("expires_in", 3600)
        if new_access:
            db.execute(
                "UPDATE mail_accounts SET oauth_access_token = %s, oauth_refresh_token = %s, oauth_token_expires = to_timestamp(%s) WHERE id = %s",
                (new_access, new_refresh, new_expires, account_id),
            )
            return new_access, new_refresh
    except Exception as e:
        logger.error("Failed to refresh OAuth token for account %d: %s", account_id, e)
    return None


def _get_mailboxes() -> list[dict[str, Any]]:
    # Try env vars first (for standalone/testing)
    host = os.environ.get("SCANNER_IMAP_HOST", "")
    port = int(os.environ.get("SCANNER_IMAP_PORT", str(IMAP_PORT_SSL)))
    user = os.environ.get("SCANNER_IMAP_USER", "")
    password = os.environ.get("SCANNER_IMAP_PASSWORD", "")
    inbox = os.environ.get("SCANNER_INBOX", "INBOX")
    if host and user and password:
        return [{"host": host, "port": port, "user": user, "password": password,
                 "inbox": inbox, "use_ssl": port == IMAP_PORT_SSL, "account_id": None,
                 "auth_type": "password"}]

    # Fall back to mail_accounts table
    try:
        rows = db.query_dicts(
            """SELECT id, email, imap_host, imap_port, password_encrypted,
                      oauth_provider, oauth_access_token, oauth_refresh_token,
                      oauth_token_expires, sync_enabled, monitored_folders
               FROM mail_accounts WHERE sync_enabled = TRUE"""
        )
        results = []
        for row in rows:
            if not row.get("imap_host") or not row.get("email"):
                continue
            raw = (row.get("monitored_folders") or "").strip()
            folders = [f.strip() for f in raw.split(",") if f.strip()] if raw else ["INBOX"]
            oauth_provider = row.get("oauth_provider")
            if oauth_provider:
                token_refreshed = _refresh_oauth_token_if_needed(
                    row["id"], oauth_provider,
                    row.get("oauth_refresh_token") or "",
                    row.get("oauth_token_expires"),
                )
                if token_refreshed:
                    access_token = token_refreshed[0]
                else:
                    access_token = row.get("oauth_access_token") or ""
                if not access_token:
                    logger.warning("No access token for OAuth account %d (%s), skipping", row["id"], row["email"])
                    continue
                results.append({
                    "host": row["imap_host"],
                    "port": int(row.get("imap_port", 993)),
                    "user": row["email"],
                    "oauth_token": access_token,
                    "inbox": folders[0],
                    "folders": folders,
                    "use_ssl": int(row.get("imap_port", 993)) == IMAP_PORT_SSL,
                    "account_id": row["id"],
                    "auth_type": "xoauth2",
                })
            else:
                pwd = row.get("password_encrypted", "")
                if not pwd:
                    continue
                results.append({
                    "host": row["imap_host"],
                    "port": int(row.get("imap_port", 993)),
                    "user": row["email"],
                    "password": pwd,
                    "inbox": folders[0],
                    "folders": folders,
                    "use_ssl": int(row.get("imap_port", 993)) == IMAP_PORT_SSL,
                    "account_id": row["id"],
                    "auth_type": "password",
                })
        return results
    except Exception as e:
        logger.error("Failed to load mail accounts: %s", e)
        return []


def _fetch_messages(mailbox_cfg: dict[str, Any], folder: str | None = None, since: datetime | None = None) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    try:
        mailbox = MailBox(mailbox_cfg["host"], port=mailbox_cfg["port"])
        if mailbox_cfg.get("auth_type") == "xoauth2":
            mailbox.xoauth2(mailbox_cfg["user"], mailbox_cfg["oauth_token"])
        else:
            mailbox.login(mailbox_cfg["user"], mailbox_cfg["password"])
        
        folder = _normalize_folder_name(folder or mailbox_cfg.get("inbox", "INBOX"))
        mailbox.folder.set(folder)
        
        criterion = A() if since is None else A(date_gte=since.date())
        
        for msg in mailbox.fetch(criteria=criterion, mark_seen=False, bulk=False):
            attachments = []
            if msg.attachments:
                for att in msg.attachments:
                    attachments.append({
                        "filename": att.filename or "unnamed",
                        "mime_type": att.content_type or "application/octet/octet-stream",
                        "size_bytes": len(att.payload) if att.payload else 0,
                        # imap_tools: att.part is the raw MIME part object (not JSON-serializable);
                        # use content_id which matches the server-side download lookup key.
                        "part_id": att.content_id or "",
                    })
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

            messages.append({
                "uid": msg.uid,
                "message_id": message_id,
                "from": msg.from_values.email if msg.from_values else "",
                "from_name": msg.from_values.name if msg.from_values else "",
                "to": msg.to_values[0].email if msg.to_values else "",
                "subject": msg.subject or "",
                "date": msg.date.isoformat() if msg.date else "",
                "body_text": msg.text or "",
                "body_html": msg.html or "",
                "flags": list(msg.flags) if msg.flags else [],
                "folder": folder,
                "attachments": attachments,
                "raw_headers": raw_headers,
            })
        
        mailbox.logout()
    except Exception as e:
        logger.error("IMAP fetch error: %s", e)
    return messages


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
        rows = db.query(
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
    rows = db.query(
        "SELECT id, title FROM opportunities WHERE id = %s",
        (deal_id,),
    )
    if rows:
        return rows[0]
    return None


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
    deal = None

    # Evaluate user-defined classification rules first
    rule_result = _evaluate_classification_rules(subject, body, from_email)
    if rule_result:
        classification = rule_result["classification"]
        match_strength = "rule_match"
        action = rule_result["action"]
        if rule_result.get("linked_deal_id"):
            linked_deal_id = rule_result["linked_deal_id"]

    # Check auto-link toggle
    config = get_contractors()
    toggles = config.get("scanner_behavior", {})
    auto_link_enabled = toggles.get("auto_link_project_id", True)

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

    # Try [#DEAL-NNN] format (DEAL_ID_RE)
    if auto_link_enabled and classification == "uncertain":
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
            else:
                for m in DEAL_ID_RE.finditer(body):
                    deal = _find_opportunity_by_claim_code(m.group(1))
                    if deal:
                        classification = "claim_code_deal_link"
                        match_strength = "strong"
                        action = "link_deal"
                        linked_deal_id = deal["id"]
                        break

    if classification == "uncertain" and DEAL_ID_RE.search(subject):
        classification = "claim_code_deal_link"
        match_strength = "medium"
        action = "link_deal"

    if classification == "uncertain" and CLAIM_CODE_RE.match(subject.strip()):
        deal = _find_opportunity_by_claim_code(subject.strip())
        if deal:
            classification = "claim_code_deal_link"
            match_strength = "strong"
            action = "link_deal"
            linked_deal_id = deal["id"]
        else:
            classification = "claim_code_no_deal"
            match_strength = "medium"
            action = "link_only"

    ml_result = None
    if classification == "uncertain":
        _init_ml()
        ml_result = _ml_classify(subject, body)
        if ml_result and ml_result.get("used_ml"):
            classification = ml_result.get("logistic", "uncertain")

    return {
        "classification": classification,
        "match_strength": match_strength,
        "action": action,
        "linked_deal_id": linked_deal_id,
        "ml_result": ml_result,
        "subject": subject,
        "from_email": from_email,
        "from_name": from_name,
        "uid": uid,
        "body_preview": body[:500],
    }


def _store_message(msg: dict[str, Any], mailbox_cfg: dict[str, Any]) -> int | None:
    try:
        account_id = mailbox_cfg.get("account_id")
        uid_key = str(msg.get("uid") or "")
        folder = _normalize_folder_name(msg.get("folder") or "INBOX")
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
        attachments_json = json.dumps(attachments) if attachments else None

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
                str(msg.get("uid") or ""),
                msg.get("message_id") or "",
                msg.get("from") or "",
                msg.get("to") or "",
                msg.get("subject") or "",
                msg.get("body_text") or "",
                msg.get("body_html") or "",
                msg.get("date") or None,
                folder,
                is_read,
                False,
                attachments_json,
                msg.get("raw_headers"),
            ),
        )
        msg_id = result["id"] if result else None
        if msg_id:
            _mark_processed(account_id, str(msg.get("uid") or msg_id))
            # Store attachment records and optionally download files
            if attachments and account_id:
                _store_attachments(msg_id, account_id, attachments, msg)
        return msg_id
    except Exception as e:
        logger.error("Failed to store message: %s", e)
        return None


def _store_attachments(message_id: int, account_id: int, attachments: list[dict], msg: dict[str, Any]) -> None:
    """Store attachment metadata in mail_attachments table."""
    for att in attachments:
        try:
            db.execute(
                "INSERT INTO mail_attachments (message_id, filename, mime_type, size_bytes, imap_part_id) "
                "VALUES (%s, %s, %s, %s, %s)",
                (message_id, att.get("filename", "unnamed"), att.get("mime_type", "application/octet/octet-stream"),
                 att.get("size_bytes", 0), att.get("part_id", "")),
            )
        except Exception as e:
            logger.error("Failed to store attachment metadata: %s", e)


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
    }

    if not uid and not msg.get("message_id"):
        log_entry["status"] = "skipped_no_id"
        _append_log(log_entry)
        return log_entry

    if _is_processed(account_id, uid or msg.get("message_id") or ""):
        log_entry["status"] = "already_processed"
        _append_log(log_entry)
        return log_entry

    msg_id = _store_message(msg, mailbox_cfg)
    if msg_id is None:
        log_entry["status"] = "store_failed"
        _append_log(log_entry)
        return log_entry

    classification = _classify_message(msg)
    log_entry["classification"] = classification["classification"]
    log_entry["match_strength"] = classification["match_strength"]
    log_entry["action"] = classification["action"]

    # Read behavior toggles from config
    config = get_contractors()
    toggles = config.get("scanner_behavior", {})

    # --- Auto-link by project ID (primary classifier) ---
    if (classification["action"] == "link_deal" and classification["linked_deal_id"]
            and _is_behavior_enabled(toggles.get("auto_link_project_id", {}), account_id)):
        dry = _is_dry_run(toggles.get("auto_link_project_id", {}), account_id)
        if dry:
            log_entry["status"] = "dry_run"
            log_entry["dry_run_action"] = "link_deal"
            log_entry["dry_run_target"] = classification["linked_deal_id"]
            deal = _find_opportunity_by_deal_id(classification["linked_deal_id"])
            log_entry["dry_run_deal_title"] = deal["title"] if deal else ""
        else:
            linked = _link_email_to_deal(msg_id, classification["linked_deal_id"])
            log_entry["deal_linked"] = linked

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
                "date_sent": str(msg.get("date") or ""),
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
        custom_results = _execute_custom_behaviors(msg, msg_id, linked_deal_id, account_id)
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

    for cfg in mailboxes:
        logger.info("Polling mailbox %s@%s", cfg["user"], cfg["host"])
        try:
            # Sync folder list first
            folder_count = _sync_imap_folders(cfg)
            if folder_count:
                logger.info("Synced %d IMAP folders", folder_count)
            
            folders = cfg.get("folders", [cfg.get("inbox", "INBOX")])
            # Bounded backfill window (SCANNER_SYNC_DAYS, default 90). None = full history.
            try:
                sync_days = int(os.environ.get("SCANNER_SYNC_DAYS", "90"))
            except (TypeError, ValueError):
                sync_days = 90
            since = datetime.now(timezone.utc) - timedelta(days=sync_days) if sync_days and sync_days > 0 else None
            for folder in folders:
                logger.info("Fetching folder %s for %s", folder, cfg["user"])
                msgs = _fetch_messages(cfg, folder, since=since)
                for msg in msgs:
                    result = _process_message(msg, cfg)
                    results.append(result)
        except Exception as e:
            logger.error("Polling error for %s: %s", cfg["host"], e)

    return results


def _enforce_retention() -> None:
    """Delete synced messages older than each account's auto_delete_days (0 = keep forever).

    Runs for every account with auto_delete_days > 0, regardless of sync_enabled.
    Messages linked to deals are always kept. Local DB only — never touches IMAP.
    """
    try:
        rows = db.query_dicts(
            "SELECT id, email, auto_delete_days FROM mail_accounts WHERE auto_delete_days IS NOT NULL AND auto_delete_days > 0"
        )
    except Exception as e:
        logger.error("Retention: failed to fetch accounts: %s", e)
        return
    for acct in rows:
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=int(acct["auto_delete_days"]))
            deleted = db.execute(
                """DELETE FROM mail_messages
                   WHERE account_id = %s
                     AND date_received IS NOT NULL
                     AND date_received < %s
                     AND id NOT IN (SELECT message_id FROM mail_deal_links)""",
                (acct["id"], cutoff),
            )
            if deleted:
                logger.info("Retention: deleted %d old message(s) for %s (older than %d days)", deleted, acct["email"], acct["auto_delete_days"])
        except Exception as e:
            logger.error("Retention: failed for %s: %s", acct["email"], e)


def _scanner_loop() -> None:
    logger.info("Scanner loop started")
    while True:
        try:
            interval = int(os.environ.get("SCANNER_POLL_INTERVAL", "300"))
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
                logger.info("Starting poll cycle")
                _poll_mailboxes()
                _enforce_retention()
                logger.info("Poll cycle complete")
        except Exception as e:
            logger.error("Scanner loop error: %s", e)
        time.sleep(int(os.environ.get("SCANNER_POLL_INTERVAL", "300")))


def start_scanner() -> threading.Thread:
    thread = threading.Thread(target=_scanner_loop, daemon=True, name="sietch-mail-scanner")
    thread.start()
    logger.info("Scanner thread started")
    return thread


def get_scanner_status() -> dict[str, Any]:
    enabled = False
    imap_host = ""
    imap_port = IMAP_PORT_SSL
    imap_user = ""
    try:
        rows = db.query_dicts("SELECT email, imap_host, imap_port FROM mail_accounts WHERE sync_enabled = TRUE LIMIT 1")
        if rows:
            r = rows[0]
            enabled = True
            imap_host = r.get("imap_host") or ""
            imap_port = r.get("imap_port") or IMAP_PORT_SSL
            imap_user = r.get("email") or ""
    except Exception:
        pass
    if not enabled:
        enabled = os.environ.get("SCANNER_ENABLED", "false").lower() in ("true", "1", "yes")
    if not imap_host:
        imap_host = os.environ.get("SCANNER_IMAP_HOST", "")
    if not imap_user:
        imap_user = os.environ.get("SCANNER_IMAP_USER", "")
    return {
        "enabled": enabled,
        "poll_interval": int(os.environ.get("SCANNER_POLL_INTERVAL", "300")),
        "imap_host": imap_host,
        "imap_port": imap_port,
        "imap_user": imap_user,
        "processed_count": len(_load_processed_ids()),
        "log_entries": 0,
        "running": threading.active_count() > 0,
    }


def get_scanner_log(limit: int = 200) -> list[dict[str, Any]]:
    if not LOG_FILE.exists():
        return []
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        entries = [json.loads(l) for l in lines[-limit:] if l.strip()]
        return entries
    except Exception:
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


def record_feedback_candidate(log_entry: dict[str, Any]) -> None:
    try:
        with open(FEEDBACK_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, default=str) + "\n")
    except Exception:
        pass


def store_user_feedback(payload: dict[str, Any]) -> dict[str, Any]:
    record_feedback_candidate(payload)
    return {"ok": True}


def get_feedback_entries(limit: int = 200) -> list[dict[str, Any]]:
    if not FEEDBACK_FILE.exists():
        return []
    try:
        with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return [json.loads(l) for l in lines[-limit:] if l.strip()]
    except Exception:
        return []


def reprocess_conversations(conversation_ids: list[int]) -> list[dict[str, Any]]:
    results = []
    for cid in conversation_ids:
        _mark_processed(None, str(cid))
        results.append({"id": cid, "reprocessed": True})
    return results


def retrain_classifier_head(mock_samples: int = 0, use_feedback: bool = True) -> dict[str, Any]:
    try:
        from .train_ml_head import train as _train_ml
        result = _train_ml(mock_samples=mock_samples, use_feedback=use_feedback)
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