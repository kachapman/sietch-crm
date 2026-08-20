"""Automation engine — hybrid SSE + polling for evaluating rules and executing actions.

Architecture:
    - Event-driven triggers (project_created, stage_changed, project_updated, tag_added)
      are evaluated immediately when SSE events arrive via event_bus.
    - Time-based triggers (stage_duration, tag_duration) are evaluated by periodic polling
      every 60 seconds.

Trigger types:
    stage_duration  — project has been in a specific stage for N days (polled)
    tag_duration    — tag has been on a project for N days (polled)
    project_created — a new project was created (SSE event-driven)
    stage_changed   — a project moved to a different stage (SSE event-driven)
    project_updated — a project was updated (SSE event-driven)
    tag_added       — a tag was added to a project (SSE event-driven)

Action types:
    create_task    — insert a task assigned to specified users
    notify_users   — insert notifications for specified users
    send_email     — send email to customer/contact/project owner
    change_stage   — move project to a target stage
    add_tag        — add tags to the project
"""

from __future__ import annotations

import json
import logging
import queue
import smtplib
import threading
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import db
from event_bus import event_bus

logger = logging.getLogger("automation-engine")

DEFAULT_POLL_INTERVAL = 3600  # 1 hour default for time-based triggers


def _get_poll_interval() -> int:
    """Get the polling interval from system_settings, default 3600s (1 hour)."""
    try:
        row = db.query_one("SELECT value FROM system_settings WHERE key = 'automation_poll_interval'")
        if row and row.get("value"):
            val = row["value"]
            if isinstance(val, (int, float)):
                return max(60, int(val))
            return max(60, int(str(val)))
    except Exception:
        pass
    return DEFAULT_POLL_INTERVAL
TemplateVars = dict[str, str]


def _render_template(template: str, ctx: TemplateVars) -> str:
    """Replace {var} placeholders with context values."""
    result = template
    for key, val in ctx.items():
        result = result.replace(f"{{{key}}}", str(val) if val is not None else "")
    return result


def _build_template_context(opp: dict, stage_title: str = "", days_in_stage: int = 0, days_with_tag: int = 0) -> TemplateVars:
    """Build template variable context from an opportunity row."""
    contact = opp.get("contact_name") or ""
    responsible = opp.get("responsible_name") or ""
    return {
        "project": opp.get("title") or "",
        "project_id": str(opp.get("id") or ""),
        "project_number": str(opp.get("project_number") or opp.get("id") or ""),
        "stage": stage_title or opp.get("stage_title") or "",
        "days_in_stage": str(days_in_stage),
        "days_with_tag": str(days_with_tag),
        "tag": opp.get("tag_title") or "",
        "value": str(opp.get("bid_value") or ""),
        "contact": contact,
        "responsible": responsible,
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "description": opp.get("description") or "",
    }


# ---------------------------------------------------------------------------
# Cooldown check
# ---------------------------------------------------------------------------

def _should_skip(rule_id: int, opp_id: int, cooldown_hours: int) -> bool:
    """Return True if this rule already fired for this opportunity within the cooldown window."""
    if cooldown_hours <= 0:
        return False
    try:
        row = db.query_one(
            """SELECT id FROM automation_logs
               WHERE rule_id = %s AND opportunity_id = %s AND dry_run = FALSE
                 AND created_at > NOW() - make_interval(hours => %s)
               LIMIT 1""",
            (rule_id, opp_id, cooldown_hours),
        )
        return row is not None
    except Exception:
        return False


def _already_fired_once(rule_id: int, opp_id: int) -> bool:
    """Return True if this rule has ever fired for this opportunity (once-only mode)."""
    try:
        row = db.query_one(
            "SELECT id FROM automation_logs WHERE rule_id = %s AND opportunity_id = %s AND dry_run = FALSE LIMIT 1",
            (rule_id, opp_id),
        )
        return row is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Action execution
# ---------------------------------------------------------------------------

def _execute_action(rule: dict, opp: dict, ctx: TemplateVars, dry_run: bool) -> dict:
    """Execute the rule's action. Returns a result dict for the log."""
    action_type = rule["action_type"]
    config = rule.get("action_config") or {}
    opp_id = opp.get("id")
    result: dict = {"action_type": action_type}

    if action_type == "create_task":
        result.update(_action_create_task(config, opp, ctx, dry_run))

    elif action_type == "send_email":
        result.update(_action_send_email(config, opp, ctx, dry_run))

    elif action_type == "change_stage":
        result.update(_action_change_stage(config, opp, ctx, dry_run))

    elif action_type == "add_tag":
        result.update(_action_add_tag(config, opp, ctx, dry_run))

    return result


def _action_create_task(config: dict, opp: dict, ctx: TemplateVars, dry_run: bool) -> dict:
    title = _render_template(config.get("title", "Automation: {project}"), ctx)
    description = _render_template(config.get("description", ""), ctx)
    assignee_ids = config.get("assignee_user_ids") or []
    due_offset_days = int(config.get("due_offset_days") or 0)
    due_date = (datetime.utcnow() + timedelta(days=due_offset_days)).strftime("%Y-%m-%d") if due_offset_days else None
    opp_id = opp.get("id")

    if dry_run:
        return {"dry_run": True, "would_create_task": title, "assignees": assignee_ids, "due_date": due_date}

    task_ids = []
    # Default category: "Follow-Up"
    default_category_id = None
    try:
        cat_row = db.query_one("SELECT id FROM history_categories WHERE title = 'Follow-Up' LIMIT 1")
        if cat_row:
            default_category_id = cat_row["id"]
    except Exception:
        pass

    for uid in assignee_ids:
        tid = db.insert_returning(
            """INSERT INTO tasks (title, description, responsible_user_id, owner_user_id,
                                  opportunity_id, due_date, created_by, is_closed, category_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE, %s) RETURNING id""",
            (title, description, uid, uid, opp_id, due_date, uid, default_category_id),
        )
        task_ids.append(tid)
        # Explicitly create task_assigned notification with "CRM Automation" actor
        # (DB trigger won't fire because created_by == responsible_user_id)
        notif_message = f"CRM Automation assigned you a task: {title}"
        notif_payload = {
            "task_id": tid, "task_title": title, "due_date": due_date,
            "actor_override": "CRM Automation",
        }
        try:
            nid = db.insert_returning(
                """INSERT INTO notifications (user_id, type, opportunity_id, actor_user_id, message, payload)
                   VALUES (%s, 'task_assigned', %s, NULL, %s, %s) RETURNING id""",
                (uid, opp_id, notif_message, json.dumps(notif_payload)),
            )
            event_bus.publish("notification:new", {
                "type": "task_assigned", "message": notif_message,
                "payload": notif_payload, "opportunity_id": opp_id,
            }, user_ids=[str(uid)])
        except Exception:
            logger.exception("Failed to create task_assigned notification for user %s", uid)
        # Publish task:created so dashboard auto-refreshes
        event_bus.publish("task:created", {
            "task_id": tid, "title": title, "opportunity_id": opp_id,
        }, user_ids=[str(uid)])

    result: dict = {"task_ids": task_ids, "count": len(task_ids)}
    if due_date:
        result["due_date"] = due_date
    return result



def _action_send_email(config: dict, opp: dict, ctx: TemplateVars, dry_run: bool) -> dict:
    from_type = config.get("from_type", "crm_mail")
    subject_template = config.get("subject", "Update on {project}")
    body_template = config.get("body", "Hello,\n\nThis is an automated update regarding {project}.\n\nStage: {stage}\nStatus: {days_in_stage} days in current stage.\n")
    subject = _render_template(subject_template, ctx)
    html_body = _render_template(body_template, ctx).replace("\n", "<br>")

    to_type = config.get("to_type", "project_owner")
    to_email = _resolve_email_recipient(to_type, opp, config)

    if not to_email:
        return {"dry_run": dry_run, "error": "No recipient email found"}

    if dry_run:
        return {"dry_run": True, "would_send_to": to_email, "subject": subject, "from_type": from_type}

    # Get SMTP config based on from_type
    smtp_config = _get_smtp_config(from_type, config)
    if not smtp_config:
        return {"error": f"SMTP not configured for from_type={from_type}"}

    try:
        msg = MIMEMultipart("alternative")
        display_from = f"{smtp_config.get('from_name', 'Sietch CRM')} <{smtp_config['from_addr']}>"
        msg["From"] = display_from
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body.replace("<br>", "\n"), "plain"))
        msg.attach(MIMEText(html_body, "html"))

        import os
        host = smtp_config["host"]
        port = int(smtp_config.get("port", 587))
        user = smtp_config.get("user", "")
        password = smtp_config.get("password", "")

        with smtplib.SMTP(host, port, timeout=30) as server:
            if port == 587:
                server.starttls()
            if user and password:
                server.login(user, password)
            server.sendmail(smtp_config["from_addr"], [to_email], msg.as_string())

        return {"email_sent_to": to_email, "subject": subject}
    except Exception as e:
        logger.exception("Automation email send failed")
        return {"error": str(e)}


def _action_change_stage(config: dict, opp: dict, ctx: TemplateVars, dry_run: bool) -> dict:
    target_stage_id = config.get("target_stage_id")
    if not target_stage_id:
        return {"error": "No target stage configured"}
    opp_id = opp.get("id")
    target_stage_title = config.get("target_stage_title") or ""

    if dry_run:
        return {"dry_run": True, "would_move_to_stage": target_stage_id, "stage_title": target_stage_title}

    db.execute(
        "UPDATE opportunities SET stage_id = %s, stage_entered_at = NOW(), updated_at = NOW() WHERE id = %s",
        (target_stage_id, opp_id),
    )
    return {"moved_to_stage": target_stage_id, "stage_title": target_stage_title}


def _action_add_tag(config: dict, opp: dict, ctx: TemplateVars, dry_run: bool) -> dict:
    tag_ids = config.get("tag_ids") or []
    opp_id = opp.get("id")

    if dry_run:
        return {"dry_run": True, "would_add_tags": tag_ids}

    added = []
    for tag_id in tag_ids:
        try:
            db.execute(
                """INSERT INTO opportunity_tags (opportunity_id, tag_id, assigned_at)
                   VALUES (%s, %s, NOW()) ON CONFLICT DO NOTHING""",
                (opp_id, tag_id),
            )
            added.append(tag_id)
        except Exception:
            pass
    return {"tags_added": added, "count": len(added)}


def _resolve_email_recipient(to_type: str, opp: dict, config: dict) -> str | None:
    """Resolve the email recipient based on to_type."""
    if to_type == "project_owner":
        uid = opp.get("responsible_user_id")
        if uid:
            row = db.query_one("SELECT email FROM users WHERE id = %s", (uid,))
            return row["email"] if row else None

    elif to_type == "contact":
        contact_id = opp.get("contact_id")
        if contact_id:
            row = db.query_one("SELECT email FROM contacts WHERE id = %s", (contact_id,))
            return row["email"] if row else None

    elif to_type == "custom":
        return config.get("custom_email")

    return None


def _get_smtp_config(from_type: str, config: dict) -> dict | None:
    """Get SMTP configuration based on from_type."""
    if from_type == "crm_mail":
        try:
            row = db.query_one(
                """SELECT email, smtp_host, smtp_port, smtp_user, smtp_password_encrypted
                   FROM mail_accounts WHERE is_crm_mail = TRUE AND smtp_host IS NOT NULL
                   ORDER BY id LIMIT 1"""
            )
            if row:
                return {
                    "host": row["smtp_host"],
                    "port": row.get("smtp_port", 587),
                    "user": row.get("smtp_user", ""),
                    "password": row.get("smtp_password_encrypted", ""),
                    "from_addr": row["email"],
                    "from_name": "Sietch CRM",
                }
        except Exception:
            pass

    elif from_type == "alternate_smtp":
        try:
            row = db.query_one(
                "SELECT value FROM bot_config WHERE key = 'alternate_smtp'"
            )
            if row:
                cfg = row["value"] if isinstance(row["value"], dict) else json.loads(row["value"])
                if cfg.get("enabled"):
                    return {
                        "host": cfg.get("host", ""),
                        "port": int(cfg.get("port", 587)),
                        "user": cfg.get("user", ""),
                        "password": cfg.get("password", ""),
                        "from_addr": cfg.get("from_addr") or cfg.get("user", ""),
                        "from_name": cfg.get("from_name") or "Sietch CRM",
                    }
        except Exception:
            pass

    elif from_type == "custom":
        return {
            "host": config.get("smtp_host", ""),
            "port": int(config.get("smtp_port", 587)),
            "user": config.get("smtp_user", ""),
            "password": config.get("smtp_password", ""),
            "from_addr": config.get("smtp_from_addr", ""),
            "from_name": config.get("smtp_from_name") or "Sietch CRM",
        }

    return None


# ---------------------------------------------------------------------------
# Trigger evaluation
# ---------------------------------------------------------------------------

_EPOCH = datetime(2000, 1, 1)


def _get_last_run(rule: dict):
    """Get last_run_at for a rule. On first run (NULL), use epoch so all records match."""
    last_run = rule.get("last_run_at")
    if last_run is None:
        return _EPOCH
    return last_run


def _evaluate_stage_duration_rules() -> list[dict]:
    """Find projects stuck in a stage beyond the configured threshold."""
    results = []
    try:
        rules = db.query_dicts(
            "SELECT * FROM automation_rules WHERE enabled = TRUE AND trigger_type = 'stage_duration'"
        )
    except Exception:
        return results

    for rule in rules:
        cfg = rule.get("trigger_config") or {}
        stage_id = cfg.get("stage_id")
        days = int(cfg.get("days") or 2)
        cooldown_mode = cfg.get("cooldown_mode", "recurring")
        cooldown_hours = int(cfg.get("cooldown_hours") or days * 24)

        if not stage_id:
            continue

        try:
            opps = db.query_dicts(
                """SELECT o.*, s.title as stage_title, u.email as owner_email,
                          u.display_name as responsible_name
                   FROM opportunities o
                   LEFT JOIN stages s ON s.id = o.stage_id
                   LEFT JOIN users u ON u.id = o.responsible_user_id
                   WHERE o.stage_id = %s
                     AND o.stage_entered_at <= NOW() - make_interval(days => %s)
                     AND (o.is_closed = FALSE OR o.is_closed IS NULL)
                   LIMIT 50""",
                (stage_id, days),
            )
        except Exception:
            logger.exception("Failed to query stuck projects for rule %s", rule["id"])
            continue

        for opp in opps:
            opp_id = opp["id"]
            if cooldown_mode == "once" and _already_fired_once(rule["id"], opp_id):
                continue
            if cooldown_mode == "recurring" and _should_skip(rule["id"], opp_id, cooldown_hours):
                continue

            entered = opp.get("stage_entered_at")
            if isinstance(entered, str):
                try:
                    entered = datetime.fromisoformat(entered)
                except Exception:
                    entered = None
            days_in_stage = (datetime.utcnow() - entered).days if entered else days

            results.append({"rule": rule, "opp": opp, "days_in_stage": days_in_stage})

    return results


def _evaluate_project_created_rules() -> list[dict]:
    """Find projects created since last run of each rule."""
    results = []
    try:
        rules = db.query_dicts(
            "SELECT * FROM automation_rules WHERE enabled = TRUE AND trigger_type = 'project_created'"
        )
    except Exception:
        return results

    for rule in rules:
        last_run = _get_last_run(rule)
        try:
            opps = db.query_dicts(
                """SELECT o.*, s.title as stage_title, u.display_name as responsible_name
                   FROM opportunities o
                   LEFT JOIN stages s ON s.id = o.stage_id
                   LEFT JOIN users u ON u.id = o.responsible_user_id
                   WHERE o.created_at > %s
                   LIMIT 50""",
                (last_run,),
            )
        except Exception:
            logger.exception("Failed to query new projects for rule %s", rule["id"])
            continue

        for opp in opps:
            results.append({"rule": rule, "opp": opp, "days_in_stage": 0})

    return results


def _evaluate_stage_changed_rules() -> list[dict]:
    """Find projects that changed stage since last run."""
    results = []
    try:
        rules = db.query_dicts(
            "SELECT * FROM automation_rules WHERE enabled = TRUE AND trigger_type = 'stage_changed'"
        )
    except Exception:
        return results

    for rule in rules:
        last_run = _get_last_run(rule)
        cfg = rule.get("trigger_config") or {}
        to_stage_id = cfg.get("to_stage_id")

        try:
            if to_stage_id:
                opps = db.query_dicts(
                    """SELECT o.*, s.title as stage_title, u.display_name as responsible_name
                       FROM opportunities o
                       LEFT JOIN stages s ON s.id = o.stage_id
                       LEFT JOIN users u ON u.id = o.responsible_user_id
                       WHERE o.stage_id = %s AND o.stage_entered_at > %s
                       LIMIT 50""",
                    (to_stage_id, last_run),
                )
            else:
                opps = db.query_dicts(
                    """SELECT o.*, s.title as stage_title, u.display_name as responsible_name
                       FROM opportunities o
                       LEFT JOIN stages s ON s.id = o.stage_id
                       LEFT JOIN users u ON u.id = o.responsible_user_id
                       WHERE o.stage_entered_at > %s
                       LIMIT 50""",
                    (last_run,),
                )
        except Exception:
            logger.exception("Failed to query stage-changed projects for rule %s", rule["id"])
            continue

        for opp in opps:
            results.append({"rule": rule, "opp": opp, "days_in_stage": 0})

    return results


def _evaluate_project_updated_rules() -> list[dict]:
    """Find projects updated since last run."""
    results = []
    try:
        rules = db.query_dicts(
            "SELECT * FROM automation_rules WHERE enabled = TRUE AND trigger_type = 'project_updated'"
        )
    except Exception:
        return results

    for rule in rules:
        last_run = _get_last_run(rule)
        try:
            opps = db.query_dicts(
                """SELECT o.*, s.title as stage_title, u.display_name as responsible_name
                   FROM opportunities o
                   LEFT JOIN stages s ON s.id = o.stage_id
                   LEFT JOIN users u ON u.id = o.responsible_user_id
                   WHERE o.updated_at > %s
                   LIMIT 50""",
                (last_run,),
            )
        except Exception:
            logger.exception("Failed to query updated projects for rule %s", rule["id"])
            continue

        for opp in opps:
            results.append({"rule": rule, "opp": opp, "days_in_stage": 0})

    return results


def _evaluate_tag_duration_rules() -> list[dict]:
    """Find projects that have had a specific tag for N days."""
    results = []
    try:
        rules = db.query_dicts(
            "SELECT * FROM automation_rules WHERE enabled = TRUE AND trigger_type = 'tag_duration'"
        )
    except Exception:
        return results

    for rule in rules:
        cfg = rule.get("trigger_config") or {}
        tag_id = cfg.get("tag_id")
        days = int(cfg.get("days") or 2)
        cooldown_mode = cfg.get("cooldown_mode", "recurring")
        cooldown_hours = int(cfg.get("cooldown_hours") or days * 24)

        if not tag_id:
            continue

        try:
            opps = db.query_dicts(
                """SELECT o.*, s.title as stage_title, u.display_name as responsible_name,
                          ot.assigned_at, t.title as tag_title
                   FROM opportunity_tags ot
                   JOIN opportunities o ON o.id = ot.opportunity_id
                   LEFT JOIN stages s ON s.id = o.stage_id
                   LEFT JOIN users u ON u.id = o.responsible_user_id
                   JOIN tag_definitions t ON t.id = ot.tag_id
                   WHERE ot.tag_id = %s
                     AND ot.assigned_at <= NOW() - make_interval(days => %s)
                     AND (o.is_closed = FALSE OR o.is_closed IS NULL)
                   LIMIT 50""",
                (tag_id, days),
            )
        except Exception:
            logger.exception("Failed to query tag-duration projects for rule %s", rule["id"])
            continue

        for opp in opps:
            opp_id = opp["id"]
            if cooldown_mode == "once" and _already_fired_once(rule["id"], opp_id):
                continue
            if cooldown_mode == "recurring" and _should_skip(rule["id"], opp_id, cooldown_hours):
                continue

            assigned = opp.get("assigned_at")
            if isinstance(assigned, str):
                try:
                    assigned = datetime.fromisoformat(assigned)
                except Exception:
                    assigned = None
            days_with_tag = (datetime.utcnow() - assigned).days if assigned else days

            results.append({"rule": rule, "opp": opp, "days_in_stage": 0, "days_with_tag": days_with_tag})

    return results


def _evaluate_tag_added_rules() -> list[dict]:
    """Find projects that had a tag added since last run of each rule."""
    results = []
    try:
        rules = db.query_dicts(
            "SELECT * FROM automation_rules WHERE enabled = TRUE AND trigger_type = 'tag_added'"
        )
    except Exception:
        return results

    for rule in rules:
        last_run = _get_last_run(rule)
        cfg = rule.get("trigger_config") or {}
        tag_id = cfg.get("tag_id")

        try:
            if tag_id:
                opps = db.query_dicts(
                    """SELECT o.*, s.title as stage_title, u.display_name as responsible_name,
                              ot.assigned_at, t.title as tag_title
                       FROM opportunity_tags ot
                       JOIN opportunities o ON o.id = ot.opportunity_id
                       LEFT JOIN stages s ON s.id = o.stage_id
                       LEFT JOIN users u ON u.id = o.responsible_user_id
                       JOIN tag_definitions t ON t.id = ot.tag_id
                       WHERE ot.tag_id = %s AND ot.assigned_at > %s
                       LIMIT 50""",
                    (tag_id, last_run),
                )
            else:
                opps = db.query_dicts(
                    """SELECT o.*, s.title as stage_title, u.display_name as responsible_name,
                              ot.assigned_at, t.title as tag_title
                       FROM opportunity_tags ot
                       JOIN opportunities o ON o.id = ot.opportunity_id
                       LEFT JOIN stages s ON s.id = o.stage_id
                       LEFT JOIN users u ON u.id = o.responsible_user_id
                       JOIN tag_definitions t ON t.id = ot.tag_id
                       WHERE ot.assigned_at > %s
                       LIMIT 50""",
                    (last_run,),
                )
        except Exception:
            logger.exception("Failed to query tag-added projects for rule %s", rule["id"])
            continue

        for opp in opps:
            opp_id = opp["id"]
            cooldown_mode = cfg.get("cooldown_mode", "recurring")
            if cooldown_mode == "once" and _already_fired_once(rule["id"], opp_id):
                continue
            if cooldown_mode == "recurring":
                cooldown_hours = int(cfg.get("cooldown_hours") or 24)
                if _should_skip(rule["id"], opp_id, cooldown_hours):
                    continue
            results.append({"rule": rule, "opp": opp, "days_in_stage": 0, "days_with_tag": 0})

    return results


# ---------------------------------------------------------------------------
# SSE event-driven evaluation
# ---------------------------------------------------------------------------

# Map SSE event types to automation trigger types
_EVENT_TRIGGER_MAP = {
    "project_created": "project_created",
    "stage_changed": "stage_changed",
    "project_updated": "project_updated",
    "tag_added": "tag_added",
    "task:created": "project_created",
}


def _handle_sse_event(event_data: dict) -> None:
    """Handle an SSE event and evaluate matching automation rules immediately."""
    try:
        event_type = event_data.get("type", "")
        trigger_type = _EVENT_TRIGGER_MAP.get(event_type)
        if not trigger_type:
            return

        # Get the opportunity_id from the event data
        data = event_data.get("data", {})
        opp_id = data.get("opportunity_id") or data.get("id")
        if not opp_id:
            return

        # Fetch the opportunity
        opp = db.query_dicts(
            """SELECT o.*, s.title as stage_title, u.display_name as responsible_name
               FROM opportunities o
               LEFT JOIN stages s ON s.id = o.stage_id
               LEFT JOIN users u ON u.id = o.responsible_user_id
               WHERE o.id = %s""",
            (opp_id,),
        )
        if not opp:
            return
        opp = opp[0]

        # Find matching rules for this trigger type
        rules = db.query_dicts(
            "SELECT * FROM automation_rules WHERE enabled = TRUE AND trigger_type = %s",
            (trigger_type,),
        )

        for rule in rules:
            rule_id = rule["id"]
            cfg = rule.get("trigger_config") or {}

            # Cooldown check
            cooldown_mode = cfg.get("cooldown_mode", "recurring")
            if cooldown_mode == "once" and _already_fired_once(rule_id, opp_id):
                continue
            if cooldown_mode == "recurring":
                cooldown_hours = int(cfg.get("cooldown_hours") or 24)
                if _should_skip(rule_id, opp_id, cooldown_hours):
                    continue

            # Additional filter checks for specific trigger types
            if trigger_type == "tag_added":
                tag_id = cfg.get("tag_id")
                if tag_id and data.get("tag_id") and str(data.get("tag_id")) != str(tag_id):
                    continue  # Wrong tag
            elif trigger_type == "stage_changed":
                to_stage_id = cfg.get("to_stage_id")
                if to_stage_id and str(opp.get("stage_id")) != str(to_stage_id):
                    continue  # Wrong stage

            # Execute the action
            days_in_stage = 0  # SSE events don't carry duration; it's a polling concept
            ctx = _build_template_context(opp, opp.get("stage_title") or "", days_in_stage, 0)
            trigger_context = {
                "trigger_type": trigger_type,
                "event_type": event_type,
                "days_in_stage": days_in_stage,
                "stage_title": opp.get("stage_title") or "",
                "project_title": opp.get("title") or "",
            }

            try:
                result = _execute_action(rule, opp, ctx, rule.get("dry_run", False))
            except Exception as e:
                logger.exception("SSE automation failed for rule %s, opp %s", rule_id, opp_id)
                result = {"error": str(e)}

            error = result.get("error")

            # Log the execution
            try:
                db.execute(
                    """INSERT INTO automation_logs (rule_id, opportunity_id, trigger_context, action_result, dry_run, error)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (rule_id, opp_id, json.dumps(trigger_context), json.dumps(result), rule.get("dry_run", False), error),
                )
                db.execute(
                    "UPDATE automation_rules SET last_run_at = NOW(), run_count = run_count + 1 WHERE id = %s",
                    (rule_id,),
                )
            except Exception:
                logger.exception("Failed to log SSE automation execution")

            if not error:
                logger.info("SSE automation: rule %s fired for opp %s (event: %s)", rule_id, opp_id, event_type)

    except Exception:
        logger.exception("Error handling SSE event for automation")


# ---------------------------------------------------------------------------
# Main sweep (polling-based triggers only)
# ---------------------------------------------------------------------------

def _sweep_once() -> int:
    """Evaluate ALL enabled rules (used by test endpoint). Returns count of actions taken."""
    total = 0
    matches = []
    matches.extend(_evaluate_stage_duration_rules())
    matches.extend(_evaluate_project_created_rules())
    matches.extend(_evaluate_stage_changed_rules())
    matches.extend(_evaluate_project_updated_rules())
    matches.extend(_evaluate_tag_duration_rules())
    matches.extend(_evaluate_tag_added_rules())
    total += _execute_matches(matches)
    return total


def _sweep_once_polling() -> int:
    """Evaluate only time-based triggers (polled periodically). Event-driven triggers use SSE."""
    total = 0
    matches = []
    matches.extend(_evaluate_stage_duration_rules())
    matches.extend(_evaluate_tag_duration_rules())
    total += _execute_matches(matches)
    return total


def _execute_matches(matches: list) -> int:
    """Execute a list of matched rules. Returns count of successful executions."""
    total = 0
    for match in matches:
        rule = match["rule"]
        opp = match["opp"]
        days_in_stage = match.get("days_in_stage", 0)
        rule_id = rule["id"]
        opp_id = opp["id"]
        dry_run = rule.get("dry_run", False)

        ctx = _build_template_context(opp, opp.get("stage_title") or "", days_in_stage, match.get("days_with_tag", 0))
        trigger_context = {
            "trigger_type": rule["trigger_type"],
            "days_in_stage": days_in_stage,
            "stage_title": opp.get("stage_title") or "",
            "project_title": opp.get("title") or "",
        }

        try:
            result = _execute_action(rule, opp, ctx, dry_run)
        except Exception as e:
            logger.exception("Action execution failed for rule %s, opp %s", rule_id, opp_id)
            result = {"error": str(e)}

        error = result.get("error")

        # Log the execution
        try:
            db.execute(
                """INSERT INTO automation_logs (rule_id, opportunity_id, trigger_context, action_result, dry_run, error)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (rule_id, opp_id, json.dumps(trigger_context), json.dumps(result), dry_run, error),
            )
            db.execute(
                "UPDATE automation_rules SET last_run_at = NOW(), run_count = run_count + 1 WHERE id = %s",
                (rule_id,),
            )
        except Exception:
            logger.exception("Failed to log automation execution")

        if not error:
            total += 1

    return total


def _sweep_loop(stop_event: threading.Event) -> None:
    logger.info("Automation engine started (hybrid SSE+polling)")

    # Subscribe to SSE events for real-time trigger evaluation
    from event_bus import event_bus
    event_queue = event_bus.subscribe_system()

    # Start a listener thread for SSE events
    def _event_listener():
        while not stop_event.is_set():
            try:
                msg = event_queue.get(timeout=1.0)
                if msg is None:
                    continue
                event_data = json.loads(msg)
                _handle_sse_event(event_data)
            except queue.Empty:
                continue
            except Exception:
                logger.exception("Error in automation event listener")

    listener_thread = threading.Thread(target=_event_listener, daemon=True, name="automation-event-listener")
    listener_thread.start()

    # Polling loop for time-based triggers only
    while not stop_event.is_set():
        interval = _get_poll_interval()
        try:
            fired = _sweep_once_polling()
            if fired:
                logger.info("Automation engine (polling, interval=%ds): %d rule(s) fired", interval, fired)
        except Exception:
            logger.exception("Automation polling sweep error")
        stop_event.wait(interval)

    # Cleanup
    event_bus.unsubscribe_system(event_queue)
    logger.info("Automation engine stopped")


def start_automation_engine() -> threading.Event:
    """Start the automation engine background thread. Returns stop event."""
    stop_event = threading.Event()
    t = threading.Thread(target=_sweep_loop, args=(stop_event,), daemon=True, name="automation-engine")
    t.start()
    return stop_event


def stop_automation_engine(stop_event: threading.Event) -> None:
    """Signal the engine to stop."""
    stop_event.set()
