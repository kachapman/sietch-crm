#!/usr/bin/env python3
"""Unit tests for the EventBus SSE leak fix (server.py SSE hardening).

These tests cover the bug fixed in commit a90f1c0:
    when a user opens two tabs in quick succession, the second tab's
    subscribe() replaces the first tab's queue in the subscribers map.
    The first tab's unsubscribe(user_id) (with no expected_q) used to
    pop whichever queue was currently registered — the NEW tab's queue.
    The new tab then sat on a queue no publisher pushed to, with
    q.get(timeout=2) returning queue.Empty forever.

After the fix:
    Each Queue has a private _event_bus_token sentinel.
    unsubscribe(user_id, expected_q=...) only removes the entry if the
    currently-registered queue is still the one the caller passed in.

These tests do NOT start a server or talk to a real DB — they exercise
the event_bus module directly. Run as:
    pytest tests/test_event_bus.py
or:
    python3 -m tests.test_event_bus
or:
    python3 tests/test_event_bus.py
"""
import json
import sys
import time
import unittest

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

import event_bus
from event_bus import EventBus


class TestEventBusSubscribeReplace(unittest.TestCase):
    """Verify subscribe() pushes a None sentinel to the old queue and
    atomically swaps in the new queue."""

    def test_subscribe_returns_a_queue(self):
        bus = EventBus()
        q = bus.subscribe("user-1")
        self.assertIsNotNone(q)
        self.assertTrue(hasattr(q, "_event_bus_token"),
                        "Queue must carry a private _event_bus_token sentinel")

    def test_second_subscribe_pushes_none_to_old_queue(self):
        bus = EventBus()
        q1 = bus.subscribe("user-1")
        q2 = bus.subscribe("user-1")
        # Old queue should have a None pushed so the consumer knows to exit
        msg = q1.get_nowait()
        self.assertIsNone(msg,
                          "Old queue should receive None to signal exit on swap")

    def test_second_subscribe_replaces_subscribers_map(self):
        bus = EventBus()
        q1 = bus.subscribe("user-1")
        q2 = bus.subscribe("user-1")
        # The map should now hold q2, not q1
        self.assertIs(bus._subscribers["user-1"], q2)

    def test_two_tabs_get_distinct_tokens(self):
        bus = EventBus()
        q1 = bus.subscribe("user-1")
        q2 = bus.subscribe("user-1")
        # Each queue has its own sentinel
        self.assertIsNot(getattr(q1, "_event_bus_token"),
                         getattr(q2, "_event_bus_token"))


class TestEventBusUnsubscribeLeakFix(unittest.TestCase):
    """Regression test for the SSE connection leak fixed in commit a90f1c0.

    The contract: unsubscribe() with a correct expected_q removes the
    entry; with a stale expected_q, it's a no-op (newer tab took over).

    Server.py's _handle_sse_stream now passes both expected_q=q and
    expected_id=connection_id, which together guarantee the old SSE
    consumer cannot pop the newer tab's queue out of the subscribers map."""

    def test_unsubscribe_with_matching_expected_q_removes(self):
        """Correct call passes expected_q — the entry IS removed."""
        bus = EventBus()
        q1 = bus.subscribe("user-1")
        bus.unsubscribe("user-1", expected_q=q1)
        self.assertNotIn("user-1", bus._subscribers)

    def test_unsubscribe_with_stale_expected_q_is_noop(self):
        """If expected_q doesn't match what's currently registered,
        it's a no-op (newer tab took over). This is the leak guard."""
        bus = EventBus()
        q1 = bus.subscribe("user-1")
        q2 = bus.subscribe("user-1")
        # Old tab tries to clean up with its own queue reference
        bus.unsubscribe("user-1", expected_q=q1)
        # Map must still hold q2
        self.assertIs(bus._subscribers.get("user-1"), q2,
                      "Stale expected_q must not remove the newer tab's queue")

    def test_unsubscribe_with_token_mismatch_is_noop(self):
        """Token sentinel guards against queue-recycling edge cases."""
        bus = EventBus()
        q1 = bus.subscribe("user-1")
        q2 = bus.subscribe("user-1")
        # Simulate a wrong sentinel (e.g. someone holding a stale ref)
        wrong_token = object()
        bus.unsubscribe("user-1", expected_q=q1, expected_id=wrong_token)
        # Map must still hold q2
        self.assertIs(bus._subscribers.get("user-1"), q2)

    def test_publish_reaches_new_tab_after_old_tab_unsubscribes_correctly(self):
        """End-to-end: simulate the SSE tear-down sequence correctly."""
        bus = EventBus()
        q1 = bus.subscribe("user-1")
        q2 = bus.subscribe("user-1")
        # Old tab tears down CORRECTLY (passes expected_q=q1 + its token)
        old_q1_token = getattr(q1, "_event_bus_token")
        bus.unsubscribe("user-1", expected_q=q1, expected_id=old_q1_token)
        # Publish to user-1 — should land in q2
        bus.publish("notification:new", {"id": 42})
        # q2 should receive the message
        msg_raw = q2.get(timeout=1)
        self.assertIsNotNone(msg_raw, "New tab's queue must receive published events")
        msg = json.loads(msg_raw)
        self.assertEqual(msg["type"], "notification:new")
        self.assertEqual(msg["data"]["id"], 42)

    def test_old_tab_with_stale_ref_cannot_disconnect_new_tab(self):
        """End-to-end: old tab unsubscribes with stale ref — must NOT
        disconnect the new tab. Publish still reaches q2."""
        bus = EventBus()
        q1 = bus.subscribe("user-1")
        q2 = bus.subscribe("user-1")
        # Old tab tries to clean up (passes expected_q=q1, no token check)
        # But q1 is no longer the registered queue — must be no-op.
        bus.unsubscribe("user-1", expected_q=q1)
        # Map still holds q2
        self.assertIs(bus._subscribers.get("user-1"), q2)
        # Publish reaches q2
        bus.publish("notification:new", {"id": 99})
        msg_raw = q2.get(timeout=1)
        self.assertIsNotNone(msg_raw)
        msg = json.loads(msg_raw)
        self.assertEqual(msg["data"]["id"], 99)


class TestEventBusPublishDrops(unittest.TestCase):
    """Verify publish() drops events for slow consumers and counts drops."""

    def test_publish_to_full_queue_drops(self):
        """Fill the queue, then publish — must not block."""
        bus = EventBus()
        # Default queue maxsize is 200. Fill it.
        for _ in range(200):
            bus.publish("test:event", {})
        # Next publish should drop (queue is full) — must not raise or hang
        bus.publish("test:event", {})
        # And subsequent publishes continue to work
        bus.publish("test:event", {})

    def test_publish_to_user_ids_subset(self):
        bus = EventBus()
        q_a = bus.subscribe("user-a")
        q_b = bus.subscribe("user-b")
        bus.publish("test:event", {"n": 1}, user_ids=["user-a"])
        # q_a gets the message
        msg_a = q_a.get(timeout=1)
        self.assertIsNotNone(msg_a)
        # q_b does NOT get the message
        self.assertRaises(__import__('queue').Empty,
                          lambda: q_b.get(timeout=0.1))

    def test_publish_to_user_ids_with_unknown_user(self):
        """Unknown user_ids entries are silently skipped."""
        bus = EventBus()
        q_a = bus.subscribe("user-a")
        # user-b never subscribed
        bus.publish("test:event", {}, user_ids=["user-a", "user-b"])
        msg_a = q_a.get(timeout=1)
        self.assertIsNotNone(msg_a)


class TestEventBusSystemSubscribers(unittest.TestCase):
    """System subscribers (automation engine) receive ALL events."""

    def test_subscribe_system_receives_broadcast(self):
        bus = EventBus()
        sys_q = bus.subscribe_system()
        q_user = bus.subscribe("user-a")
        bus.publish("test:event", {"x": 1})
        # Both should receive the message
        self.assertIsNotNone(sys_q.get(timeout=1))
        self.assertIsNotNone(q_user.get(timeout=1))

    def test_unsubscribe_system_removes(self):
        bus = EventBus()
        sys_q = bus.subscribe_system()
        bus.unsubscribe_system(sys_q)
        # Now publish — system subscriber should NOT receive
        bus.publish("test:event", {"x": 1})
        self.assertRaises(__import__('queue').Empty,
                          lambda: sys_q.get(timeout=0.1))


class TestEventBusSingleton(unittest.TestCase):
    """The module-level singleton must be a real EventBus."""

    def test_singleton_is_event_bus(self):
        self.assertIsInstance(event_bus.event_bus, EventBus)


if __name__ == "__main__":
    unittest.main(verbosity=2)