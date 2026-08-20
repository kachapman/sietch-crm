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
        """Remove a user's subscription."""
        with self._lock:
            self._subscribers.pop(user_id, None)

    def publish(self, event_type: str, data: dict, user_ids: list[str] = None):
        """Publish an event. If user_ids is None, broadcasts to all subscribers."""
        msg = json.dumps({"type": event_type, "data": data, "ts": time.time()})
        with self._lock:
            targets = list(self._subscribers.keys()) if user_ids is None else user_ids
            for uid in targets:
                q = self._subscribers.get(uid)
                if q:
                    try:
                        q.put_nowait(msg)
                    except queue.Full:
                        pass  # drop event for slow consumers
        # Also publish to system subscribers (for automation engine)
        with self._lock:
            for q in self._system_subscribers:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass

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
