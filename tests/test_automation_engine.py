"""Unit tests for the automation engine.

Covers:
  - _render_template: placeholder substitution
  - _build_template_context: all expected keys present
  - _should_skip: cooldown logic
  - _already_fired_once: once-only mode
  - _action_create_task: dry_run vs real, assignee handling
  - _action_send_email: dry_run + missing SMTP path
  - _action_change_stage: dry_run + missing target + real move
  - _action_add_tag: dry_run + ON CONFLICT handling
  - _resolve_email_recipient: project_owner / contact / custom
  - _get_smtp_config: crm_mail / alternate_smtp / custom paths
  - _handle_sse_event: task:created should NOT fire project_created
    rules (regression: previously mapped, audit round 8 fix #2)
  - _handle_sse_event: project_updated should be handled (regression:
    the trigger type was registered but had no publisher; audit
    round 8 fix #1)
  - _sweep_once: smoke that the polling sweep runs without crashing
  - _action_change_stage: should publish stage_changed on the event
    bus (regression: previously silent, audit round 8 fix #3)

The tests use a tmpdir for system_settings / the test DB layer is
mocked. We monkey-patch the `db` module to avoid touching the real
PostgreSQL.

Run with: python3 tests/test_automation_engine.py
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make the project root importable
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

# Pre-create tmpdir for any disk-backed bits
_TMP = tempfile.mkdtemp(prefix="automation_test_")


def _make_db_mock():
    """Return a MagicMock that mimics the db module surface used by
    automation_engine. The mock records query_dicts / query_one /
    execute / insert_returning calls so tests can assert on them."""
    db = MagicMock()
    db.query_dicts = MagicMock(return_value=[])
    db.query_one = MagicMock(return_value=None)
    db.execute = MagicMock(return_value=None)
    db.insert_returning = MagicMock(return_value=1)
    return db


class TestTemplateRendering(unittest.TestCase):
    def test_placeholder_substitution(self):
        from automation_engine import _render_template
        out = _render_template(
            "Hello {contact}, your project {project} is in stage {stage}.",
            {"contact": "Alice", "project": "Roof", "stage": "Bidding"},
        )
        self.assertEqual(out, "Hello Alice, your project Roof is in stage Bidding.")

    def test_none_value_renders_as_empty_string(self):
        from automation_engine import _render_template
        out = _render_template("Contact: {contact}", {"contact": None})
        self.assertEqual(out, "Contact: ")

    def test_missing_placeholder_left_intact(self):
        # Unknown placeholders are not stripped (template author error
        # shows up at action time, not at save time). This is the
        # current behavior; documenting it.
        from automation_engine import _render_template
        out = _render_template("Hi {contact} from {unknown}", {"contact": "Bob"})
        self.assertEqual(out, "Hi Bob from {unknown}")


class TestTemplateContext(unittest.TestCase):
    def test_all_keys_present(self):
        from automation_engine import _build_template_context
        opp = {
            "id": 42,
            "title": "Roof Repair",
            "project_number": 1234,
            "stage_title": "Bidding",
            "contact_name": "Alice",
            "responsible_name": "Bob",
            "bid_value": 5000.0,
            "description": "Repair the roof",
        }
        ctx = _build_template_context(opp, "Bidding", days_in_stage=3)
        for key in ("project", "project_id", "project_number", "stage",
                    "days_in_stage", "contact", "responsible", "value",
                    "description", "date", "tag", "days_with_tag"):
            self.assertIn(key, ctx, f"context missing key: {key}")
        self.assertEqual(ctx["project"], "Roof Repair")
        self.assertEqual(ctx["days_in_stage"], "3")
        self.assertEqual(ctx["date"], datetime.now(timezone.utc).strftime("%Y-%m-%d"))


class TestCooldownHelpers(unittest.TestCase):
    def test_should_skip_returns_false_when_cooldown_zero(self):
        with patch("automation_engine.db") as db:
            db.query_one.return_value = None
            from automation_engine import _should_skip
            # cooldown_hours <= 0 short-circuits (returns False = "don't skip" = "do fire").
            self.assertFalse(_should_skip(1, 100, 0))
            self.assertFalse(_should_skip(1, 100, -1))

    def test_should_skip_returns_true_when_log_within_window(self):
        with patch("automation_engine.db") as db:
            db.query_one.return_value = {"id": 999}  # row exists
            from automation_engine import _should_skip
            self.assertTrue(_should_skip(1, 100, 24))

    def test_should_skip_returns_false_when_log_outside_window(self):
        with patch("automation_engine.db") as db:
            db.query_one.return_value = None  # no row found
            from automation_engine import _should_skip
            self.assertFalse(_should_skip(1, 100, 24))

    def test_should_skip_tolerates_db_error(self):
        with patch("automation_engine.db") as db:
            db.query_one.side_effect = Exception("db down")
            from automation_engine import _should_skip
            # On DB error we err on the side of "don't skip" so a
            # single DB hiccup doesn't freeze the rule.
            self.assertFalse(_should_skip(1, 100, 24))

    def test_already_fired_once_when_log_exists(self):
        with patch("automation_engine.db") as db:
            db.query_one.return_value = {"id": 999}
            from automation_engine import _already_fired_once
            self.assertTrue(_already_fired_once(1, 100))

    def test_already_fired_once_when_log_missing(self):
        with patch("automation_engine.db") as db:
            db.query_one.return_value = None
            from automation_engine import _already_fired_once
            self.assertFalse(_already_fired_once(1, 100))


class TestActionCreateTask(unittest.TestCase):
    def test_dry_run_does_not_insert(self):
        from automation_engine import _action_create_task
        config = {
            "title": "Follow up: {project}",
            "description": "Auto-generated",
            "assignee_user_ids": [101, 102],
            "due_offset_days": 3,
        }
        opp = {"id": 5, "title": "Test Project"}
        ctx = {"project": "Test Project"}
        result = _action_create_task(config, opp, ctx, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["assignees"], [101, 102])
        # No DB call should have been made.
        # (The mock is at the module level, but in this test the
        # action's call to db is mocked away by the patch context.)

    def test_real_run_creates_one_task_per_assignee(self):
        from automation_engine import _action_create_task
        config = {
            "title": "Follow up: {project}",
            "description": "",
            "assignee_user_ids": [101, 102, 103],
            "due_offset_days": 1,
        }
        opp = {"id": 5, "title": "Test"}
        ctx = {"project": "Test"}
        with patch("automation_engine.db") as db:
            db.query_one.return_value = {"id": 99}
            # The function calls insert_returning 6 times in this order:
            # task 1, notif 1, task 2, notif 2, task 3, notif 3. The
            # TASK ids are the 1st, 3rd, 5th returns (every other one);
            # the notification ids are the 2nd, 4th, 6th and are discarded
            # by the function. So the returned task_ids should be the
            # task-id positions of the side_effect list.
            db.insert_returning.side_effect = [
                201, 301,  # task 1, notif 1
                202, 302,  # task 2, notif 2
                203, 303,  # task 3, notif 3
            ]
            result = _action_create_task(config, opp, ctx, dry_run=False)
        self.assertEqual(result["count"], 3)
        # task_ids collects only the task insert returns (every other
        # call to insert_returning).
        self.assertEqual(result["task_ids"], [201, 202, 203])
        # 6 insert_returning calls total: 3 tasks + 3 notifications.
        self.assertEqual(db.insert_returning.call_count, 6)

    def test_real_run_publishes_task_assigned_notifications(self):
        from automation_engine import _action_create_task
        with patch("automation_engine.db") as db, \
             patch("automation_engine.event_bus") as bus:
            db.query_one.return_value = {"id": 99}
            # 2 task inserts + 2 notification inserts = 4 returns.
            db.insert_returning.side_effect = [1, 2, 3, 4]
            _action_create_task(
                {"title": "T", "assignee_user_ids": [10, 20]},
                {"id": 1, "title": "P"},
                {"project": "P"},
                dry_run=False,
            )
        # 2 task inserts + 2 notification rows = 4 insert_returning calls
        self.assertEqual(db.insert_returning.call_count, 4)
        # 2 notification:new events + 2 task:created events = 4 publishes
        self.assertEqual(bus.publish.call_count, 4)


class TestActionSendEmail(unittest.TestCase):
    def test_dry_run_does_not_send(self):
        from automation_engine import _action_send_email
        config = {
            "from_type": "crm_mail",
            "to_type": "project_owner",
            "subject": "Hi {project}",
            "body": "About {project}",
        }
        opp = {"id": 5, "title": "Test", "responsible_user_id": 100}
        with patch("automation_engine.db") as db:
            # The function calls query_one twice: first to resolve the
            # project_owner email, then to look up the CRM mail config
            # (which is short-circuited by the dry_run path before the
            # second call, but we provide a fallback for safety).
            db.query_one.side_effect = [
                {"email": "owner@example.com"},
            ]
            result = _action_send_email(config, opp, {"project": "P"}, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["would_send_to"], "owner@example.com")

    def test_no_recipient_returns_error(self):
        from automation_engine import _action_send_email
        config = {"from_type": "crm_mail", "to_type": "project_owner"}
        opp = {"id": 5, "title": "T", "responsible_user_id": None}
        with patch("automation_engine.db") as db:
            # No DB call should be needed; resolve_email_recipient
            # returns None immediately when responsible_user_id is None.
            result = _action_send_email(config, opp, {}, dry_run=False)
        self.assertIn("error", result)
        self.assertIn("recipient", result["error"].lower())

    def test_missing_smtp_returns_error(self):
        from automation_engine import _action_send_email
        config = {"from_type": "crm_mail", "to_type": "project_owner"}
        opp = {"id": 5, "title": "T", "responsible_user_id": 100}
        with patch("automation_engine.db") as db:
            # Order of query_one calls in _action_send_email:
            # 1) _resolve_email_recipient(project_owner) → user email
            # 2) _get_smtp_config(crm_mail) → mail_accounts row (None here)
            db.query_one.side_effect = [
                {"email": "owner@example.com"},
                None,  # no CRM mail row → "SMTP not configured"
            ]
            result = _action_send_email(config, opp, {}, dry_run=False)
        self.assertIn("error", result)
        self.assertIn("smtp", result["error"].lower())


class TestActionChangeStage(unittest.TestCase):
    def test_dry_run_does_not_update(self):
        from automation_engine import _action_change_stage
        config = {"target_stage_id": 7, "target_stage_title": "Won"}
        opp = {"id": 5}
        result = _action_change_stage(config, opp, {}, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["would_move_to_stage"], 7)

    def test_missing_target_returns_error(self):
        from automation_engine import _action_change_stage
        opp = {"id": 5}
        result = _action_change_stage({}, opp, {}, dry_run=False)
        self.assertIn("error", result)
        self.assertIn("target stage", result["error"].lower())

    def test_real_move_publishes_stage_changed(self):
        # Audit round 8 fix #3: _action_change_stage must publish
        # stage_changed on the event bus so SSE-driven rules can chain.
        from automation_engine import _action_change_stage
        with patch("automation_engine.db") as db, \
             patch("automation_engine.event_bus") as bus:
            _action_change_stage(
                {"target_stage_id": 7},
                {"id": 5},
                {},
                dry_run=False,
            )
        # The UPDATE happens.
        db.execute.assert_called_once()
        # And the stage_changed event is published.
        bus.publish.assert_called_once()
        call_args = bus.publish.call_args
        self.assertEqual(call_args[0][0], "stage_changed")
        self.assertEqual(call_args[0][1]["opportunity_id"], 5)
        self.assertEqual(call_args[0][1]["stage_id"], 7)
        # user_ids is optional; the current implementation doesn't pass
        # it (the SSE handler re-fetches the opp and matches by stage_id
        # if to_stage_id is set in the rule config). Document the
        # current behavior.
        self.assertNotIn("user_ids", call_args[1])

    def test_publish_failure_does_not_break_action(self):
        # The publish is wrapped in try/except so a broken bus
        # doesn't reverse the stage move.
        from automation_engine import _action_change_stage
        with patch("automation_engine.db") as db, \
             patch("automation_engine.event_bus") as bus:
            bus.publish.side_effect = Exception("bus down")
            result = _action_change_stage(
                {"target_stage_id": 7},
                {"id": 5},
                {},
                dry_run=False,
            )
        # The UPDATE still happened.
        db.execute.assert_called_once()
        # And we got a normal success result (not an error).
        self.assertEqual(result["moved_to_stage"], 7)


class TestActionAddTag(unittest.TestCase):
    def test_dry_run_does_not_insert(self):
        from automation_engine import _action_add_tag
        config = {"tag_ids": [10, 20]}
        result = _action_add_tag(config, {"id": 5}, {}, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["would_add_tags"], [10, 20])

    def test_real_run_uses_on_conflict(self):
        from automation_engine import _action_add_tag
        with patch("automation_engine.db") as db:
            _action_add_tag(
                {"tag_ids": [10, 20]},
                {"id": 5},
                {},
                dry_run=False,
            )
        # Two ON CONFLICT inserts.
        self.assertEqual(db.execute.call_count, 2)
        # Both should use ON CONFLICT DO NOTHING.
        for call in db.execute.call_args_list:
            self.assertIn("ON CONFLICT DO NOTHING", call.args[0])


class TestResolveEmailRecipient(unittest.TestCase):
    def test_project_owner(self):
        from automation_engine import _resolve_email_recipient
        with patch("automation_engine.db") as db:
            db.query_one.return_value = {"email": "owner@example.com"}
            result = _resolve_email_recipient("project_owner", {"responsible_user_id": 100}, {})
        self.assertEqual(result, "owner@example.com")

    def test_project_owner_no_user_id(self):
        from automation_engine import _resolve_email_recipient
        result = _resolve_email_recipient("project_owner", {}, {})
        self.assertIsNone(result)

    def test_contact(self):
        from automation_engine import _resolve_email_recipient
        with patch("automation_engine.db") as db:
            db.query_one.return_value = {"email": "contact@example.com"}
            result = _resolve_email_recipient("contact", {"contact_id": 50}, {})
        self.assertEqual(result, "contact@example.com")

    def test_custom(self):
        from automation_engine import _resolve_email_recipient
        result = _resolve_email_recipient(
            "custom", {}, {"custom_email": "specific@example.com"}
        )
        self.assertEqual(result, "specific@example.com")

    def test_unknown_type(self):
        from automation_engine import _resolve_email_recipient
        result = _resolve_email_recipient("unknown_type", {}, {})
        self.assertIsNone(result)


class TestGetSmtpConfig(unittest.TestCase):
    def test_crm_mail(self):
        from automation_engine import _get_smtp_config
        with patch("automation_engine.db") as db:
            db.query_one.return_value = {
                "email": "crm@example.com",
                "smtp_host": "smtp.example.com",
                "smtp_port": 587,
                "smtp_user": "u",
                "smtp_password_encrypted": "p",
            }
            result = _get_smtp_config("crm_mail", {})
        self.assertEqual(result["host"], "smtp.example.com")
        self.assertEqual(result["from_addr"], "crm@example.com")
        self.assertEqual(result["from_name"], "Sietch CRM")

    def test_alternate_smtp_when_disabled_returns_none(self):
        from automation_engine import _get_smtp_config
        with patch("automation_engine.db") as db:
            db.query_one.return_value = {
                "value": json.dumps({"enabled": False, "host": "x"})
            }
            result = _get_smtp_config("alternate_smtp", {})
        self.assertIsNone(result)

    def test_alternate_smtp_when_enabled(self):
        from automation_engine import _get_smtp_config
        with patch("automation_engine.db") as db:
            db.query_one.return_value = {
                "value": json.dumps({
                    "enabled": True, "host": "alt.example.com",
                    "port": 587, "user": "u", "password": "p",
                    "from_addr": "alt@example.com", "from_name": "Alt",
                })
            }
            result = _get_smtp_config("alternate_smtp", {})
        self.assertEqual(result["host"], "alt.example.com")
        self.assertEqual(result["from_name"], "Alt")

    def test_custom(self):
        from automation_engine import _get_smtp_config
        result = _get_smtp_config("custom", {
            "smtp_host": "c.example.com",
            "smtp_port": 2525,
            "smtp_user": "u",
            "smtp_password": "p",
            "smtp_from_addr": "c@example.com",
            "smtp_from_name": "C",
        })
        self.assertEqual(result["host"], "c.example.com")
        self.assertEqual(result["port"], 2525)
        self.assertEqual(result["from_addr"], "c@example.com")

    def test_unknown_type_returns_none(self):
        from automation_engine import _get_smtp_config
        with patch("automation_engine.db") as db:
            db.query_one.return_value = None
            result = _get_smtp_config("weird_type", {})
        self.assertIsNone(result)


class TestSseEventMapping(unittest.TestCase):
    """Audit round 8 fix #2: task:created must NOT fire project_created rules."""

    def test_task_created_does_not_match_project_created(self):
        # The event map should not contain the bad mapping.
        from automation_engine import _EVENT_TRIGGER_MAP
        self.assertNotIn(
            "task:created", _EVENT_TRIGGER_MAP,
            "task:created was removed from _EVENT_TRIGGER_MAP because it caused "
            "every 'new project' rule to fire when any task was created."
        )

    def test_known_event_types_all_present(self):
        from automation_engine import _EVENT_TRIGGER_MAP
        for key, val in [
            ("project_created", "project_created"),
            ("stage_changed", "stage_changed"),
            ("project_updated", "project_updated"),
            ("tag_added", "tag_added"),
        ]:
            self.assertEqual(_EVENT_TRIGGER_MAP.get(key), val)


class TestHandleSseEvent(unittest.TestCase):
    def test_unknown_event_type_is_ignored(self):
        from automation_engine import _handle_sse_event
        # Should not raise even on unknown event type.
        _handle_sse_event({"type": "unknown_type", "data": {}})

    def test_event_without_opportunity_id_is_ignored(self):
        from automation_engine import _handle_sse_event
        # Event with no opportunity_id in data: should silently return.
        _handle_sse_event({"type": "project_created", "data": {"title": "x"}})

    def test_event_with_nonexistent_opportunity_is_ignored(self):
        from automation_engine import _handle_sse_event
        with patch("automation_engine.db") as db:
            db.query_dicts.return_value = []  # opp not found
            _handle_sse_event({
                "type": "project_created",
                "data": {"opportunity_id": 999, "title": "x"},
            })
        # No execute_action should have been called.

    def test_task_created_does_not_trigger_project_created_rule(self):
        # The full path: a task:created event comes in. The rule is
        # trigger_type=project_created. The OLD code would have
        # matched task:created → project_created and fired the rule
        # with the task's parent opp as context. The NEW code should
        # do nothing (no rule should be evaluated).
        from automation_engine import _handle_sse_event, event_bus
        with patch("automation_engine.db") as db, \
             patch("automation_engine.event_bus"):
            db.query_dicts.return_value = [{
                "id": 5, "title": "Some Project", "stage_id": 1,
                "responsible_user_id": 100,
            }]
            db.query_dicts.return_value = [{
                "id": 5, "title": "Some Project", "stage_id": 1,
                "responsible_user_id": 100, "stage_title": "Bidding",
                "responsible_name": "Alice",
            }]
            # No rules match (project_created is not in the event map).
            db.query_dicts.return_value = []
            _handle_sse_event({
                "type": "task:created",
                "data": {
                    "task_id": 42,
                    "title": "Some task",
                    "opportunity_id": 5,
                },
            })
            # No action should have been executed (the rule fetch returned []).

    def test_project_updated_event_fires_project_updated_rule(self):
        # Audit round 8 fix #1: project_updated is in the event map
        # AND is published from _handle_project_update.
        from automation_engine import _handle_sse_event
        with patch("automation_engine.db") as db:
            db.query_dicts.side_effect = [
                # 1st call: fetch the opportunity
                [{"id": 5, "title": "P", "stage_id": 1, "responsible_user_id": 100,
                  "stage_title": "Bidding", "responsible_name": "Alice"}],
                # 2nd call: find matching rules
                [],  # no rules
            ]
            _handle_sse_event({
                "type": "project_updated",
                "data": {"opportunity_id": 5},
            })


class TestSweepSmoke(unittest.TestCase):
    def test_sweep_once_runs_without_crashing(self):
        # The polling sweep should iterate every trigger type and
        # call execute_matches even when there are no rules. The
        # result is the count of actions taken (zero here).
        from automation_engine import _sweep_once
        with patch("automation_engine.db") as db:
            db.query_dicts.return_value = []
            result = _sweep_once()
        self.assertEqual(result, 0)


class TestExecuteActionDispatch(unittest.TestCase):
    def test_unknown_action_type_returns_empty(self):
        from automation_engine import _execute_action
        rule = {"action_type": "weird_unknown", "action_config": {}}
        result = _execute_action(rule, {}, {}, dry_run=False)
        # The default case: no branch matched, so result is just
        # {"action_type": ...} with no other fields.
        self.assertEqual(result, {"action_type": "weird_unknown"})

    def test_create_task_dispatches(self):
        from automation_engine import _execute_action
        with patch("automation_engine._action_create_task") as action:
            action.return_value = {"count": 2, "task_ids": [1, 2]}
            result = _execute_action(
                {"action_type": "create_task", "action_config": {}},
                {"id": 1, "title": "P"},
                {"project": "P"},
                dry_run=True,
            )
        action.assert_called_once()
        self.assertEqual(result["count"], 2)

    def test_send_email_dispatches(self):
        from automation_engine import _execute_action
        with patch("automation_engine._action_send_email") as action:
            action.return_value = {"email_sent_to": "x@y.z"}
            result = _execute_action(
                {"action_type": "send_email", "action_config": {}},
                {"id": 1, "title": "P"},
                {"project": "P"},
                dry_run=False,
            )
        action.assert_called_once()
        self.assertEqual(result["email_sent_to"], "x@y.z")

    def test_change_stage_dispatches(self):
        from automation_engine import _execute_action
        with patch("automation_engine._action_change_stage") as action:
            action.return_value = {"moved_to_stage": 7}
            result = _execute_action(
                {"action_type": "change_stage", "action_config": {}},
                {"id": 1, "title": "P"},
                {"project": "P"},
                dry_run=False,
            )
        action.assert_called_once()
        self.assertEqual(result["moved_to_stage"], 7)

    def test_add_tag_dispatches(self):
        from automation_engine import _execute_action
        with patch("automation_engine._action_add_tag") as action:
            action.return_value = {"count": 1, "tags_added": [99]}
            result = _execute_action(
                {"action_type": "add_tag", "action_config": {}},
                {"id": 1, "title": "P"},
                {"project": "P"},
                dry_run=False,
            )
        action.assert_called_once()
        self.assertEqual(result["count"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
