#!/usr/bin/env python3
"""Telegram "Project:" line for task reminders / forgotten clock-out (2026-11-10).

Live bug: reminders printed ``Project: Unknown Project`` even though the task
was linked to a project, and forgotten clock-outs did too.

Root causes:
  1. ``task_reminders._sweep_once`` inserted the notification WITHOUT
     ``opportunity_id`` (the task query already selected it), so the
     dispatcher's ``LEFT JOIN opportunities`` found nothing.
  2. ``notification_dispatcher._process_pending_notifications`` substituted
     the literal ``"Unknown Project"`` for a missing joined title, and that
     placeholder was then printed for every notification without a project.

Behaviour under test:
  * linked task reminder → real project title in the message;
  * task without a project → no ``Project:`` line at all, no ``Unknown``;
  * forgotten clock-out (time entries have no project) → same omission;
  * legacy rows with NULL opportunity_id still resolve via the task in
    ``payload->>'taskId'`` (SQL fallback);
  * ``task_reminders`` writes ``opportunity_id`` from the task row.

Telegram is fully mocked — no network, no bot token.
Run with: python3 tests/test_notification_project_line.py
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import network_guard  # noqa: E402

network_guard.install()

import notification_dispatcher as nd  # noqa: E402
import task_reminders  # noqa: E402


def _task_reminder_row(project_title, payload=None):
    return {
        "id": 501,
        "user_id": 2,
        "type": "task_reminder",
        "message": "Reminder: Update mdm on the reconciliation",
        "payload": payload if payload is not None else {
            "taskId": 900,
            "taskTitle": "Update mdm on the reconciliation",
        },
        "opportunity_id": 1245 if project_title else None,
        "actor_user_id": None,
        "telegram_sent": False,
        "email_sent": True,
        "actor_name": None,
        "project_title": project_title,
        "project_number": 1245 if project_title else None,
    }


def _timesheet_row(project_title=None):
    return {
        "id": 502,
        "user_id": 2,
        "type": "timesheet_overdue",
        "message": "Clocked in for 10h 0m",
        "payload": {"entry_id": "29", "threshold_hours": 10, "elapsed": "10h 0m"},
        "opportunity_id": None,
        "actor_user_id": None,
        "telegram_sent": False,
        "email_sent": True,
        "actor_name": None,
        "project_title": project_title,
        "project_number": None,
    }


def _send_with_row(row):
    """Run the dispatcher for one mocked notification; return the sent text."""
    sent: list[str] = []

    def fake_send(chat_id, text, reply_to_message_id=None):
        sent.append(text)
        return 9001

    with patch.object(nd.db, "query_dicts", return_value=[row]), \
         patch.object(nd, "_user_telegram_enabled", return_value=True), \
         patch.object(nd, "_is_customer", return_value=False), \
         patch.object(nd, "_get_employee_chat_id", return_value=12345), \
         patch.object(nd, "_claim_telegram_notification", return_value=True), \
         patch.object(nd, "_send_telegram_message", side_effect=fake_send), \
         patch.object(nd, "_log_telegram_message"), \
         patch.object(nd, "_mark_telegram_sent"):
        nd._process_pending_notifications()
    assert len(sent) == 1, f"expected exactly one Telegram send, got {len(sent)}"
    return sent[0]


class TestProjectLine(unittest.TestCase):
    def test_linked_task_reminder_prints_real_project(self):
        text = _send_with_row(_task_reminder_row("Tony Gosz (Bonnie Gosz)"))
        self.assertIn("Project:</b>", text)
        self.assertIn("Tony Gosz (Bonnie Gosz)", text)
        self.assertNotIn("Unknown", text)

    def test_task_without_project_omits_project_line(self):
        text = _send_with_row(_task_reminder_row(None))
        self.assertNotIn("Project:", text)
        self.assertNotIn("Unknown", text)
        # The reminder body itself is still delivered.
        self.assertIn("Update mdm on the reconciliation", text)

    def test_forgotten_clock_out_omits_project_line(self):
        text = _send_with_row(_timesheet_row(None))
        self.assertNotIn("Project:", text)
        self.assertNotIn("Unknown", text)
        self.assertIn("Forgotten clock-out?", text)

    def test_forgotten_clock_out_with_project_prints_name(self):
        text = _send_with_row(_timesheet_row("Tony Gosz (Bonnie Gosz)"))
        self.assertIn("Project:</b>", text)
        self.assertIn("Tony Gosz (Bonnie Gosz)", text)
        self.assertNotIn("Unknown", text)

    def test_project_link_helper_omits_when_title_blank(self):
        self.assertEqual(nd._project_link("", 1245, 1245), "")
        self.assertNotIn("Project", nd._project_link("", 1245, 1245))
        self.assertIn("Tony Gosz", nd._project_link("Tony Gosz", 1245, 1245))

    def test_no_placeholder_string_left_in_sources(self):
        for path in (HERE.parent / "notification_dispatcher.py",
                     HERE.parent / "server.py",
                     HERE.parent / "telegram_bot.py",
                     HERE.parent / "task_reminders.py"):
            self.assertNotIn("Unknown Project", path.read_text(encoding="utf-8"),
                             f"{path.name} still contains the placeholder")

    def test_dispatcher_sql_falls_back_to_task_project(self):
        src = (HERE.parent / "notification_dispatcher.py").read_text(encoding="utf-8")
        self.assertIn("COALESCE(n.opportunity_id, tk.opportunity_id)", src)
        self.assertIn("n.payload->>'taskId'", src)
        # The cast is guarded so non-numeric payloads cannot break other types.
        self.assertIn("~ '^[0-9]+$'", src)


class TestTaskReminderInsert(unittest.TestCase):
    def test_reminder_notification_carries_opportunity_id(self):
        task = {
            "id": 900, "title": "Update mdm on the reconciliation",
            "owner_user_id": 2, "remind_at": None, "repeat": "none",
            "opportunity_id": 1245,
        }
        executed: list[tuple] = []

        def fake_execute(sql, params=None):
            executed.append((sql, params))
            return 1

        with patch.object(task_reminders.db, "query_dicts", return_value=[task]), \
             patch.object(task_reminders.db, "execute", side_effect=fake_execute), \
             patch.object(task_reminders.event_bus, "publish") as publish:
            count = task_reminders._sweep_once()

        self.assertEqual(count, 1)
        inserts = [(sql, p) for sql, p in executed if "INSERT INTO notifications" in sql]
        self.assertEqual(len(inserts), 1, "expected one reminder notification insert")
        sql, params = inserts[0]
        self.assertIn("opportunity_id", sql, "INSERT must set opportunity_id")
        self.assertIn(1245, params, "task's project id must be part of the INSERT params")
        published = [c.args[1] for c in publish.call_args_list
                     if c.args and c.args[0] == "notification:new"]
        self.assertEqual(published[0].get("opportunityId"), 1245)

    def test_reminder_for_unlinked_task_inserts_null(self):
        task = {
            "id": 901, "title": "Private todo", "owner_user_id": 2,
            "remind_at": None, "repeat": "none", "opportunity_id": None,
        }
        executed: list[tuple] = []

        with patch.object(task_reminders.db, "query_dicts", return_value=[task]), \
             patch.object(task_reminders.db, "execute",
                          side_effect=lambda sql, params=None: executed.append((sql, params)) or 1), \
             patch.object(task_reminders.event_bus, "publish"):
            task_reminders._sweep_once()

        inserts = [(sql, p) for sql, p in executed if "INSERT INTO notifications" in sql]
        self.assertEqual(len(inserts), 1)
        self.assertIsNone(inserts[0][1][2], "unlinked task must store NULL project")


if __name__ == "__main__":
    unittest.main(verbosity=2)
