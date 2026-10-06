"""EventBus — thread-safe pub/sub for real-time SSE events.

Import the singleton `event_bus` from any module:
    from event_bus import event_bus
"""

import json
import queue
import threading
import time


class EventBus:
    """Thread-safe pub/sub for real-time events."""

    def __init__(self):
        self._subscribers: dict[str, queue.Queue] = {}
        self._system_subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self, user_id: str) -> queue.Queue:
        """Subscribe a user to events. Returns a queue that receives JSON messages."""
        q = queue.Queue(maxsize=200)
        # Tag the queue with a private sentinel so a later unsubscribe() with
        # a different queue doesn't accidentally remove the newer tab's
        # queue. Without this, if a user opens 2 tabs in quick succession,
        # the old tab's "I'm done" path would pop the new tab's queue from
        # the subscribers map — leaving the new tab's writer blocked on a
        # queue no publisher pushes to.
        setattr(q, "_event_bus_token", object())
        with self._lock:
            old = self._subscribers.get(user_id)
            if old:
                try:
                    old.put_nowait(None)  # signal old consumer to exit
                except queue.Full:
                    pass
            self._subscribers[user_id] = q
        return q

    def unsubscribe(self, user_id: str, expected_q=None, expected_id=None):
        """Remove a user's subscription.

        If expected_q is given, only remove the entry when it still
        matches the queue we registered. This prevents an old SSE
        consumer that's tearing down from accidentally unsubscribing
        a newer tab that took over the slot.
        """
        with self._lock:
            current = self._subscribers.get(user_id)
            if expected_q is not None and current is not expected_q:
                # Newer tab has replaced us — leave the entry alone.
                return
            if expected_id is not None:
                # Caller is asserting which token it had; verify.
                token = getattr(expected_q, "_event_bus_token", None) if expected_q is not None else None
                if token is not expected_id:
                    return
            self._subscribers.pop(user_id, None)

    def publish(self, event_type: str, data: dict, user_ids: list[str] = None):
        """Publish an event. If user_ids is None, broadcasts to all subscribers."""
        msg = json.dumps({"type": event_type, "data": data, "ts": time.time()})
        dropped_per_sub = 0
        with self._lock:
            targets = list(self._subscribers.keys()) if user_ids is None else user_ids
            for uid in targets:
                q = self._subscribers.get(uid)
                if q:
                    try:
                        q.put_nowait(msg)
                    except queue.Full:
                        # Slow consumer — drop the event for this user. We
                        # count drops rather than logging every one (would
                        # flood stderr at scale).
                        dropped_per_sub += 1
        # Also publish to system subscribers (for automation engine)
        with self._lock:
            for q in self._system_subscribers:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    dropped_per_sub += 1
        if dropped_per_sub:
            import sys as _sys
            print(f"[event_bus] dropped {dropped_per_sub} events for type={event_type} (slow consumers)", file=_sys.stderr)

    def subscribe_system(self) -> queue.Queue:
        """Subscribe to all events (for system components like automation engine)."""
        q = queue.Queue(maxsize=500)
        with self._lock:
            self._system_subscribers.append(q)
        return q

    def unsubscribe_system(self, q: queue.Queue):
        """Remove a system subscription."""
        with self._lock:
            try:
                self._system_subscribers.remove(q)
            except ValueError:
                pass


event_bus = EventBus()  # singleton — safe to import from any module
