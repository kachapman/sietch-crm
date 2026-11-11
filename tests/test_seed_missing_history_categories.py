#!/usr/bin/env python3
"""055: idempotent seed of the 016/017 history categories that never landed.

Ken (2026-11-10 10:10 PM ET): add Action, Follow-Up, Estimate, Inspection,
and Expression of gratitude with the original 016/017 values. Do not reassign
existing tasks or history rows.

Run with: python3 tests/test_seed_missing_history_categories.py
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import network_guard  # noqa: E402

network_guard.install()

import psycopg2  # noqa: E402

MIG = (ROOT / "migrations" / "055_seed_missing_history_categories.sql").read_text()
INIT = (ROOT / "init.sql").read_text()
M016 = (ROOT / "migrations" / "016_add_task_categories.sql").read_text()
M017 = (ROOT / "migrations" / "017_add_custom_field_groups.sql").read_text()

DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_USER = os.environ.get("DB_USER", "sietch")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "local_dev_password")
SCRATCH = f"sietch_seed055_{os.getpid()}"

EXPECTED = [
    ("Action", "pencil", "accent", True, 15, "#6c757d"),
    ("Follow-Up", "clock", "stroke", True, 9, None),
    ("Estimate", "document", "stroke", True, 10, None),
    ("Inspection", "building", "stroke", True, 11, None),
    ("Expression of gratitude", "star", "stroke", True, 12, None),
]


def _admin():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname="postgres",
        user=DB_USER, password=DB_PASSWORD, connect_timeout=5,
    )


def _connect(dbname: str):
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=dbname,
        user=DB_USER, password=DB_PASSWORD, connect_timeout=5,
    )


class TestMigrationSource(unittest.TestCase):
    def test_uses_where_not_exists(self):
        self.assertGreaterEqual(MIG.count("WHERE NOT EXISTS"), 5)
        self.assertIn("FROM history_categories WHERE title = 'Action'", MIG)
        for title, *_rest in EXPECTED:
            with self.subTest(title=title):
                self.assertIn("WHERE title = '%s'" % title, MIG)

    def test_values_match_016_and_017(self):
        self.assertIn("'Action', '#6c757d', TRUE, 15, 'pencil', 'accent'", MIG)
        self.assertIn("'Action', '#6c757d', TRUE, 15, 'pencil', 'accent'", M017)
        for fragment in (
            "'Follow-Up', 'clock', 'stroke', TRUE, 9, NULL",
            "'Estimate', 'document', 'stroke', TRUE, 10, NULL",
            "'Inspection', 'building', 'stroke', TRUE, 11, NULL",
            "'Expression of gratitude', 'star', 'stroke', TRUE, 12, NULL",
        ):
            self.assertIn(fragment, MIG)
            self.assertIn(fragment, M016)

    def test_does_not_reassign_existing_rows(self):
        sql_lines = []
        for line in MIG.splitlines():
            stripped = line.strip()
            if stripped.startswith("--") or not stripped:
                continue
            sql_lines.append(stripped)
        body = "\n".join(sql_lines)
        self.assertNotIn("UPDATE tasks", body)
        self.assertNotIn("UPDATE history_events", body)
        self.assertNotIn("SET category_id", body)
        self.assertNotIn("UPDATE ", body)

    def test_init_sql_seeds_and_stamps(self):
        self.assertIn("('055_seed_missing_history_categories.sql')", INIT)
        for title, icon_key, icon_style, _sys, sort_order, color in EXPECTED:
            with self.subTest(title=title):
                self.assertIn("'%s'" % title, INIT)
                self.assertIn("'%s'" % icon_key, INIT)
                self.assertIn("'%s'" % icon_style, INIT)
                self.assertIn(str(sort_order), INIT)
                if color:
                    self.assertIn("'%s'" % color, INIT)


class TestIdempotentSeed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        conn = _admin()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("DROP DATABASE IF EXISTS %s" % SCRATCH)
        cur.execute("CREATE DATABASE %s" % SCRATCH)
        cur.close()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        conn = _admin()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("DROP DATABASE IF EXISTS %s" % SCRATCH)
        cur.close()
        conn.close()

    def setUp(self):
        conn = _connect(SCRATCH)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS tasks")
        cur.execute("DROP TABLE IF EXISTS history_events")
        cur.execute("DROP TABLE IF EXISTS history_categories")
        cur.execute(
            "CREATE TABLE history_categories ("
            " id SERIAL PRIMARY KEY,"
            " title TEXT UNIQUE NOT NULL,"
            " display_color TEXT,"
            " is_system BOOLEAN DEFAULT TRUE,"
            " sort_order INTEGER DEFAULT 0,"
            " icon_key TEXT,"
            " icon_style TEXT)"
        )
        cur.execute(
            "CREATE TABLE tasks ("
            " id SERIAL PRIMARY KEY,"
            " category_id INTEGER REFERENCES history_categories(id))"
        )
        cur.execute(
            "INSERT INTO history_categories "
            "(title, icon_key, icon_style, is_system, sort_order, display_color) "
            "VALUES ('Opportunity', 'alert', 'stroke', TRUE, 13, NULL)"
        )
        cur.execute("INSERT INTO tasks (category_id) VALUES (NULL) RETURNING id")
        self.task_id = cur.fetchone()[0]
        cur.close()
        conn.close()

    def _apply(self):
        conn = _connect(SCRATCH)
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute(MIG)
        conn.commit()
        cur.close()
        conn.close()

    def _rows(self):
        conn = _connect(SCRATCH)
        cur = conn.cursor()
        cur.execute(
            "SELECT title, icon_key, icon_style, is_system, sort_order, display_color, id "
            "FROM history_categories ORDER BY title"
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return rows

    def test_inserts_the_five_rows_and_leaves_existing_alone(self):
        self._apply()
        got = {r[0]: r for r in self._rows()}
        self.assertIn("Opportunity", got)
        self.assertEqual(got["Opportunity"][1:6], ("alert", "stroke", True, 13, None))
        for title, icon_key, icon_style, is_system, sort_order, color in EXPECTED:
            self.assertIn(title, got, title)
            self.assertEqual(
                got[title][1:6],
                (icon_key, icon_style, is_system, sort_order, color),
                title,
            )
        conn = _connect(SCRATCH)
        cur = conn.cursor()
        cur.execute("SELECT category_id FROM tasks WHERE id = %s", (self.task_id,))
        self.assertIsNone(cur.fetchone()[0])
        cur.close()
        conn.close()

    def test_second_apply_is_a_noop(self):
        self._apply()
        first = {r[0]: r[6] for r in self._rows()}
        self._apply()
        second = {r[0]: r[6] for r in self._rows()}
        self.assertEqual(first, second)
        self.assertEqual(len(self._rows()), 6)


if __name__ == "__main__":
    unittest.main()
