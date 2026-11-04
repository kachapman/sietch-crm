"""IMAP flag/folder helpers shared by the Sietch mail server.

Extracted from server.py so the mail IMAP logic lives in one place and
server.py stays a thin wrapper. server.py still owns the request handlers
and the DB writes; this module owns the IMAP protocol work.

Background (live bug 2026-11-03, MDM outlook.com): opening an email in the
Sietch mail modal marked it read in Sietch but NOT in Outlook. Causes:

  1. the stored OAuth access token was used as-is (no expiry check, no
     refresh) — a stale token means the UID STORE never reaches the server;
  2. `imap_tools` `MailBox.flag()` runs EXPUNGE after every UID STORE
     (imap_tools 1.14 mailbox.py:351), so each read/unread toggle
     permanently expunged every \\Deleted message in that folder;
  3. the stored UID was trusted blindly — after a move the message has a
     NEW UID (UIDs are only unique per folder), so the flag hit nothing or
     the wrong message;
  4. failures were logged at warning level and swallowed.

All of that is fixed here:
  - flags are set with a plain `UID STORE` (never EXPUNGE);
  - the target is verified by Message-ID before anything is stored, and a
    mismatched message is never flagged;
  - an authentication failure refreshes the token and retries once.
"""

from __future__ import annotations

import email as _email
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("sietch.mail_imap")

# ── Per-message sync-failure markers ──────────────────────────────────────────
# Keyed by mail message id. Value: {"at": iso, "action": ..., "error": ...}
# Surfaced through the API as "sync_failure" so the UI can tell the user a
# change (e.g. "mark read") did not reach their mailbox.
_sync_failures: dict[int, dict] = {}
_sync_failures_lock = threading.Lock()

# Bounded: this is a status display, not an audit log.
SYNC_FAILURE_LIMIT = 500

# The marker is ALSO persisted in the existing `mail_flag_queue` table
# (message_id, flag_name, flag_value, queued_at, pushed). That table has
# always existed in the schema but was never used; a row with pushed = FALSE
# means "this flag operation did not reach IMAP". Using it needs NO
# migration. The detailed error text stays in memory; the persisted row
# carries the fact, the action and the timestamp.
_FLAG_NAME_BY_ACTION = {
    "read": "\\Seen",
    "unread": "\\Seen",
    "star": "\\Flagged",
    "move": "\\Move",
}


def _persist_failure(message_id: int, action: str, db: Any = None) -> None:
    """Record the failed flag operation in `mail_flag_queue` (pushed = FALSE)."""
    try:
        import db as db_module
        target = db or db_module
        flag_name = _FLAG_NAME_BY_ACTION.get(action, "\\Seen")
        # Replace any previous pending row for this message+flag so the
        # queue does not grow without bound.
        target.execute(
            "DELETE FROM mail_flag_queue WHERE message_id = %s AND flag_name = %s",
            (message_id, flag_name),
        )
        target.execute(
            "INSERT INTO mail_flag_queue (message_id, flag_name, flag_value, queued_at, pushed) "
            "VALUES (%s, %s, %s, NOW(), FALSE)",
            (message_id, flag_name, action == "read"),
        )
    except Exception:
        # Persisting the marker is best-effort — never fail the sync because
        # of it (the in-memory marker is the primary source).
        logger.exception("Could not persist the mail sync failure for message %s", message_id)


def _clear_persisted_failure(message_id: int, action: str, db: Any = None) -> None:
    """Drop the persisted pending row once the flag reached IMAP."""
    try:
        import db as db_module
        target = db or db_module
        flag_name = _FLAG_NAME_BY_ACTION.get(action, "\\Seen")
        target.execute(
            "DELETE FROM mail_flag_queue WHERE message_id = %s AND flag_name = %s AND pushed = FALSE",
            (message_id, flag_name),
        )
    except Exception:
        logger.exception("Could not clear the persisted mail sync failure for message %s", message_id)

# ── Folder discovery ──────────────────────────────────────────────────────────
# The folders a message may live in. Resolved from the account's IMAP LIST
# output (which carries Outlook's own names such as "Sent Items"), with a
# static fallback for when LIST is unavailable.
_STATIC_FALLBACK_FOLDERS = (
    "INBOX",
    "Sent Items", "Sent", "Sent Mail", "Sent Messages",
    "Deleted Items", "Trash",
    "Junk Email", "Junk",
    "Archive",
    "Drafts", "Draft",
    "Outbox",
)

# Cache of the resolved folder list per account, so a click does not re-LIST.
_folder_cache: dict[int, list[str]] = {}
_folder_cache_lock = threading.Lock()

# Special-use flags from the IMAP LIST response (\Sent \Trash \Junk ...).
# A folder carrying one of these is a candidate for a moved message.
_SPECIAL_USE_FLAGS = ("\\sent", "\\trash", "\\junk", "\\archive", "\\drafts")


def record_sync_failure(message_id: int, action: str, error: Any, db: Any = None) -> None:
    """Remember that an IMAP flag sync failed for a message (shown in the UI).

    Persisted in the existing `mail_flag_queue` table (pushed = FALSE), so the
    marker survives a restart and is visible to every worker.
    """
    try:
        with _sync_failures_lock:
            _sync_failures[int(message_id)] = {
                "at": datetime.now(timezone.utc).isoformat(),
                "action": action,
                "error": str(error)[:400],
            }
            if len(_sync_failures) > SYNC_FAILURE_LIMIT:
                # dicts preserve insertion order — drop the oldest entries.
                for old_id in list(_sync_failures.keys())[: len(_sync_failures) - SYNC_FAILURE_LIMIT]:
                    _sync_failures.pop(old_id, None)
    except Exception:
        logger.exception("Failed to record mail sync failure for message %s", message_id)
    _persist_failure(message_id, action, db)


def clear_sync_failure(message_id: int, action: str | None = None, db: Any = None) -> None:
    """Drop a previously recorded sync failure (the sync worked)."""
    try:
        with _sync_failures_lock:
            _sync_failures.pop(int(message_id), None)
    except Exception:
        logger.exception("Failed to clear mail sync failure for message %s", message_id)
    if action:
        _clear_persisted_failure(message_id, action, db)


def sync_failure(message_id: int) -> dict | None:
    """Return the recorded sync failure for a message, if any."""
    with _sync_failures_lock:
        return _sync_failures.get(int(message_id))


def persisted_failure(message_id: int, db: Any = None) -> dict | None:
    """Return the persisted sync failure for a message (from mail_flag_queue).

    This survives a restart, so a failure recorded before the process died
    is still reported to the user.
    """
    try:
        import db as db_module
        target = db or db_module
        row = target.query_one(
            "SELECT flag_name, flag_value, queued_at FROM mail_flag_queue "
            "WHERE message_id = %s AND pushed = FALSE ORDER BY queued_at DESC LIMIT 1",
            (message_id,),
        )
    except Exception:
        logger.exception("Could not read the persisted mail sync failure for message %s", message_id)
        return None
    if not row:
        return None
    flag_name = row.get("flag_name") or ""
    action = "read" if flag_name == "\\Seen" and row.get("flag_value") else (
        "unread" if flag_name == "\\Seen" else ("star" if flag_name == "\\Flagged" else "sync")
    )
    queued_at = row.get("queued_at")
    return {
        "at": queued_at.isoformat() if hasattr(queued_at, "isoformat") else "",
        "action": action,
        "error": "not synced to the mail server",
        "persisted": True,
    }


def persisted_failures(message_ids: list[int], db: Any = None) -> dict[int, dict]:
    """Batch version of persisted_failure() — one query for many messages."""
    if not message_ids:
        return {}
    try:
        import db as db_module
        target = db or db_module
        rows = target.query_dicts(
            "SELECT DISTINCT ON (message_id) message_id, flag_name, flag_value, queued_at "
            "FROM mail_flag_queue WHERE message_id = ANY(%s) AND pushed = FALSE "
            "ORDER BY message_id, queued_at DESC",
            (message_ids,),
        )
    except Exception:
        logger.exception("Could not read the persisted mail sync failures")
        return {}
    out: dict[int, dict] = {}
    for row in rows:
        flag_name = row.get("flag_name") or ""
        action = "read" if flag_name == "\\Seen" and row.get("flag_value") else (
            "unread" if flag_name == "\\Seen" else ("star" if flag_name == "\\Flagged" else "sync")
        )
        queued_at = row.get("queued_at")
        out[row["message_id"]] = {
            "at": queued_at.isoformat() if hasattr(queued_at, "isoformat") else "",
            "action": action,
            "error": "not synced to the mail server",
            "persisted": True,
        }
    return out


def normalize_message_id(value: str | None) -> str:
    """Normalize a Message-ID for comparison (strip <> and whitespace)."""
    return str(value or "").strip().strip("<>").strip()


def is_synthetic_uid(uid: str | None) -> bool:
    """Return True for locally-generated UIDs that do not exist on the IMAP server."""
    return bool(uid) and str(uid).startswith("sent:outgoing:")


def is_auth_error(err: Exception) -> bool:
    """True when an IMAP error looks like an authentication/authorization failure.

    Deliberately conservative: a false positive costs one wasted token
    refresh, a false negative costs a silent sync failure. The bare word
    "authenticate" is NOT a marker on its own (it appears in benign
    messages such as "server does not support AUTHENTICATE").
    """
    text = f"{type(err).__name__}: {err}".lower()
    markers = (
        "mailboxloginerror",
        "authentication failed",
        "authentication was cancelled",
        "authentication unsuccessful",
        "no credentials",
        "invalid credentials",
        "unauthorized",
        "invalid token",
        "expired token",
        "token expired",
        "invalidauthorization",
        # IMAP/XOAUTH2 auth-failure response codes. The code must carry its
        # dot-version ("535 5.7.3") or be labelled ("code 535"), so a bare
        # UID like 535 never triggers a pointless token refresh.
        "535 5.7",
        "334 5.7",
        "code 535",
        "code 334",
        "status 535",
        "status 334",
        "response 535",
        "response 334",
    )
    return any(m in text for m in markers)


def store_flags(mb: Any, uid: str, flag: str, value: bool) -> None:
    """Set/unset a system flag with a plain UID STORE — never EXPUNGE.

    imap_tools' MailBox.flag() runs EXPUNGE after every STORE, which
    permanently removes every \\Deleted message in the folder. A read/unread
    toggle must never delete mail, so we issue the UID STORE ourselves.
    """
    client = getattr(mb, "client", None)
    if client is None:
        raise RuntimeError("IMAP client unavailable")
    op = "+FLAGS" if value else "-FLAGS"
    result = client.uid("STORE", str(uid), op, f"({flag})")
    status = (result or ("",))[0]
    if status != "OK":
        raise RuntimeError(f"UID STORE {op} {flag} failed: {status}")


def uid_message_id(mb: Any, uid: str) -> str:
    """Fetch the Message-ID header of a UID in the currently selected folder."""
    result = mb.client.uid("FETCH", str(uid), "(BODY.PEEK[HEADER])")
    if not result or result[0] != "OK":
        return ""
    raw = b""
    for part in (result[1] or []):
        if isinstance(part, tuple) and len(part) > 1 and isinstance(part[1], (bytes, bytearray)):
            raw += bytes(part[1])
    if not raw:
        return ""
    try:
        msg = _email.message_from_bytes(raw)
    except Exception:
        return ""
    return normalize_message_id(msg.get("Message-ID") or "")


def list_folders(mb: Any) -> list[str]:
    """Return the folder names the account exposes.

    Uses the IMAP LIST output when available, so Outlook's own names
    ("Sent Items", "Deleted Items", "Junk Email") are used instead of
    guessed ones. Falls back to a static list.
    """
    names: list[str] = []
    try:
        for folder in mb.folder.list():
            name = getattr(folder, "name", None) or getattr(folder, "path", None) or ""
            if name:
                names.append(str(name))
    except Exception as e:
        logger.error("IMAP: folder list failed: %s", e)
    if not names:
        return list(_STATIC_FALLBACK_FOLDERS)
    # Keep the static fallbacks as a safety net for servers whose LIST is
    # incomplete, but put the real names first.
    merged = list(names)
    for fallback in _STATIC_FALLBACK_FOLDERS:
        if fallback not in merged:
            merged.append(fallback)
    return merged


def account_folders(account_id: int, mb: Any) -> list[str]:
    """Return (and cache) the folder list for an account."""
    with _folder_cache_lock:
        cached = _folder_cache.get(account_id)
    if cached:
        return cached
    folders = list_folders(mb)
    with _folder_cache_lock:
        _folder_cache[account_id] = folders
    return folders


def clear_folder_cache(account_id: int | None = None) -> None:
    """Forget the cached folder list (all accounts, or just one)."""
    with _folder_cache_lock:
        if account_id is None:
            _folder_cache.clear()
        else:
            _folder_cache.pop(account_id, None)


def find_uid_by_message_id(mb: Any, folder: str, message_id: str) -> tuple[str, str]:
    """Find a UID for `message_id` in `folder`.

    IMAP `SEARCH HEADER Message-Id` is a SUBSTRING match, so every hit is
    re-fetched (BODY.PEEK) and only an EXACT Message-ID match is accepted.
    The folder is echoed back so the caller can update the stored row when
    the message was moved to another folder.

    Returns (uid, folder), or ("", "") when the message cannot be found.
    """
    from imap_tools import AND, Header
    if not message_id:
        return "", ""
    try:
        mb.folder.set(folder)
        criteria = AND(header=Header("Message-Id", message_id))
        uids = mb.uids(criteria=criteria)
        if not uids:
            return "", ""
        want = normalize_message_id(message_id)
        for candidate in uids:
            actual = uid_message_id(mb, str(candidate))
            if actual and normalize_message_id(actual) == want:
                return str(candidate), folder
        logger.error(
            "IMAP: no exact Message-ID match for %s in folder %s (%d search hits, none exact)",
            want, folder, len(uids),
        )
        return "", ""
    except Exception as e:
        logger.error("IMAP: Message-ID lookup failed (folder %s, message-id %s): %s", folder, message_id, e)
        return "", ""


def resolve_uid(mb: Any, folder: str, uid: str, message_id: str,
                candidate_folders: list[str] | None = None) -> tuple[str, str]:
    """Resolve the UID that really holds `message_id`.

    The stored UID can be stale: after a move/delete the message lives in a
    different folder, and UIDs are only unique per folder. We verify the
    Message-ID of the stored UID first; on mismatch we look the message up
    by Message-ID header — in the folder we expect it in, and in the other
    folders it may have been moved to (the IMAP move and the DB folder
    update are not atomic, so either can be the stale one).

    Returns (uid, folder) — ("", "") when the message could not be located.
    An empty stored Message-ID yields ("", "") too: without it the target
    cannot be verified at all, and a message must never be flagged blind.
    """
    want = normalize_message_id(message_id)
    if not want:
        logger.error(
            "IMAP: message in folder %s has no stored Message-ID — refusing to flag uid %s blind",
            folder, uid,
        )
        return "", ""
    actual = uid_message_id(mb, uid)
    if actual and normalize_message_id(actual) == want:
        return str(uid), folder
    if actual:
        logger.error(
            "IMAP: Message-ID mismatch for uid %s in folder %s (expected %s, got %s) — not flagging",
            uid, folder, want, normalize_message_id(actual),
        )
    # The UID is stale (or gone): find the message by Message-ID.
    candidates = [folder] + [f for f in (candidate_folders or []) if f != folder]
    for candidate in candidates:
        found_uid, found_folder = find_uid_by_message_id(mb, candidate, want)
        if found_uid:
            logger.error(
                "IMAP: located message-id %s at uid %s (folder %s) instead of stale uid %s",
                want, found_uid, found_folder, uid,
            )
            return found_uid, found_folder
    return "", ""


def apply_flag(account_id: int, folder: str, uid: str, flag: str, value: bool,
               message_id: str | None = None, message_db_id: int | None = None,
               connect: Any = None, db: Any = None,
               candidate_folders: list[str] | None = None,
               force_refresh: Any = None) -> bool:
    """Set/unset a system flag on one message, with a single auth-failure retry.

    Steps:
      1. connect (the OAuth token is refreshed first when it is near expiry)
      2. resolve the real UID by Message-ID (never flag a mismatched message)
      3. plain `UID STORE` — never EXPUNGE
      4. on an authentication failure: refresh the token and retry once

    `connect`, `db` and `force_refresh` are injected so this module stays
    free of a dependency on server.py and can be tested in isolation.
    """
    if force_refresh is None:
        import mail_oauth
        force_refresh = mail_oauth.force_refresh_access_token
    last_error: Exception | None = None
    for attempt in (1, 2):
        mb = None
        try:
            mb = connect(account_id)
            if not mb:
                raise RuntimeError("IMAP connect failed (no mailbox)")
            folders = candidate_folders
            if folders is None:
                folders = account_folders(account_id, mb)
            mb.folder.set(folder)
            target_uid, target_folder = resolve_uid(mb, folder, uid, message_id or "",
                                                   folders)
            if not target_uid:
                raise RuntimeError("message not found by Message-ID")
            if target_folder != folder:
                mb.folder.set(target_folder)
            store_flags(mb, target_uid, flag, value)
            # The UID can change when the message was moved — keep the row honest.
            if db is not None and message_db_id and (target_uid != str(uid) or target_folder != folder):
                db.execute(
                    "UPDATE mail_messages SET imap_uid = %s, folder = %s WHERE id = %s",
                    (target_uid, target_folder, message_db_id),
                )
            mb.logout()
            mb = None
            if message_db_id:
                action = {"\\Seen": "read" if value else "unread", "\\Flagged": "star"}.get(flag, "sync")
                clear_sync_failure(message_db_id, action, db)
            return True
        except Exception as e:
            last_error = e
            if attempt == 1 and is_auth_error(e):
                logger.error(
                    "IMAP auth failure (account %d, folder %s, uid %s): %s — refreshing token and retrying once",
                    account_id, folder, uid, e,
                )
                try:
                    mb.logout()
                except Exception:
                    pass
                mb = None
                if force_refresh(account_id):
                    continue
                break
            break
        finally:
            if mb is not None:
                try:
                    mb.logout()
                except Exception:
                    pass
    logger.error(
        "IMAP flag sync failed (account %d, message %s, folder %s, uid %s, flag %s, value %s): %s",
        account_id, message_db_id if message_db_id else "-", folder, uid, flag, value, last_error,
    )
    if message_db_id:
        action = {"\\Seen": "read" if value else "unread", "\\Flagged": "star"}.get(flag, "sync")
        record_sync_failure(message_db_id, action, last_error, db)
    return False


def move(account_id: int, folder: str, uid: str, dest_folder: str,
         message_id: str | None = None, message_db_id: int | None = None,
         connect: Any = None, db: Any = None,
         candidate_folders: list[str] | None = None) -> bool:
    """Move a message between IMAP folders.

    The source UID is resolved by Message-ID FIRST. If it cannot be
    resolved the move is ABORTED: moving a stale UID would move whichever
    message now owns that UID.

    After the move the message has a NEW UID in the destination folder (UIDs
    are only unique per folder), so we look it up by Message-ID there and
    store the new UID/folder.
    """
    if is_synthetic_uid(uid):
        return True
    mb = None
    try:
        mb = connect(account_id)
        if not mb:
            raise RuntimeError("IMAP connect failed (no mailbox)")
        folders = candidate_folders
        if folders is None:
            folders = account_folders(account_id, mb)
        mb.folder.set(folder)
        source_uid, source_folder = resolve_uid(mb, folder, uid, message_id or "",
                                                folders)
        if not source_uid:
            # Never move a UID we could not verify — it may belong to a
            # different message by now.
            raise RuntimeError("source message could not be resolved by Message-ID — move aborted")
        if source_folder != folder:
            mb.folder.set(source_folder)
        mb.move([source_uid], dest_folder)
        # Re-find the message in the destination folder to capture its new UID.
        new_uid, new_folder = find_uid_by_message_id(mb, dest_folder, normalize_message_id(message_id))
        if new_uid and db is not None and message_db_id:
            db.execute(
                "UPDATE mail_messages SET imap_uid = %s, folder = %s WHERE id = %s",
                (new_uid, new_folder or dest_folder, message_db_id),
            )
        elif not new_uid and message_id:
            logger.error(
                "IMAP: moved message-id %s to %s but could not re-locate it there",
                message_id, dest_folder,
            )
        mb.logout()
        mb = None
        if message_db_id:
            clear_sync_failure(message_db_id, "move", db)
        return True
    except Exception as e:
        logger.error(
            "IMAP move failed (account %d, folder %s, uid %s → %s): %s",
            account_id, folder, uid, dest_folder, e,
        )
        if message_db_id:
            record_sync_failure(message_db_id, "move", e, db)
        return False
    finally:
        if mb is not None:
            try:
                mb.logout()
            except Exception:
                pass


__all__ = [
    "SYNC_FAILURE_LIMIT",
    "account_folders",
    "apply_flag",
    "clear_folder_cache",
    "clear_sync_failure",
    "find_uid_by_message_id",
    "is_auth_error",
    "is_synthetic_uid",
    "list_folders",
    "move",
    "normalize_message_id",
    "persisted_failure",
    "persisted_failures",
    "record_sync_failure",
    "resolve_uid",
    "store_flags",
    "sync_failure",
    "uid_message_id",
]
