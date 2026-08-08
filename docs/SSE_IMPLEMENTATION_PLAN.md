# SSE (Server-Sent Events) Implementation Plan

**Date:** 2026-08-08  
**Status:** Draft — awaiting approval before implementation

---

## Problem Statement

The Sietch CRM dashboard currently relies on HTTP polling for real-time updates:

| Feature | Current Mechanism | Interval | Requests/Hour/User |
|---------|------------------|----------|-------------------|
| Presence heartbeat | `POST /api/presence/heartbeat` | 30s (visible), 2min (hidden) | ~120 |
| Presence polling | `GET /api/presence` | 30s | ~120 |
| DM polling | `GET /api/presence/dm?with=X` | 30s (when modal open) | ~120 |
| Task refresh | Manual button only | On demand | 0 |
| Notification badge | `GET /api/v2/notifications/unread-count` | 60s | ~60 |

**Total: ~300+ HTTP requests per hour per user** just for real-time awareness. With 10 users, that's 3,000+ requests/hour — most returning unchanged data.

Worse, the 30-second polling cycle means:
- Messages appear with up to 30s delay
- Online status is wrong for up to 30s
- Read receipts are meaningless ("Seen 30s ago")
- Heartbeats waste bandwidth saying "I'm alive" every 30 seconds

## Proposed Solution: Server-Sent Events (SSE)

SSE is a one-way server→client push mechanism over standard HTTP. The server holds a connection open and streams events to the client. The browser's `EventSource` API handles reconnection automatically.

### Why SSE (Not WebSocket)?

| Factor | SSE | WebSocket |
|--------|-----|-----------|
| Direction | Server → Client | Bidirectional |
| Protocol | Standard HTTP | Upgraded protocol |
| Reconnection | Built-in (auto-reconnect) | Manual |
| Infrastructure | Works with existing `ThreadingHTTPServer` | Requires WebSocket library |
| Complexity | ~100 lines Python + ~50 lines JS | ~200+ lines, new dependency |
| Use case fit | Perfect (server push only) | Overkill (client rarely sends) |

For this CRM, the only real-time need is server→client push (new messages, presence changes, task updates). SSE is the right tool.

### Scalability

`ThreadingHTTPServer` holds one thread per SSE connection. For a small-team CRM:
- 10 users × 1 connection = 10 threads (negligible)
- 50 users × 1 connection = 50 threads (fine)
- 100 users × 1 connection = 100 threads (acceptable)
- 500+ users → consider switching to an async framework (FastAPI + uvicorn)

The production deployment has ~12 users. SSE is well within capacity.

---

## Architecture

### Event Bus (In-Memory Broadcaster)

A thread-safe publish/subscribe queue that any part of the server can publish to, and SSE handlers subscribe to.

```python
# server.py — new module-level class

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
        """Subscribe a user to events. Returns a Queue to read from."""
        q = queue.Queue(maxsize=200)
        with self._lock:
            # Clean up old subscription for this user
            old = self._subscribers.get(user_id)
            if old:
                try: old.put_nowait(None)  # signal old consumer to exit
                except queue.Full: pass
            self._subscribers[user_id] = q
        return q
    
    def unsubscribe(self, user_id: str):
        """Remove a user's subscription."""
        with self._lock:
            self._subscribers.pop(user_id, None)
    
    def publish(self, event_type: str, data: dict, user_ids: list[str] = None):
        """Send an event. If user_ids is None, broadcasts to all subscribers."""
        msg = json.dumps({"type": event_type, "data": data, "ts": time.time()})
        with self._lock:
            targets = list(self._subscribers.keys()) if user_ids is None else user_ids
            for uid in targets:
                q = self._subscribers.get(uid)
                if q:
                    try: q.put_nowait(msg)
                    except queue.Full: pass
    
    def publish_all(self, event_type: str, data: dict):
        """Broadcast to all connected users."""
        self.publish(event_type, data, user_ids=None)

# Module-level instance
event_bus = EventBus()
```

### SSE Endpoint

A new `GET /api/events` endpoint that holds the connection open and streams events.

```python
def _handle_sse_stream(self):
    """SSE endpoint — streams real-time events to the client."""
    user = _require_auth(self)
    if not user:
        return
    
    user_id = str(user["id"])
    q = event_bus.subscribe(user_id)
    
    try:
        # Send initial connection event
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")  # nginx proxy bypass
        self.end_headers()
        
        self.wfile.write(b"event: connected\ndata: {}\n\n")
        self.wfile.flush()
        
        while True:
            try:
                msg = q.get(timeout=25)  # 25s timeout → heartbeat
                if msg is None:  # subscription replaced
                    break
                self.wfile.write(f"data: {msg}\n\n".encode())
                self.wfile.flush()
            except queue.Empty:
                # Send heartbeat to keep connection alive
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass  # client disconnected
    finally:
        event_bus.unsubscribe(user_id)
```

### Route Dispatch

Add to `do_GET` handler:

```python
if api_path == "/api/events":
    self._handle_sse_stream()
    return
```

---

## Events to Implement

### Phase 1: Messaging (Highest Impact)

| Event | Trigger | Payload | Subscribers |
|-------|---------|---------|-------------|
| `dm:new` | `POST /api/presence/dm` succeeds | `{from, fromName, to, text, ts, reply_to}` | Recipient only |
| `dm:read` | `POST /api/presence/last-read` | `{reader, with_user, at}` | Sender only |
| `presence:update` | `POST /api/presence/heartbeat` or `POST /api/presence/status` | `{user_id, status, online, autoStatus}` | All users |

**Implementation points in server.py:**

1. After `POST /api/presence/dm` succeeds (~line 7305):
   ```python
   event_bus.publish("dm:new", {
       "from": str(user["id"]),
       "fromName": user.get("displayName", ""),
       "to": str(body["to"]),
       "text": body["text"][:200],
       "ts": msg["ts"],
       "reply_to": body.get("reply_to"),
   }, user_ids=[str(body["to"])])
   ```

2. After `POST /api/presence/last-read` succeeds (~line 7270):
   ```python
   event_bus.publish("dm:read", {
       "reader": str(user["id"]),
       "with_user": str(body["with"]),
       "at": body["at"],
   }, user_ids=[str(body["with"])])
   ```

3. After `POST /api/presence/heartbeat` succeeds (~line 7240):
   ```python
   event_bus.publish("presence:update", {
       "user_id": str(user["id"]),
       "online": True,
       "status": presence_data.get("status", ""),
       "autoStatus": presence_data.get("autoStatus", ""),
   })
   ```

### Phase 2: Tasks (Bonus — Same Connection)

| Event | Trigger | Payload | Subscribers |
|-------|---------|---------|-------------|
| `task:created` | `POST /api/v2/tasks` | `{task}` | All users (or assigned user) |
| `task:updated` | `PUT /api/v2/tasks/{id}` | `{task}` | All users |
| `task:deleted` | `DELETE /api/v2/tasks/{id}` | `{id}` | All users |

### Phase 3: Notifications (Future)

| Event | Trigger | Payload | Subscribers |
|-------|---------|---------|-------------|
| `notification:new` | `INSERT INTO notifications` | `{notification}` | Target user |

---

## Frontend Changes

### SSE Connection Manager

```js
// public/app.js — new module

let _evtSource = null;
let _sseReconnectTimer = null;

function connectSSE() {
  if (_evtSource) return;  // already connected
  
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
    case "dm:new":
      handleNewDM(data);
      break;
    case "dm:read":
      handleDMRead(data);
      break;
    case "presence:update":
      handlePresenceUpdate(data);
      break;
    case "task:created":
    case "task:updated":
    case "task:deleted":
      loadTasks();
      break;
  }
}
```

### Event Handlers

```js
function handleNewDM(data) {
  // If the DM thread with this user is open, refresh it
  const currentDmUser = state.currentDMUser;
  if (currentDmUser && String(currentDmUser) === String(data.from)) {
    refreshDMThread(data.from);
  }
  // Update inbox/unread counts
  refreshPresenceInbox();
  // Update badge
  updateUnreadBadge();
  // Browser notification (if tab is hidden)
  if (document.hidden && Notification.permission === "granted") {
    new Notification(data.fromName || "New message", {
      body: data.text,
      tag: `dm-${data.from}`,
    });
  }
}

function handleDMRead(data) {
  // Update read receipts on sent messages
  updateReadReceipts(data.with_user, data.at);
}

function handlePresenceUpdate(data) {
  // Update the user's online status in the roster
  updatePresenceStatus(data.user_id, data.online, data.status);
}
```

### Lifecycle

```js
// On login / dashboard load
connectSSE();

// On logout / page unload
window.addEventListener("beforeunload", disconnectSSE);

// On visibility change (optional — SSE stays connected when hidden)
// No action needed — SSE handles this automatically
```

---

## What to Remove (After SSE is Working)

| Current Mechanism | Replacement | Savings |
|-------------------|-------------|---------|
| `POST /api/presence/heartbeat` every 30s | SSE connection lifecycle (server detects disconnect) | ~120 req/hr/user |
| `GET /api/presence` polling every 30s | `presence:update` events | ~120 req/hr/user |
| `GET /api/presence/dm` polling every 30s | `dm:new` events | ~120 req/hr/user |
| Manual refresh button on tasks tile | `task:created/updated/deleted` events | UI cleanup |
| 1-hour auto-refresh for tasks | `task:*` events | Background polling |

**Total savings: ~360+ HTTP requests/hour/user**

---

## Migration Strategy

### Phase 1: Add SSE Infrastructure (No Behavior Changes)
1. Add `EventBus` class to server.py
2. Add `GET /api/events` SSE endpoint
3. Add `connectSSE()` / `disconnectSSE()` to frontend
4. Test: verify connection establishes, heartbeats work, reconnection works

### Phase 2: Wire Messaging to SSE (High Impact)
1. Add `event_bus.publish()` calls to DM send, read, presence endpoints
2. Add frontend event handlers for `dm:new`, `dm:read`, `presence:update`
3. Remove 30-second DM polling (keep heartbeat for now)
4. Test: send a message from User A → User B sees it instantly

### Phase 3: Wire Tasks to SSE (Bonus)
1. Add `event_bus.publish()` calls to task create/update/delete endpoints
2. Add frontend handler for `task:*` events
3. Remove manual refresh button from tasks tile
4. Remove 1-hour auto-refresh for tasks
5. Test: create a task → all connected users see it instantly

### Phase 4: Remove Heartbeat Polling (Cleanup)
1. Remove `POST /api/presence/heartbeat` polling from frontend
2. Server detects disconnection via SSE stream closure
3. Update online status calculation to use SSE connection state
4. Keep `beforeunload` beacon as fallback for clean disconnects

### Phase 5: Remove Old Polling Code (Cleanup)
1. Remove `startPresencePolling()` / `fetchPresenceSnapshot()` polling
2. Remove `PRESENCE_POLL_MS` constant
3. Simplify `renderPresenceInbox` to use cached state + SSE updates
4. Clean up dead code

---

## Risk Assessment

| Risk | Mitigation |
|------|-----------|
| Thread exhaustion (many SSE connections) | Monitor thread count; for 50+ users, consider async framework |
| Stale connections (user walks away) | 25s heartbeat timeout detects dead connections |
| Network interruption | `EventSource` auto-reconnects with exponential backoff |
| Server restart | Frontend reconnects automatically; no state loss |
| Memory growth (queued events) | Bounded queues (maxsize=200); old messages dropped |
| nginx proxy buffering | Add `X-Accel-Buffering: no` header + `proxy_buffering off` |

---

## Testing Plan

1. **Unit test EventBus:** publish/subscribe, multi-subscriber, queue overflow
2. **Integration test SSE:** connect, receive event, heartbeat, reconnection
3. **Manual test messaging:** User A sends DM → User B sees it instantly
4. **Manual test presence:** User goes online → other users see green dot instantly
5. **Manual test tasks:** Create task → all users see it without refresh
6. **Load test:** 10 concurrent SSE connections for 1 hour, monitor thread/memory
7. **Disconnect test:** Kill client → server detects within 25s, cleans up subscription

---

## Files to Change

| File | Changes |
|------|---------|
| `server.py` | Add `EventBus` class, SSE endpoint, `event_bus.publish()` calls in DM/presence/task handlers |
| `public/app.js` | Add SSE connection manager, event handlers, remove polling code |
| `docs/SSE_IMPLEMENTATION_PLAN.md` | This document |

---

## Open Questions

1. **Should SSE events include full data or just IDs?** (e.g., `dm:new` sends the full message object, or just `{from, ts}` and the client fetches the full thread?)
   - Recommendation: Send full message object for DMs (small payload), just IDs for tasks (client re-fetches)

2. **Should the notes tile also use SSE?** (note created/updated/deleted events)
   - Low priority — notes are less time-sensitive than messaging

3. **Should we add typing indicators?** (requires client→server push, so WebSocket would be needed)
   - Out of scope for now — SSE handles server→client only

4. **Should SSE work when no modal/tile is open?** (e.g., receive DM notifications while on a different page)
   - Yes — connect SSE on login, not just when the presence tile is open
