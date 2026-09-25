"""SQLite persistence layer for the truth-maintenance rule engine.

All mutations go through a single transactional boundary (`Store.tx`) so a
retraction and the propagation it triggers (support updates, node-state
flips, verdict recording) are committed atomically.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id           TEXT PRIMARY KEY,
    label        TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL CHECK (status IN ('asserted', 'retracted'))
                 DEFAULT 'asserted',
    last_verdict TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rules (
    id         TEXT PRIMARY KEY,
    conclusion TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Reverse index: premise node -> rules that mention it.
CREATE TABLE IF NOT EXISTS rule_premises (
    rule_id  TEXT NOT NULL REFERENCES rules (id) ON DELETE CASCADE,
    premise  TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (rule_id, premise)
);
CREATE INDEX IF NOT EXISTS idx_rule_premises_premise
    ON rule_premises (premise);

-- One support row per rule firing; the complete premise set is stored with
-- it every time the rule fires.
CREATE TABLE IF NOT EXISTS supports (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id    TEXT NOT NULL UNIQUE REFERENCES rules (id) ON DELETE CASCADE,
    conclusion TEXT NOT NULL,
    premises   TEXT NOT NULL,               -- JSON array, complete premise set
    status     TEXT NOT NULL CHECK (status IN ('valid', 'invalid')),
    fire_count INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Reverse index: premise node -> supports built on it.
CREATE TABLE IF NOT EXISTS support_premises (
    support_id INTEGER NOT NULL REFERENCES supports (id) ON DELETE CASCADE,
    premise    TEXT NOT NULL,
    PRIMARY KEY (support_id, premise)
);
CREATE INDEX IF NOT EXISTS idx_support_premises_premise
    ON support_premises (premise);

CREATE TABLE IF NOT EXISTS node_state (
    node       TEXT PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('fact', 'conclusion')),
    valid      INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    type       TEXT NOT NULL,
    payload    TEXT NOT NULL,               -- JSON
    created_at TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Store:
    """Thin transactional wrapper around the SQLite database."""

    def __init__(self, path: str):
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    @contextmanager
    def tx(self):
        """Single persistent transaction: commit all or roll back all."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()

    # ------------------------------------------------------------------ facts

    def add_fact(self, fact_id: str, label: str) -> None:
        now = utcnow()
        self.conn.execute(
            "INSERT INTO facts (id, label, status, created_at, updated_at)"
            " VALUES (?, ?, 'asserted', ?, ?)",
            (fact_id, label, now, now),
        )
        self.set_node_state(fact_id, "fact", True)

    def get_fact(self, fact_id: str):
        return self.conn.execute(
            "SELECT * FROM facts WHERE id = ?", (fact_id,)
        ).fetchone()

    def list_facts(self):
        return self.conn.execute(
            "SELECT * FROM facts ORDER BY id"
        ).fetchall()

    def set_fact_status(self, fact_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE facts SET status = ?, updated_at = ? WHERE id = ?",
            (status, utcnow(), fact_id),
        )
        self.set_node_state(fact_id, "fact", status == "asserted")

    def set_fact_verdict(self, fact_id: str, verdict: dict) -> None:
        self.conn.execute(
            "UPDATE facts SET last_verdict = ?, updated_at = ? WHERE id = ?",
            (json.dumps(verdict, ensure_ascii=False), utcnow(), fact_id),
        )

    # ------------------------------------------------------------------ rules

    def add_rule(self, rule_id: str, premises: list, conclusion: str) -> None:
        self.conn.execute(
            "INSERT INTO rules (id, conclusion, created_at) VALUES (?, ?, ?)",
            (rule_id, conclusion, utcnow()),
        )
        for position, premise in enumerate(premises):
            self.conn.execute(
                "INSERT INTO rule_premises (rule_id, premise, position)"
                " VALUES (?, ?, ?)",
                (rule_id, premise, position),
            )

    def get_rule(self, rule_id: str):
        row = self.conn.execute(
            "SELECT * FROM rules WHERE id = ?", (rule_id,)
        ).fetchone()
        if row is None:
            return None
        return self._rule_with_premises(row)

    def list_rules(self) -> list:
        rows = self.conn.execute("SELECT * FROM rules ORDER BY id").fetchall()
        return [self._rule_with_premises(row) for row in rows]

    def _rule_with_premises(self, row) -> dict:
        premises = [
            r["premise"]
            for r in self.conn.execute(
                "SELECT premise FROM rule_premises"
                " WHERE rule_id = ? ORDER BY position",
                (row["id"],),
            ).fetchall()
        ]
        return {
            "id": row["id"],
            "conclusion": row["conclusion"],
            "premises": premises,
            "created_at": row["created_at"],
        }

    def rules_triggered_by(self, premise: str) -> list:
        """Reverse-index lookup: rules that use `premise` as a premise."""
        rows = self.conn.execute(
            "SELECT r.* FROM rules r"
            " JOIN rule_premises rp ON rp.rule_id = r.id"
            " WHERE rp.premise = ? ORDER BY r.id",
            (premise,),
        ).fetchall()
        return [self._rule_with_premises(row) for row in rows]

    # ---------------------------------------------------------------- supports

    def upsert_support(self, rule_id: str, conclusion: str, premises: list,
                       firing: bool) -> None:
        """Persist a rule firing with its complete premise set."""
        now = utcnow()
        status = "valid" if firing else "invalid"
        payload = json.dumps(list(premises), ensure_ascii=False)
        row = self.conn.execute(
            "SELECT id, status FROM supports WHERE rule_id = ?", (rule_id,)
        ).fetchone()
        if row is None:
            cur = self.conn.execute(
                "INSERT INTO supports"
                " (rule_id, conclusion, premises, status, fire_count,"
                "  created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 1, ?, ?)",
                (rule_id, conclusion, payload, status, now, now),
            )
            support_id = cur.lastrowid
        else:
            support_id = row["id"]
            if row["status"] != status:
                # A (re-)firing rewrites the complete premise set.
                self.conn.execute(
                    "UPDATE supports SET status = ?, premises = ?,"
                    " fire_count = fire_count + 1, updated_at = ?"
                    " WHERE id = ?",
                    (status, payload, now, support_id),
                )
            else:
                self.conn.execute(
                    "UPDATE supports SET premises = ? WHERE id = ?",
                    (payload, support_id),
                )
        self.conn.execute(
            "DELETE FROM support_premises WHERE support_id = ?", (support_id,)
        )
        for premise in premises:
            self.conn.execute(
                "INSERT OR IGNORE INTO support_premises (support_id, premise)"
                " VALUES (?, ?)",
                (support_id, premise),
            )

    def get_support(self, rule_id: str):
        return self.conn.execute(
            "SELECT * FROM supports WHERE rule_id = ?", (rule_id,)
        ).fetchone()

    def supports_for_conclusion(self, conclusion: str) -> list:
        rows = self.conn.execute(
            "SELECT * FROM supports WHERE conclusion = ? ORDER BY rule_id",
            (conclusion,),
        ).fetchall()
        return [self._support_dict(r) for r in rows]

    def _support_dict(self, row) -> dict:
        return {
            "rule_id": row["rule_id"],
            "conclusion": row["conclusion"],
            "premises": json.loads(row["premises"]),
            "status": row["status"],
            "fire_count": row["fire_count"],
        }

    # ------------------------------------------------------------- node state

    def set_node_state(self, node: str, kind: str, valid: bool) -> None:
        now = utcnow()
        self.conn.execute(
            "INSERT INTO node_state (node, kind, valid, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(node) DO UPDATE SET valid = excluded.valid,"
            " kind = excluded.kind, updated_at = excluded.updated_at",
            (node, kind, 1 if valid else 0, now),
        )

    def node_valid(self, node: str) -> bool:
        row = self.conn.execute(
            "SELECT valid FROM node_state WHERE node = ?", (node,)
        ).fetchone()
        return bool(row["valid"]) if row else False

    def node_kind(self, node: str):
        row = self.conn.execute(
            "SELECT kind FROM node_state WHERE node = ?", (node,)
        ).fetchone()
        return row["kind"] if row else None

    def list_conclusions(self) -> list:
        rows = self.conn.execute(
            "SELECT node, valid FROM node_state WHERE kind = 'conclusion'"
            " ORDER BY node"
        ).fetchall()
        return [{"id": r["node"], "valid": bool(r["valid"])} for r in rows]

    def invalid_nodes(self) -> set:
        rows = self.conn.execute(
            "SELECT node FROM node_state WHERE valid = 0"
        ).fetchall()
        return {r["node"] for r in rows}

    # ----------------------------------------------------------------- events

    def record_event(self, event_type: str, payload: dict) -> None:
        self.conn.execute(
            "INSERT INTO events (type, payload, created_at) VALUES (?, ?, ?)",
            (event_type, json.dumps(payload, ensure_ascii=False), utcnow()),
        )

    def last_event(self):
        row = self.conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return {
            "type": row["type"],
            "payload": json.loads(row["payload"]),
            "at": row["created_at"],
        }

    # ------------------------------------------------------------------ admin

    def reset(self) -> None:
        for table in ("support_premises", "supports", "rule_premises",
                      "rules", "facts", "node_state", "events"):
            self.conn.execute(f"DELETE FROM {table}")

    def ping(self) -> bool:
        try:
            self.conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
