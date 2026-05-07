"""
corkboard.py — helpers for the project's fact-store substrate.

Provides emit/retract/register primitives plus the boot-time read helpers
that surface vocab + namespaces + today's plan + open contradictions.

The discipline mechanisms from corkboard_schema.sql are enforced at the
schema level (constraints, triggers); these helpers make them ergonomic
and prevent drift in calling code.

Public API:

    bootstrap(db_path)                         # apply schema, idempotent
    register_namespace(conn, prefix, ...)      # extend subject vocab
    register_predicate(conn, name, ...)        # extend predicate vocab
    register_traveler(conn, name, ...)         # add a fact producer
    emit(conn, traveler, subject, predicate,
         object, object_kind='literal',
         captured_in_context=None,
         notes_for_claude=None,
         retracts_id=None, retracts_reason=None)
    retract(conn, fact_id, reason, traveler)   # explicit retraction
    boot_summary(conn)                         # what to load at session start

JSON fields (captured_in_context, notes_for_claude) are stored as text;
this module accepts dicts and serializes for you.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

HERE          = Path(__file__).parent
SCHEMA_FILE   = HERE / "corkboard_schema.sql"
DEFAULT_DB    = HERE / "corkboard.db"


# ------------------------------------------------------------------
# bootstrap
# ------------------------------------------------------------------
def bootstrap(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    """Apply schema to the given DB path. Idempotent (DROP IF EXISTS in schema)."""
    db_path = Path(db_path)
    fresh = not db_path.exists()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if fresh or _schema_needs_apply(conn):
        conn.executescript(SCHEMA_FILE.read_text())
        conn.commit()
    return conn


def _schema_needs_apply(conn: sqlite3.Connection) -> bool:
    """True if the canonical tables are missing — used to decide re-apply."""
    row = conn.execute(
        "SELECT count(*) AS c FROM sqlite_master "
        "WHERE type='table' AND name IN ('namespaces','predicates','travelers','facts')"
    ).fetchone()
    return row["c"] < 4


# ------------------------------------------------------------------
# vocabulary registration
# ------------------------------------------------------------------
def register_namespace(conn: sqlite3.Connection, prefix: str,
                       definition: str, example: str) -> None:
    """Register a subject-prefix namespace. Idempotent."""
    if not prefix.endswith(":"):
        raise ValueError(f"namespace prefix must end with ':' — got {prefix!r}")
    conn.execute(
        "INSERT OR IGNORE INTO namespaces(prefix, definition, example) VALUES (?,?,?)",
        (prefix, definition, example),
    )


def register_predicate(conn: sqlite3.Connection, name: str,
                       domain: str, range_: str,
                       cardinality: str, definition: str,
                       examples: list[str]) -> int:
    """Register a predicate. Returns its id. Idempotent on (name)."""
    if cardinality not in ("one", "many"):
        raise ValueError(f"cardinality must be 'one' or 'many' — got {cardinality!r}")
    examples_json = json.dumps(examples)
    conn.execute(
        "INSERT OR IGNORE INTO predicates(name, domain, range, cardinality, definition, examples) "
        "VALUES (?,?,?,?,?,?)",
        (name, domain, range_, cardinality, definition, examples_json),
    )
    row = conn.execute("SELECT id FROM predicates WHERE name=?", (name,)).fetchone()
    return row["id"]


def register_traveler(conn: sqlite3.Connection, name: str,
                      purpose: str, role: str,
                      source: str | None = None, note: str | None = None) -> None:
    """Register a fact-producing traveler. Idempotent on (name)."""
    if role not in ("substrate", "meta", "external", "human"):
        raise ValueError(f"role must be substrate|meta|external|human — got {role!r}")
    conn.execute(
        "INSERT OR IGNORE INTO travelers(name, purpose, role, source, note) "
        "VALUES (?,?,?,?,?)",
        (name, purpose, role, source, note),
    )


# ------------------------------------------------------------------
# fact emission
# ------------------------------------------------------------------
def _resolve_predicate_id(conn: sqlite3.Connection, predicate: str) -> tuple[int, str]:
    row = conn.execute(
        "SELECT id, cardinality FROM predicates WHERE name=?", (predicate,)
    ).fetchone()
    if row is None:
        raise ValueError(
            f"predicate {predicate!r} not registered — call register_predicate first"
        )
    return row["id"], row["cardinality"]


def emit(conn: sqlite3.Connection,
         traveler: str,
         subject: str,
         predicate: str,
         object: Any,
         object_kind: str = "literal",
         captured_in_context: dict | None = None,
         notes_for_claude: dict | None = None,
         retracts_id: int | None = None,
         retracts_reason: str | None = None) -> int:
    """
    Emit a fact. Returns the new fact id.

    For 'one' cardinality predicates, automatically retracts any prior fact
    with the same (traveler, subject, predicate) but a different object.
    The retraction is recorded with retracts_id linkage (not just timestamp),
    so the chain of supersession is queryable.

    object can be a string, int, float, dict, or list. Dicts/lists are
    JSON-encoded and object_kind defaults to 'json' in that case.
    """
    pid, cardinality = _resolve_predicate_id(conn, predicate)

    if isinstance(object, (dict, list)):
        object = json.dumps(object, ensure_ascii=False)
        if object_kind == "literal":
            object_kind = "json"
    elif not isinstance(object, str):
        object = str(object)

    ctx_json   = json.dumps(captured_in_context, ensure_ascii=False) if captured_in_context else None
    notes_json = json.dumps(notes_for_claude,    ensure_ascii=False) if notes_for_claude    else None

    # No-op short-circuit: if an identical live fact already exists,
    # return its id without inserting (idempotent re-emit).
    existing = conn.execute(
        "SELECT id FROM facts "
        "WHERE traveler=? AND subject=? AND predicate_id=? AND object=? "
        "  AND retracted_at IS NULL",
        (traveler, subject, pid, object),
    ).fetchone()
    if existing is not None:
        return existing["id"]

    # For 'one' cardinality, find any existing live fact with different object
    # and treat THIS emit as a retraction of it (set retracts_id automatically).
    if cardinality == "one" and retracts_id is None:
        prior = conn.execute(
            "SELECT id, object FROM facts "
            "WHERE traveler=? AND subject=? AND predicate_id=? AND retracted_at IS NULL "
            "  AND object <> ?",
            (traveler, subject, pid, object),
        ).fetchone()
        if prior is not None:
            retracts_id = prior["id"]
            if retracts_reason is None:
                retracts_reason = (
                    f"superseded by new value (auto-retract on 'one' cardinality)"
                )

    cur = conn.execute(
        "INSERT INTO facts(traveler, subject, predicate_id, object, object_kind, "
        "                  captured_in_context, notes_for_claude, "
        "                  retracts_id, retracts_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (traveler, subject, pid, object, object_kind,
         ctx_json, notes_json,
         retracts_id, retracts_reason),
    )
    return cur.lastrowid


def retract(conn: sqlite3.Connection, fact_id: int,
            reason: str, by_traveler: str) -> None:
    """
    Explicit retraction — marks a fact retracted without emitting a replacement.
    Use when a claim is just wrong (not superseded by a different value).
    For supersession, prefer emit(..., retracts_id=fact_id) which both
    asserts the new value AND retracts the old in one step.
    """
    conn.execute(
        "UPDATE facts SET retracted_at = strftime('%Y-%m-%dT%H:%M:%f','now') "
        "WHERE id = ? AND retracted_at IS NULL",
        (fact_id,),
    )
    # Record the retraction-without-replacement as a fact under the meta
    # vocabulary so the audit trail isn't just "timestamp appeared."
    # (Caller must have RETRACTION_REASON predicate registered.)
    try:
        rid, _ = _resolve_predicate_id(conn, "RETRACTION_REASON")
        conn.execute(
            "INSERT INTO facts(traveler, subject, predicate_id, object, object_kind) "
            "VALUES (?,?,?,?,?)",
            (by_traveler, f"fact:{fact_id}", rid, reason, "literal"),
        )
    except ValueError:
        pass  # predicate not registered yet; bare timestamp retraction is acceptable fallback


# ------------------------------------------------------------------
# boot-time read helpers
# ------------------------------------------------------------------
def boot_summary(conn: sqlite3.Connection) -> dict:
    """
    What a session boot should load before interpreting any data.
    Returns a dict with vocab, namespaces, today's plan, open contradictions,
    recent retractions, and pinned items.

    Mechanism #6: vocab + namespaces FIRST, so future-Claude knows what
    the data MEANS before reading any of it.
    """
    rows = lambda sql, *args: [dict(r) for r in conn.execute(sql, args).fetchall()]

    return {
        "namespaces":     rows("SELECT prefix, definition, example FROM namespaces ORDER BY prefix"),
        "predicates":     rows("SELECT name, domain, range, cardinality, definition FROM predicates ORDER BY name"),
        "travelers":      rows("SELECT name, role, purpose FROM travelers WHERE retired_at IS NULL ORDER BY role, name"),
        "plan":           rows("SELECT * FROM v_plan_today"),
        "contradictions": rows("SELECT * FROM v_contradictions"),
        "pinned":         rows("SELECT * FROM v_pinned"),
        "trajectory":     rows("SELECT * FROM v_trajectory"),
        "fact_counts": dict(conn.execute(
            "SELECT 'live' AS kind, COUNT(*) AS n FROM v_facts_live "
            "UNION ALL SELECT 'retracted', COUNT(*) FROM facts WHERE retracted_at IS NOT NULL "
            "UNION ALL SELECT 'total',     COUNT(*) FROM facts"
        ).fetchall() and {r["kind"]: r["n"] for r in conn.execute(
            "SELECT 'live' AS kind, COUNT(*) AS n FROM v_facts_live "
            "UNION ALL SELECT 'retracted', COUNT(*) FROM facts WHERE retracted_at IS NOT NULL "
            "UNION ALL SELECT 'total',     COUNT(*) FROM facts"
        )}),
    }


def query(conn: sqlite3.Connection, sql: str, *args) -> list[dict]:
    """Convenience: run a query, return list of dicts."""
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


# ------------------------------------------------------------------
# CLI: bootstrap and print boot summary
# ------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB
    conn = bootstrap(db)
    summary = boot_summary(conn)
    print(json.dumps({
        "db_path":         str(db),
        "namespace_count": len(summary["namespaces"]),
        "predicate_count": len(summary["predicates"]),
        "traveler_count":  len(summary["travelers"]),
        "fact_counts":     summary["fact_counts"],
        "plan_rows":       len(summary["plan"]),
        "contradictions":  len(summary["contradictions"]),
    }, indent=2))
