# SSE (Server-Sent Events) Implementation Plan

**Date:** 2026-08-08  
**Status:** Draft v2 — awaiting approval before implementation  
**Updated:** Comprehensive audit of all real-time systems, nginx requirements, multi-tab handling, mail inbox, and production deployment.

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

For this CRM, the only real-time need is server-to-client push (new messages, presence changes, task updates). SSE is the right tool.

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
| Presence heartbeat (hidden) | 2min | Keep-alive for background tabs | **Replace** with SSE connection lifecycle |
| DM polling | 30s | Refresh DM thread | **Replace** with `dm:new` / `dm:read` events |
| Notification badge | 60s | Unread count for bell icon | **Replace** with `notification:new` events |
| Mail inbox auto-refresh | 30s | Refresh mail list when modal open | **Replace** with `mail:new` events |
| Compose draft auto-save | 30s | Auto-save email draft | **Keep** — client-initiated, not server-push |
| Admin sync progress | 3s | Sync status during admin ops | **Keep** — admin-only, transient |

### Background Threads (Server)

| Thread | Interval | Purpose | SSE Interaction |
|--------|----------|---------|----------------|
| Telegram notifier | 5s | Send Telegram messages | Publish `notification:telegram` event |
| Mail scanner | 5min | IMAP poll, classify, link | Publish `mail:new` event when new email arrives |
| Task reminders | 30s | Fire due reminders | Publish `task:reminder` event |
| Backup scheduler | 24h | pg_dump + archive | No SSE interaction |

---

## Architecture

### Event Bus (In-Memory Broadcaster)

```python
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
                try: old.put_nowait(None)  # signal old consumer to exit
                except queue.Full: pass
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
                    try: q.put_nowait(msg)
                    except queue.Full: pass

event_bus = EventBus()
```

### SSE Endpoint

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
        
        while True:
            try:
                msg = q.get(timeout=25)  # 25s timeout -> heartbeat
                if msg is None:
                    break
                self.wfile.write(f"data: {msg}\n\n".encode())
                self.wfile.flush()
            except queue.Empty:
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        event_bus.unsubscribe(user_id)
```

---

## Events to Implement

### Phase 1: Messaging (Highest Impact)

| Event | Trigger | Payload | Subscribers |
|-------|---------|---------|-------------|
| `dm:new` | `POST /api/presence/dm` | `{from, fromName, to, text, ts, reply_to}` | Recipient only |
| `dm:read` | `POST /api/presence/last-read` | `{reader, with_user, at}` | Sender only |
| `presence:update` | `POST /api/presence/heartbeat` or `POST /api/presence/status` | `{user_id, status, online, autoStatus}` | All users |

### Phase 2: Tasks + Notifications (Same Connection)

| Event | Trigger | Payload | Subscribers |
|-------|---------|---------|-------------|
| `task:created` | `POST /api/v2/tasks` | `{task}` | All users |
| `task:updated` | `PUT /api/v2/tasks/{id}` | `{task}` | All users |
| `task:deleted` | `DELETE /api/v2/tasks/{id}` | `{id}` | All users |
| `notification:new` | `INSERT INTO notifications` | `{notification}` | Target user |
| `task:reminder` | `task_reminders.py` fires | `{task_id, title, message}` | Task owner |

### Phase 3: Mail (When Ready)

| Event | Trigger | Payload | Subscribers |
|-------|---------|---------|-------------|
| `mail:new` | `scanner/mail_scanner.py` finds new email | `{account_id, subject, from, preview}` | Account owner |

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

function handleSSEEvent(type, data) {
  switch (type) {
    case "dm:new": handleNewDM(data); break;
    case "dm:read": handleDMRead(data); break;
    case "presence:update": handlePresenceUpdate(data); break;
    case "task:created":
    case "task:updated":
    case "task:deleted": loadTasks(); break;
    case "notification:new": updateNotificationBadge(); break;
    case "mail:new": handleNewMail(data); break;
    case "task:reminder": handleTaskReminder(data); break;
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

function handleNewMail(data) {
  if (state.mailModalOpen) {
    loadMailMessagesForModal(true);
    renderMailUnreadBadge();
  }
}

function handleTaskReminder(data) {
  showToast(`Reminder: ${data.title}`);
  loadTasks();
}
```

---

## Multi-Tab Handling

If a user has multiple tabs open, each tab has its own SSE connection. This is fine — the EventBus delivers events to all subscribers. Each tab independently:
- Receives the same events
- Updates its own UI state
- Sends its own heartbeats (but SSE replaces heartbeats, so this is moot)

**No special multi-tab coordination is needed.**

---

## nginx Configuration (Critical for Production)

The production nginx at `/etc/nginx/sites-enabled/dashboard.publicadjustermidwest.com` MUST be updated:

```nginx
location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    
    # SSE requirements:
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 86400s;
    proxy_set_header Connection "";
    chunked_transfer_encoding off;
}
```

### Why Each Setting Matters

| Setting | Why |
|---------|-----|
| `proxy_http_version 1.1` | HTTP/1.0 does not support keep-alive; SSE needs persistent connections |
| `proxy_buffering off` | nginx buffers responses by default; SSE events would be delayed |
| `proxy_cache off` | Prevents nginx from caching SSE responses |
| `proxy_read_timeout 86400s` | Default 60s timeout would kill SSE connections |
| `proxy_set_header Connection ""` | Prevents nginx from sending `Connection: close` to upstream |
| `chunked_transfer_encoding off` | SSE uses its own framing; chunked encoding interferes |

### Server-Side Header

The SSE endpoint MUST send `X-Accel-Buffering: no` to tell nginx to bypass buffering for this specific response.

---

## Mail Inbox Auto-Refresh

Replace the 30s mail inbox auto-refresh with SSE:

1. Mail scanner publishes `mail:new` events when new emails arrive
2. Frontend handler refreshes inbox if modal is open, updates badge if closed
3. Removes ~120 req/hr/user

---

## Background Thread Integration

### Task Reminders -> SSE

```python
# In task_reminders.py, after inserting notification:
event_bus.publish("task:reminder", {
    "task_id": task_id,
    "title": title,
    "message": message,
}, user_ids=[str(owner_user_id)])
```

### Notification Dispatcher -> SSE

```python
# In notification_dispatcher.py, after inserting notification:
event_bus.publish("notification:new", {
    "id": notification_id,
    "type": notification_type,
    "message": message,
}, user_ids=[str(target_user_id)])
```

### Mail Scanner -> SSE

```python
# In mail_scanner.py, after _store_message:
event_bus.publish("mail:new", {
    "account_id": account_id,
    "subject": subject,
    "from": from_addr,
}, user_ids=[str(owner_user_id)])
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
| Manual refresh button on tasks tile | `task:created/updated/deleted` events | UI cleanup |
| 1-hour panel tile auto-refresh | `task:*` + `dm:*` + `mail:*` events | Background polling |

**Total savings: ~480+ HTTP requests/hour/user**

---

## Migration Strategy

### Phase 1: SSE Infrastructure (No Behavior Changes)
1. Add `EventBus` class to server.py
2. Add `GET /api/events` SSE endpoint
3. Add `connectSSE()` / `disconnectSSE()` to frontend
4. Add `X-Accel-Buffering: no` header to SSE endpoint
5. **Test:** verify connection, heartbeats, reconnection

### Phase 2: Messaging to SSE (Highest Impact)
1. Add `event_bus.publish()` to DM send, read, presence endpoints
2. Add frontend event handlers for `dm:new`, `dm:read`, `presence:update`
3. Remove 30-second DM polling
4. Remove heartbeat polling (SSE connection lifecycle replaces it)
5. Keep `beforeunload` beacon as fallback for clean disconnects
6. **Test:** User A sends DM -> User B sees it instantly

### Phase 3: Tasks + Notifications to SSE (Same Connection)
1. Add `event_bus.publish()` to task CRUD endpoints
2. Add `event_bus.publish()` to task_reminders.py after firing reminders
3. Add `event_bus.publish()` to notification_dispatcher.py after inserting notifications
4. Add frontend handlers for `task:*`, `notification:new`, `task:reminder`
5. Remove manual refresh button from tasks tile
6. Remove 60s notification badge polling
7. **Test:** Create task -> all users see it; reminder fires -> toast appears

### Phase 4: Mail to SSE
1. Add `event_bus.publish()` to mail_scanner.py after storing new emails
2. Add frontend handler for `mail:new`
3. Remove 30s mail inbox auto-refresh
4. **Test:** New email arrives -> mail badge updates, inbox refreshes if open

### Phase 5: Cleanup
1. Remove `startPresencePolling()` / `fetchPresenceSnapshot()` polling
2. Remove `PRESENCE_POLL_MS` constant
3. Remove `startPresenceHeartbeats()` (SSE replaces heartbeats)
4. Remove `PRESENCE_HEARTBEAT_VISIBLE_MS` / `PRESENCE_HEARTBEAT_BG_MS` constants
5. Simplify `renderPresenceInbox` to use cached state + SSE updates
6. Update nginx config in production
7. Clean up dead code

---

## Production Deployment Checklist

| Step | What | Who |
|------|------|-----|
| 1 | Update nginx config on production droplet | Admin |
| 2 | Test SSE locally with nginx proxy | Developer |
| 3 | Deploy server.py changes | `git pull && docker compose build && docker compose up -d` |
| 4 | Deploy frontend changes | Same deploy |
| 5 | Verify SSE connection in browser DevTools (Network -> EventStream) | Developer |
| 6 | Verify messaging works in real-time across tabs | Developer |
| 7 | Monitor thread count for 24 hours | Admin |

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
| Background thread interaction | Threads publish to EventBus; SSE handlers deliver to clients |

---

## Testing Plan

1. **Unit test EventBus:** publish/subscribe, multi-subscriber, queue overflow
2. **Integration test SSE:** connect, receive event, heartbeat, reconnection
3. **Manual test messaging:** User A sends DM -> User B sees it instantly
4. **Manual test presence:** User goes online -> other users see green dot instantly
5. **Manual test tasks:** Create task -> all users see it without refresh
6. **Manual test mail:** New email arrives -> badge updates, inbox refreshes if open
7. **Multi-tab test:** Open 2 tabs -> both receive same events
8. **Reconnection test:** Kill network -> EventSource reconnects automatically
9. **Load test:** 10 concurrent SSE connections for 1 hour, monitor thread/memory
10. **nginx test:** Verify SSE works through nginx proxy in production-like setup

---

## Files to Change

| File | Changes |
|------|---------|
| `server.py` | Add `EventBus` class, SSE endpoint, `event_bus.publish()` calls in DM/presence/task handlers |
| `scanner/mail_scanner.py` | Add `event_bus.publish("mail:new", ...)` after storing new emails |
| `task_reminders.py` | Add `event_bus.publish("task:reminder", ...)` after firing reminders |
| `notification_dispatcher.py` | Add `event_bus.publish("notification:new", ...)` after inserting notifications |
| `public/app.js` | Add SSE connection manager, event handlers, remove polling code |
| `docs/SSE_IMPLEMENTATION_PLAN.md` | This document |

---

## Open Questions

1. **Should SSE events include full data or just IDs?**
   - Recommendation: Full message object for DMs (small payload), just IDs for tasks (client re-fetches)

2. **Should the notes tile also use SSE?**
   - Low priority — notes are less time-sensitive; add `note:created/updated/deleted` events in a future phase

3. **Should we add typing indicators?**
   - Requires client-to-server push -> WebSocket needed. Out of scope for SSE.

4. **Should SSE work when no modal/tile is open?**
   - Yes — connect SSE on login, not just when presence tile is open. Enables background DM notifications.

5. **What about the mail inbox auto-refresh?**
   - Replace with `mail:new` events (Phase 4). Keep unread badge separate.

6. **What about the 1-hour panel tile auto-refresh?**
   - Keep as slow background refresh. SSE handles the real-time updates; auto-refresh catches anything SSE might miss.
