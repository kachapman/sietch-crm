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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from imap_tools import MailBox, MailMessage, MailBoxFolderManager

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import db
from user_profile_store import load_user_profile, save_user_profile

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
        "auto_link_project_id": True,
        "create_deals": False,
        "create_tasks": False,
        "post_notes": False,
        "notify_users": False,
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


def _mark_processed(conv_id: str) -> None:
    ids = _load_processed_ids()
    ids.add(conv_id)
    _save_processed_ids(ids)


def _is_processed(conv_id: str) -> bool:
    return str(conv_id) in _load_processed_ids()


def _get_mailboxes() -> list[dict[str, Any]]:
    # Try env vars first (for standalone/testing)
    host = os.environ.get("SCANNER_IMAP_HOST", "")
    port = int(os.environ.get("SCANNER_IMAP_PORT", str(IMAP_PORT_SSL)))
    user = os.environ.get("SCANNER_IMAP_USER", "")
    password = os.environ.get("SCANNER_IMAP_PASSWORD", "")
    inbox = os.environ.get("SCANNER_INBOX", "INBOX")
    if host and user and password:
        return [{"host": host, "port": port, "user": user, "password": password,
                 "inbox": inbox, "use_ssl": port == IMAP_PORT_SSL, "account_id": None}]

    # Fall back to mail_accounts table
    try:
        rows = db.query_dicts(
            "SELECT id, email, imap_host, imap_port, password_encrypted, sync_enabled "
            "FROM mail_accounts WHERE sync_enabled = TRUE"
        )
        results = []
        for row in rows:
            if not row.get("imap_host") or not row.get("email"):
                continue
            results.append({
                "host": row["imap_host"],
                "port": int(row.get("imap_port", 993)),
                "user": row["email"],
                "password": row.get("password_encrypted", ""),
                "inbox": "INBOX",
                "use_ssl": int(row.get("imap_port", 993)) == IMAP_PORT_SSL,
                "account_id": row.get("id"),
            })
        return results
    except Exception as e:
        logger.error("Failed to load mail accounts: %s", e)
        return []


def _fetch_messages(mailbox_cfg: dict[str, Any], since: datetime | None = None) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    try:
        mailbox = MailBox(mailbox_cfg["host"], port=mailbox_cfg["port"])
        mailbox.login(mailbox_cfg["user"], mailbox_cfg["password"])
        
        folder = mailbox_cfg.get("inbox", "INBOX")
        mailbox.folder.set(folder)
        
        criterion = {}
        if since:
            criterion = {"date": since}
        
        for msg in mailbox.fetch(criteria=criterion, mark_seen=False, bulk=False):
            attachments = []
            if msg.attachments:
                for att in msg.attachments:
                    attachments.append({
                        "filename": att.filename or "unnamed",
                        "mime_type": att.content_type or "application/octet/octet-stream",
                        "size_bytes": len(att.payload) if att.payload else 0,
                        "part_id": att.part or "",
                    })
            # Build raw headers string from imap_tools headers object
            raw_headers = ""
            if hasattr(msg, "headers") and msg.headers:
                header_lines = []
                for key in ("from", "to", "cc", "subject", "date", "message-id", "in-reply-to", "references", "content-type"):
                    val = msg.headers.get(key)
                    if val:
                        header_lines.append(f"{key}: {val}")
                raw_headers = "\n".join(header_lines)

            messages.append({
                "uid": msg.uid,
                "message_id": msg.message_id,
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


def _sync_imap_folders(mailbox_cfg: dict[str, Any]) -> int:
    imap_folders = _list_imap_folders(mailbox_cfg)
    account_id = mailbox_cfg.get("account_id")
    if not imap_folders or not account_id:
        return 0
    count = 0
    for f in imap_folders:
        name = f["name"]
        flags = f.get("flags", [])
        icon = "folder"
        if "\\Sent" in str(flags): icon = "send"
        elif "\\Drafts" in str(flags): icon = "file"
        elif "\\Trash" in str(flags): icon = "trash"
        elif "\\Junk" in str(flags): icon = "alert-triangle"
        elif "\\Archive" in str(flags): icon = "archive"
        existing = db.query_one(
            "SELECT id FROM mail_folders WHERE imap_account_id = %s AND imap_path = %s",
            (account_id, name),
        )
        if existing:
            db.execute("UPDATE mail_folders SET last_sync = NOW(), icon = %s WHERE id = %s", (icon, existing["id"]))
        else:
            db.execute(
                "INSERT INTO mail_folders (name, icon, folder_type, imap_account_id, imap_path) "
                "VALUES (%s, %s, 'imap', %s, %s)",
                (name, icon, account_id, name),
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


def _find_opportunity_by_claim_code(claim_code: str) -> dict[str, Any] | None:
    rows = db.query(
        "SELECT id, title FROM opportunities WHERE id IN "
        "(SELECT opportunity_id FROM opportunity_custom_field_values WHERE custom_field_id = 11 AND field_value = %s)",
        (claim_code,),
    )
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


def _post_note_to_deal(opp_id: int, content: str, notify_users: list[str] | None = None) -> bool:
    try:
        db.execute(
            "INSERT INTO history_events (opportunity_id, user_id, category, content, created_at) "
            "VALUES (%s, %s, %s, %s, NOW())",
            (opp_id, None, "Email", content),
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
        existing = db.query_one(
            "SELECT id FROM mail_messages WHERE imap_uid = %s AND folder = %s",
            (str(msg.get("uid") or ""), msg.get("folder") or "INBOX"),
        )
        if existing:
            return existing["id"]

        account_id = mailbox_cfg.get("account_id")
        attachments = msg.get("attachments", [])
        attachments_json = json.dumps(attachments) if attachments else None

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
                msg.get("folder") or "INBOX",
                False,
                False,
                attachments_json,
                msg.get("raw_headers"),
            ),
        )
        msg_id = result["id"] if result else None
        if msg_id:
            _mark_processed(str(msg.get("uid") or msg_id))
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
    log_entry = {
        "timestamp": _now_et(),
        "uid": uid,
        "subject": msg.get("subject", ""),
        "from": msg.get("from", ""),
        "folder": msg.get("folder", "INBOX"),
        "mailbox": mailbox_cfg.get("inbox", ""),
    }

    if not uid and not msg.get("message_id"):
        log_entry["status"] = "skipped_no_id"
        _append_log(log_entry)
        return log_entry

    if _is_processed(uid or msg.get("message_id") or ""):
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

    if classification["action"] == "link_deal" and classification["linked_deal_id"]:
        linked = _link_email_to_deal(msg_id, classification["linked_deal_id"])
        log_entry["deal_linked"] = linked
        if linked and toggles.get("post_notes", False):
            deal = _find_opportunity_by_deal_id(classification["linked_deal_id"])
            if deal:
                body_text = _email_body_for_note(msg)
                from_addr = msg.get("from") or ""
                note_content = (
                    f"Email from {from_addr} (subject: {msg.get('subject', '')})\n\n"
                    f"{body_text[:2000]}"
                )
                _post_note_to_deal(deal["id"], note_content)
                log_entry["note_posted"] = True

    if classification["classification"] == "claim_code_no_deal":
        log_entry["status"] = "linked_no_deal"
    elif classification["action"] == "link_deal":
        log_entry["status"] = "processed"
    else:
        log_entry["status"] = "processed"

    _append_log(log_entry)
    return log_entry


def _poll_mailboxes() -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    mailboxes = _get_mailboxes()
    if not mailboxes:
        logger.info("No IMAP mailboxes configured")
        return results

    for cfg in mailboxes:
        logger.info("Polling mailbox %s@%s", cfg["user"], cfg["host"])
        try:
            # Sync folder list first
            folder_count = _sync_imap_folders(cfg)
            if folder_count:
                logger.info("Synced %d IMAP folders", folder_count)
            
            msgs = _fetch_messages(cfg)
            for msg in msgs:
                result = _process_message(msg, cfg)
                results.append(result)
        except Exception as e:
            logger.error("Polling error for %s: %s", cfg["host"], e)

    return results


def _scanner_loop() -> None:
    logger.info("Scanner loop started")
    while True:
        try:
            interval = int(os.environ.get("SCANNER_POLL_INTERVAL", "300"))
            if os.environ.get("SCANNER_ENABLED", "false").lower() not in ("true", "1", "yes"):
                logger.info("Scanner disabled, sleeping %ds", interval)
            else:
                logger.info("Starting poll cycle")
                _poll_mailboxes()
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
    return {
        "enabled": os.environ.get("SCANNER_ENABLED", "false").lower() in ("true", "1", "yes"),
        "poll_interval": int(os.environ.get("SCANNER_POLL_INTERVAL", "300")),
        "imap_host": os.environ.get("SCANNER_IMAP_HOST", ""),
        "imap_port": int(os.environ.get("SCANNER_IMAP_PORT", str(IMAP_PORT_SSL))),
        "imap_user": os.environ.get("SCANNER_IMAP_USER", ""),
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
        _mark_processed(str(cid))
        results.append({"id": cid, "reprocessed": True})
    return results


def retrain_classifier_head(mock_samples: int = 300, use_feedback: bool = True) -> dict[str, Any]:
    return {"ok": False, "message": "ML retraining not fully implemented"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger.info("Scanner starting in standalone mode")
    start_scanner()
    while True:
        time.sleep(60)