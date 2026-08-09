# SSE + Multi-Channel Notifications — Implementation Plan

**Date:** 2026-08-08
**Status:** Approved — implementation in progress
**Version:** 3.0 (revised with multi-channel notifications, bug fixes, email channel)

---

## Problem Statement

The Sietch CRM dashboard relies on HTTP polling for real-time updates:

| Feature | Current Mechanism | Interval | Requests/Hour/User |
|---------|------------------|----------|-------------------|
| Presence heartbeat | `POST /api/presence/heartbeat` | 30s (visible), 2min (hidden) | ~120 |
| Presence polling | `GET /api/presence` | 30s | ~120 |
| DM polling | `GET /api/presence/dm?with=X` | 30s (when modal open) | ~120 |
| Notification badge | `GET /api/v2/notifications/unread-count` | 60s | ~60 |
| Health poller | `GET /api/health` | 60s | ~60 |
| Mail inbox auto-refresh | `GET /api/v2/mail/messages` | 30s (when modal open) | ~120 |
| Compose draft auto-save | `POST /api/v2/mail/drafts` | 30s (when composing) | ~120 |
| Panel tile auto-refresh | `loadExpandedDashboardTiles()` | 60 min | ~1 |
| Admin sync progress | `GET /api/v2/admin/sync/status` | 3s (during sync only) | N/A (transient) |

**Total: ~420-540 HTTP requests/hour/user** — most returning unchanged data. With 10 users, that's 4,200-5,400 requests/hour.

Additionally, the notification system currently only supports Telegram delivery. Email notifications are stubbed but not implemented, and there is no real-time in-dashboard push.

---

## Architecture Overview

### Three Notification Channels

```
                    ┌─────────────────┐
                    │  notifications  │
                    │     table       │
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
              ▼              ▼              ▼
     ┌────────────┐  ┌────────────┐  ┌────────────┐
     │  SSE Push  │  │  Telegram  │  │   Email    │
     │ (instant)  │  │  (5s poll) │  │ (immediate)│
     └────────────┘  └────────────┘  └────────────┘

     Sources that INSERT into notifications:
     ┌──────────────────────────────────────┐
     │ server.py           note_tagged      │
     │ DB trigger          task_assigned    │
     │ task_reminders.py   task_reminder    │
     │ mail_scanner.py     email_linked,    │
     │                     deal_created     │
     └──────────────────────────────────────┘
```

### Notification Delivery Flow

1. **Insert**: Source inserts into `notifications` table
2. **SSE publish**: Source (or 5s sweep for DB triggers) calls `event_bus.publish("notification:new", ...)` → delivers to connected dashboard clients instantly
3. **Telegram**: `notification_dispatcher.py` polls every 5s, checks `telegram_sent` flag, sends via Bot API
4. **Email**: `notification_dispatcher.py` polls every 5s, checks `email_sent` flag, sends via SMTP relay

All three channels fire independently. A user with all three enabled gets the notification three ways simultaneously.

---

## Pre-Existing Bugs (Phase 0)

| # | File:Line | Bug | Fix |
|---|-----------|-----|-----|
| 1 | `scanner/mail_scanner.py:255,291` | INSERT uses columns `title`, `link` that **don't exist** on `notifications` table. Actual columns: `message`, `payload`. These inserts fail at runtime. | Change to `INSERT INTO notifications (user_id, message, type, payload, created_at) VALUES (%s, %s, %s, %s::jsonb, NOW())` with `payload` as JSONB containing link/title. |
| 2 | `public/app.js:32080` | Reads `prefs.email` but API returns `prefs.emailDigest` | Change to `prefs.emailDigest` |
| 3 | `public/app.js:32118-32129` | Save handler omits `emailDigest` in payload | Add `emailDigest: emailEl?.checked ? 'immediate' : 'disabled'` |
| 4 | `public/index.html:2302` | Telegram checkbox has `disabled` + "Coming soon" | Remove `disabled` and `title` attributes |
| 5 | `public/index.html:2307` | Email checkbox has `disabled` + "Coming soon" | Remove `disabled`, change label to "Email (immediate)", remove `title` |

---

## Why SSE (Not WebSocket)?

| Factor | SSE | WebSocket |
|--------|-----|-----------|
| Direction | Server -> Client | Bidirectional |
| Protocol | Standard HTTP | Upgraded protocol |
| Reconnection | Built-in (auto-reconnect) | Manual |
| Infrastructure | Works with existing `ThreadingHTTPServer` | Requires WebSocket library |
| Complexity | ~100 lines Python + ~50 lines JS | ~200+ lines, new dependency |
| Use case fit | Perfect (server push only) | Overkill (client rarely sends) |

### Scalability

`ThreadingHTTPServer` holds one thread per SSE connection:

| Users | SSE Connections | Impact |
|-------|----------------|--------|
| 12 (current) | 12 threads | Negligible |
| 50 | 50 threads | Fine |
| 100 | 100 threads | Acceptable |
| 500+ | 500 threads | Consider async framework (FastAPI + uvicorn) |

---

## Complete Inventory of Real-Time Systems

### Frontend Periodic Timers

| Timer | Interval | Purpose | SSE Candidate? |
|-------|----------|---------|----------------|
| Health poller | 60s | Server reachability check | **Keep** — fallback health check |
| Panel tile auto-refresh | 60 min | Refresh all expanded tiles | **Keep** — slow background refresh |
| Presence polling | 30s | User roster + online status | **Replace** with `presence:update` events |
| Presence heartbeat (visible) | 30s | Mark user as online | **Replace** with SSE connection lifecycle |
| Presence heartbeat (hidden) | 2min | Keep-alive for background tabs | **Keep** `beforeunload` beacon only |
| Presence header poll | 30s | Independent tile+badge poll | **Replace** with `presence:update` events |
| DM polling | 30s | Refresh DM thread | **Replace** with `dm:new` / `dm:read` events |
| Notification badge | 60s | Unread count for bell icon | **Replace** with `notification:new` events |
| Mail inbox auto-refresh | 30s | Refresh mail list when modal open | **Replace** with `mail:new` events |
| Compose draft auto-save | 30s | Auto-save email draft | **Keep** — client-initiated, not server-push |
| Admin sync progress | 3s | Sync status during admin ops | **Keep** — admin-only, transient |

### Background Threads (Server)

| Thread | Interval | Purpose | SSE Interaction |
|--------|----------|---------|----------------|
| Notification dispatcher | 5s | Send Telegram + email notifications | Reads `notifications` table; no SSE publish (SSE handled at insert site) |
| Mail scanner | 5min | IMAP poll, classify, link | Publishes `notification:new` + `mail:new` events when new email arrives |
| Task reminders | 30s | Fire due reminders | Publishes `notification:new` + `task:reminder` events |
| Backup scheduler | 24h | pg_dump + archive | No SSE interaction |

---

## Architecture: Event Bus + SSE Endpoint

### EventBus (Standalone Module)

**File: `event_bus.py`** (new — avoids circular imports between server.py, mail_scanner.py, task_reminders.py)

```python
"""EventBus — thread-safe pub/sub for real-time SSE events."""

import threading
import queue
import json
import time


class EventBus:
    """Thread-safe pub/sub for real-time events."""

    def __init__(self):
        self._subscribers = {}  # {user_id: queue.Queue}
        self._lock = threading.Lock()

    def subscribe(self, user_id: str) -> queue.Queue:
        q = queue.Queue(maxsize=200)
        with self._lock:
            old = self._subscribers.get(user_id)
            if old:
                try:
                    old.put_nowait(None)  # signal old consumer to exit
                except queue.Full:
                    pass
            self._subscribers[user_id] = q
        return q

    def unsubscribe(self, user_id: str):
        with self._lock:
            self._subscribers.pop(user_id, None)

    def publish(self, event_type: str, data: dict, user_ids: list[str] = None):
        msg = json.dumps({"type": event_type, "data": data, "ts": time.time()})
        with self._lock:
            targets = list(self._subscribers.keys()) if user_ids is None else user_ids
            for uid in targets:
                q = self._subscribers.get(uid)
                if q:
                    try:
                        q.put_nowait(msg)
                    except queue.Full:
                        pass


event_bus = EventBus()  # singleton — import from any module
```

### SSE Endpoint

Route: `GET /api/events` — added to `_handle_api_get()` in `server.py`.

```python
def _handle_sse_stream(self):
    user = _require_auth(self)
    if not user:
        return

    user_id = str(user["id"])
    q = event_bus.subscribe(user_id)

    try:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")  # nginx: bypass buffering
        self.end_headers()

        self.wfile.write(b"event: connected\ndata: {}\n\n")
        self.wfile.flush()

        last_sweep_ts = time.time()

        while True:
            try:
                msg = q.get(timeout=25)  # 25s timeout -> heartbeat
                if msg is None:
                    break
                self.wfile.write(f"data: {msg}\n\n".encode())
                self.wfile.flush()
            except queue.Empty:
                # Heartbeat + 5s sweep for DB-triggered notifications
                now = time.time()
                if now - last_sweep_ts >= 5:
                    _sse_publish_recent_notifications(user_id, last_sweep_ts)
                    last_sweep_ts = now
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        event_bus.unsubscribe(user_id)
```

### DB Trigger Sweep

Task assignments are inserted by a PostgreSQL `AFTER INSERT ON tasks` trigger (`init.sql:339-361`), which has no access to the Python `event_bus`. A lightweight sweep inside the SSE connection handles this:

```python
def _sse_publish_recent_notifications(user_id: str, since_ts: float):
    """Publish any notifications inserted since since_ts for this user.
    Catches DB-triggered inserts (task assignment) and any future triggers."""
    try:
        rows = db.query(
            "SELECT id, type, message, payload, opportunity_id, actor_user_id "
            "FROM notifications WHERE created_at > to_timestamp(%s) AND user_id = %s",
            (since_ts, int(user_id)),
        )
        for row in rows:
            event_bus.publish("notification:new", {
                "id": row[0], "type": row[1], "message": row[2],
                "payload": row[3], "opportunity_id": row[4],
                "actor_user_id": row[5],
            }, user_ids=[user_id])
    except Exception:
        pass  # non-fatal — will retry on next sweep
```

---

## Events to Implement

| Event | Trigger | Payload | Subscribers |
|-------|---------|---------|-------------|
| `dm:new` | `POST /api/presence/dm` | `{from, fromName, to, text, ts, reply_to}` | Recipient only |
| `dm:read` | `POST /api/presence/last-read` | `{reader, with_user, at}` | Sender only |
| `presence:update` | `POST /api/presence/heartbeat` or `POST /api/presence/status` | `{user_id, status, online, autoStatus}` | All users |
| `task:created` | `POST /api/v2/tasks` | `{task}` | All users |
| `task:updated` | `PUT /api/v2/tasks/{id}` | `{task}` | All users |
| `task:deleted` | `DELETE /api/v2/tasks/{id}` | `{id}` | All users |
| `task:reminder` | `task_reminders.py` fires | `{task_id, title, message}` | Task owner |
| `notification:new` | `INSERT INTO notifications` | `{id, type, message, payload}` | Target user |
| `mail:new` | `scanner/mail_scanner.py` finds new email | `{account_id, subject, from}` | Account owner |

---

## Frontend Changes

### SSE Connection Manager

```js
let _evtSource = null;
let _sseReconnectTimer = null;

function connectSSE() {
  if (_evtSource) return;

  _evtSource = new EventSource("/api/events");

  _evtSource.addEventListener("connected", () => {
    console.log("[SSE] Connected");
  });

  _evtSource.onmessage = (e) => {
    try {
      const { type, data } = JSON.parse(e.data);
      handleSSEEvent(type, data);
    } catch (err) {
      console.error("[SSE] Parse error:", err);
    }
  };

  _evtSource.onerror = () => {
    console.log("[SSE] Connection lost, reconnecting in 5s...");
    _evtSource.close();
    _evtSource = null;
    _sseReconnectTimer = setTimeout(connectSSE, 5000);
  };
}

function disconnectSSE() {
  if (_sseReconnectTimer) clearTimeout(_sseReconnectTimer);
  if (_evtSource) { _evtSource.close(); _evtSource = null; }
}
```

### Event Dispatcher

```js
function handleSSEEvent(type, data) {
  switch (type) {
    case "dm:new":           handleNewDM(data); break;
    case "dm:read":          handleDMRead(data); break;
    case "presence:update":  handlePresenceUpdate(data); break;
    case "task:created":
    case "task:updated":
    case "task:deleted":     loadTasks(); break;
    case "task:reminder":
      showToast(`Reminder: ${data.title || "Task reminder"}`);
      loadTasks();
      break;
    case "notification:new":
      updateNotificationBadge();
      if (state.feedModalOpen) refreshFeed();
      break;
    case "mail:new":
      if (state.mailModalOpen) loadMailMessagesForModal(true);
      renderMailUnreadBadge();
      break;
  }
}
```

### Event Handlers

```js
function handleNewDM(data) {
  const currentDmUser = state.currentDMUser;
  if (currentDmUser && String(currentDmUser) === String(data.from)) {
    refreshDMThread(data.from);
  }
  refreshPresenceInbox();
  updateUnreadBadge();
  if (document.hidden && Notification.permission === "granted") {
    new Notification(data.fromName || "New message", {
      body: data.text, tag: `dm-${data.from}`,
    });
  }
}

function handleDMRead(data) {
  const currentDmUser = state.currentDMUser;
  if (currentDmUser && String(currentDmUser) === String(data.with_user)) {
    refreshDMThread(data.with_user);
  }
}

function handlePresenceUpdate(data) {
  // Update presence roster state and re-render
  if (state.presenceData) {
    const idx = state.presenceData.findIndex(u => String(u.id) === String(data.user_id));
    if (idx >= 0) {
      Object.assign(state.presenceData[idx], data);
    }
    renderPresenceInbox();
  }
}
```

### Lifecycle

- `connectSSE()` called after login (in `initApp()` after `loadUserProfileFromServer`)
- `disconnectSSE()` called on sign-out
- `beforeunload` beacon kept as fallback for clean disconnects

---

## Polling Timers to Remove

| Timer | Variable | Line | Replacement |
|-------|----------|------|-------------|
| `startPresencePolling()` | `state.presencePollTimer` | 14144 | `presence:update` events |
| `startPresenceHeartbeats()` (visible) | `state.presenceHeartbeatTimer` | 14190 | SSE connection lifecycle |
| `startPresenceHeartbeats()` (bg) | `state.presenceHeartbeatBgTimer` | 14240 | Keep `beforeunload` beacon |
| Presence header poll | `presenceHeaderPollTimer` | 16554 | `presence:update` events |
| Notification badge poll | `_notificationBadgePollTimer` | 32910 | `notification:new` events |
| Mail auto-refresh | `_mailInboxAutoRefreshTimer` | 16966 | `mail:new` events |

### Timers to Keep

| Timer | Why |
|-------|-----|
| Health poller (60s) | Fallback when SSE is down |
| Panel tile auto-refresh (60min) | Catches anything SSE misses |
| Compose draft auto-save (30s) | Client-initiated, not server-push |
| Admin sync progress (3s) | Admin-only, transient |

### Constants to Remove

- `PRESENCE_POLL_MS` (line 83)
- `PRESENCE_HEARTBEAT_VISIBLE_MS` (line 84)
- `PRESENCE_HEARTBEAT_BG_MS` (line 85)

---

## Multi-Tab Handling

If a user has multiple tabs open, each tab has its own SSE connection. The EventBus delivers events to all subscribers. Each tab independently receives the same events and updates its own UI state.

**No special multi-tab coordination is needed.**

---

## Email Notification Channel (Phase 4)

### Schema Migration

**File: `migrations/008_add_email_notification_channel.sql`**

```sql
-- Add email tracking to notifications
ALTER TABLE notifications ADD COLUMN email_sent BOOLEAN DEFAULT FALSE;
ALTER TABLE notifications ADD COLUMN email_sent_at TIMESTAMP;

-- Add email branding fields
ALTER TABLE branding ADD COLUMN email_header_color TEXT DEFAULT '#3b82f6';
ALTER TABLE branding ADD COLUMN email_footer_text TEXT DEFAULT 'Sietch CRM — Confidential';
ALTER TABLE branding ADD COLUMN email_from_name TEXT;
```

### Email Formatter

Added to `notification_dispatcher.py`:

```python
def _format_email_notification(notification: dict, project_title: str, actor_name: str,
                                project_id: int | None = None) -> tuple[str | None, str | None]:
    """Build branded HTML email from notification. Returns (subject, html) or (None, None)."""
    branding = _get_branding()
    header_color = branding.get("email_header_color") or "#3b82f6"
    company = branding.get("company_name") or "Sietch CRM"
    footer = branding.get("email_footer_text") or "Sietch CRM — Confidential"
    logo_path = branding.get("logo_path") or "/assets/sietch-logo-2-nobg2.png"
    link = f"{DASHBOARD_URL}/project/{project_id}" if project_id else ""
    base_url = DASHBOARD_URL.rstrip("/")

    ntype = notification.get("type")
    if ntype == "note_tagged":
        subject = f"{actor_name} tagged you on {project_title}"
        body = notification.get("message") or "You were tagged in a note."
    elif ntype == "task_assigned":
        payload = notification.get("payload") or {}
        subject = f"Task assigned: {payload.get('task_title', '')}"
        body = f"You've been assigned a task on {project_title}."
        if payload.get("due_date"):
            body += f" Due: {payload['due_date']}."
    elif ntype == "task_reminder":
        payload = notification.get("payload") or {}
        subject = f"Reminder: {payload.get('taskTitle', 'Task')}"
        body = payload.get("taskTitle") or notification.get("message") or "Task reminder."
    elif ntype == "email_linked":
        subject = f"New email linked to {project_title}"
        body = notification.get("message") or "A new email was linked to this project."
    elif ntype == "deal_created":
        subject = notification.get("message") or "New project created"
        body = notification.get("message") or "A new project was auto-created from an email."
    else:
        return None, None

    cta = ""
    if link:
        cta = (f'<p style="text-align:center;margin:24px 0;">'
               f'<a href="{link}" style="display:inline-block;padding:12px 24px;'
               f'background:{header_color};color:#fff;text-decoration:none;'
               f'border-radius:6px;font-weight:600;">View Project</a></p>')

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<div style="background:{header_color};padding:24px;text-align:center;">
  <h1 style="color:#fff;margin:0;font-size:20px;">{company}</h1>
</div>
<div style="padding:32px 24px;max-width:600px;margin:0 auto;">
  <h2 style="color:#1a1a1a;margin:0 0 16px;">{subject}</h2>
  <p style="color:#333;font-size:15px;line-height:1.6;">{body}</p>
  {cta}
</div>
<div style="padding:16px 24px;text-align:center;color:#999;font-size:12px;border-top:1px solid #eee;">
  {footer}
</div>
</body>
</html>"""

    return subject, html
```

### Dispatcher Changes

Extend `_process_pending_notifications()` to handle both channels:

```python
# For each notification row:
# 1. Telegram (existing logic)
# 2. Email (new)
if not row.get("email_sent"):
    email_pref = _get_email_pref(user_id)
    if email_pref and email_pref != "disabled":
        user_email = _get_user_email(user_id)
        if user_email:
            subject, html = _format_email_notification(row, project_title, actor_name, project_id)
            if subject and html:
                from_name = _get_branding().get("email_from_name") or None
                success = send_email(user_email, subject, html, from_name_override=from_name)
                if success:
                    _mark_email_sent(notification_id)
```

### Branding Admin UI

Add three fields to the branding tab in admin console (same section as existing `primary_color`, `company_name`, etc.):

- **Email Header Color** — color picker (defaults to `primary_color`)
- **Email Footer Text** — text input (defaults to "Sietch CRM — Confidential")
- **Email From Name** — text input (defaults to `SMTP_FROM_NAME` env var)

These are saved via the existing `POST /api/branding` endpoint with the new column names.

### SMTP Integration

Reuses `smtp_client.send_email()`. Minor addition: optional `from_name_override` parameter to allow branding override of `SMTP_FROM_NAME`.

---

## Background Thread Integration

### Task Reminders → SSE

```python
# In task_reminders.py, after inserting notification:
event_bus.publish("notification:new", {
    "id": notification_id,
    "type": "task_reminder",
    "message": f"Reminder: {title}",
    "payload": {"task_id": task_id, "taskTitle": title},
}, user_ids=[str(owner_user_id)])

event_bus.publish("task:reminder", {
    "task_id": task_id,
    "title": title,
}, user_ids=[str(owner_user_id)])
```

### Mail Scanner → SSE

```python
# In mail_scanner.py, after _store_message notification inserts:
event_bus.publish("notification:new", {
    "id": notification_id,
    "type": "email_linked",
    "message": f"New email linked to {project_title}",
    "payload": {"link": f"/project/{deal_id}"},
}, user_ids=[str(uid)])

event_bus.publish("mail:new", {
    "account_id": account_id,
    "subject": subject,
    "from": from_addr,
}, user_ids=[str(owner_user_id)])
```

### Note Tagged → SSE

```python
# In server.py, after note_tagged notification insert:
event_bus.publish("notification:new", {
    "id": notification_id,
    "type": "note_tagged",
    "message": message,
    "payload": {"event_id": event_id, "snippet": snippet},
}, user_ids=[str(uid)])
```

---

## What to Remove (After SSE is Working)

| Current Mechanism | Replacement | Savings |
|-------------------|-------------|---------|
| `POST /api/presence/heartbeat` (30s/2min) | SSE connection lifecycle | ~120 req/hr/user |
| `GET /api/presence` polling (30s) | `presence:update` events | ~120 req/hr/user |
| `GET /api/presence/dm` polling (30s) | `dm:new` / `dm:read` events | ~120 req/hr/user |
| `GET /api/v2/notifications/unread-count` (60s) | `notification:new` events | ~60 req/hr/user |
| `GET /api/v2/mail/messages` auto-refresh (30s) | `mail:new` events | ~120 req/hr/user |

**Total savings: ~540+ HTTP requests/hour/user**

---

## Implementation Phases

### Phase 0: Bug Fixes

1. Fix `scanner/mail_scanner.py` notification column names (title→message, link→payload as JSONB)
2. Fix `public/app.js` email pref key (`prefs.email` → `prefs.emailDigest`)
3. Fix `public/app.js` save handler to include `emailDigest`
4. Enable Telegram checkbox in `public/index.html`
5. Enable Email checkbox in `public/index.html`, update label

### Phase 1: SSE Infrastructure

1. Create `event_bus.py` with `EventBus` class
2. Add `GET /api/events` SSE endpoint to `server.py`
3. Add `connectSSE()` / `disconnectSSE()` to `public/app.js`
4. Add `X-Accel-Buffering: no` header to SSE endpoint
5. **Test:** verify connection, heartbeats, reconnection

### Phase 2: Wire Event Publishing

1. Add `event_bus.publish("notification:new", ...)` after note_tagged insert in `server.py`
2. Add `event_bus.publish()` calls in `task_reminders.py` after notification insert
3. Add `event_bus.publish()` calls in `scanner/mail_scanner.py` after notification inserts
4. Add `event_bus.publish("mail:new", ...)` in `scanner/mail_scanner.py` after `_store_message`
5. Add 5s sweep (`_sse_publish_recent_notifications`) for DB-triggered notifications
6. **Test:** create note → SSE delivers notification; task reminder fires → SSE delivers; new email → SSE delivers

### Phase 3: Replace Polling

1. Remove `startPresencePolling()` / `state.presencePollTimer`
2. Remove `startPresenceHeartbeats()` / `state.presenceHeartbeatTimer` / `state.presenceHeartbeatBgTimer`
3. Remove `presenceHeaderPollTimer`
4. Remove `_notificationBadgePollTimer` / `startNotificationBadgePoll()`
5. Remove `_mailInboxAutoRefreshTimer` / mail auto-refresh logic
6. Remove `PRESENCE_POLL_MS`, `PRESENCE_HEARTBEAT_VISIBLE_MS`, `PRESENCE_HEARTBEAT_BG_MS` constants
7. Add `handleSSEEvent()` dispatcher + individual handlers
8. Keep `beforeunload` beacon as fallback for clean disconnects
9. **Test:** all notifications arrive via SSE; no stale polling timers firing

### Phase 4: Email Notification Channel

1. Create migration `008_add_email_notification_channel.sql` (email_sent, email_sent_at, branding columns)
2. Run migration on database
3. Add `_format_email_notification()` to `notification_dispatcher.py`
4. Add `_get_branding()`, `_get_user_email()`, `_get_email_pref()`, `_mark_email_sent()` helpers
5. Extend dispatcher loop to check `email_sent` alongside `telegram_sent`
6. Add `from_name_override` param to `smtp_client.send_email()`
7. Add email branding fields to admin branding UI (HTML + JS)
8. Wire branding fields into `POST /api/branding` save/load
9. **Test:** enable email pref → create note → receive branded email; verify branding fields save/load

### Phase 5: Cleanup + Docs

1. Remove dead polling code from `app.js`
2. Update `docs/SSE_IMPLEMENTATION_PLAN.md`
3. Update `CHANGELOG.md`
4. Update `AGENTS.md`

### Phase 6: nginx (Production Manual Step)

Add to `/etc/nginx/sites-enabled/dashboard.publicadjustermidwest.com` inside the `location /` block:

```nginx
proxy_http_version 1.1;
proxy_buffering off;
proxy_cache off;
proxy_read_timeout 86400s;
proxy_set_header Connection "";
chunked_transfer_encoding off;
```

Apply manually on production droplet after code deploy.

---

## Files to Change

| File | Phase | Changes |
|------|-------|---------|
| `event_bus.py` | 1 | **New file** — standalone EventBus singleton |
| `server.py` | 1,2 | Import event_bus, SSE endpoint, `event_bus.publish()` after note_tagged insert, `_sse_publish_recent_notifications()` |
| `task_reminders.py` | 2 | Import event_bus, `event_bus.publish()` after notification insert |
| `scanner/mail_scanner.py` | 0,2 | Fix notification column names, import event_bus, `event_bus.publish()` after inserts |
| `notification_dispatcher.py` | 4 | Add email channel: formatter, helpers, extend loop |
| `smtp_client.py` | 4 | Add `from_name_override` param to `send_email()` |
| `init.sql` | 4 | Add `email_sent`, `email_sent_at` to notifications; `email_header_color`, `email_footer_text`, `email_from_name` to branding |
| `public/app.js` | 0,1,3 | Fix prefs, SSE manager + handlers, remove polling timers |
| `public/index.html` | 0 | Enable Telegram/email checkboxes |
| `docs/SSE_IMPLEMENTATION_PLAN.md` | 5 | This document |
| `CHANGELOG.md` | 5 | Release notes |
| `AGENTS.md` | 5 | Session summary |

---

## Production Deployment Checklist

| Step | What | Who |
|------|------|-----|
| 1 | Merge + push code to `main` | Developer |
| 2 | On production: `git pull` | Developer |
| 3 | Run migration `008_add_email_notification_channel.sql` | Developer |
| 4 | `docker compose build && docker compose up -d` | Developer |
| 5 | Update nginx config on production droplet (add SSE settings) | Admin |
| 6 | Verify SSE connection in browser DevTools (Network → EventStream) | Developer |
| 7 | Test: create note → notification appears instantly in dashboard + Telegram + email | Developer |
| 8 | Test: enable/disable prefs → verify channels respect settings | Developer |
| 9 | Monitor thread count for 24 hours | Admin |

---

## Risk Assessment

| Risk | Mitigation |
|------|-----------|
| Thread exhaustion (many SSE connections) | Monitor thread count; for 50+ users, consider async framework |
| Stale connections (user walks away) | 25s heartbeat timeout detects dead connections |
| Network interruption | `EventSource` auto-reconnects with exponential backoff |
| Server restart | Frontend reconnects automatically; no state loss |
| Memory growth (queued events) | Bounded queues (maxsize=200); old messages dropped |
| nginx buffering | `X-Accel-Buffering: no` header + `proxy_buffering off` |
| Multi-tab | Each tab has independent SSE connection; no coordination needed |
| Docker deployment | No changes needed — SSE works over standard HTTP |
| Circular imports | EventBus extracted to standalone `event_bus.py` |
| DB trigger notifications missed | 5s sweep in SSE loop catches all |

---

## Open Questions (Resolved)

1. **Should SSE events include full data or just IDs?** → Full message for small payloads, IDs for tasks (client re-fetches).
2. **Should the notes tile use SSE?** → Deferred — low priority.
3. **Typing indicators?** → Requires WebSocket. Out of scope.
4. **SSE when no modal open?** → Yes — connect on login for background DM notifications.
5. **Email notification cadence?** → Immediate (one email per notification).
6. **Document co-edit notifications?** → Deferred — will revisit when sharing model is defined.
7. **Document commenting notifications?** → Deferred — OnlyOffice doesn't expose comments via webhooks.
