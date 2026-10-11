from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def init_db(path: str | Path = "data/archivlotse.db") -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS step_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            step_name TEXT NOT NULL,
            document_id INTEGER NOT NULL,
            fingerprint TEXT NOT NULL,
            output_value TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_name TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            estimated_cost_usd REAL,
            created_at TEXT NOT NULL,
            UNIQUE (step_name, document_id, fingerprint)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL,
            field_name TEXT NOT NULL DEFAULT 'title',
            old_value TEXT,
            new_value TEXT NOT NULL,
            changed_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS candidate_cache (
            endpoint TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        )
        """
    )
    _migrate_legacy_audit_log(conn)
    _migrate_step_results_usage_columns(conn)
    conn.commit()
    return conn


def _migrate_legacy_audit_log(conn: sqlite3.Connection) -> None:
    """Upgrade an audit_log table created before it became step-generic
    (old_title/new_title columns, implicitly always about the title
    field) to the current schema, in place, without losing history."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(audit_log)")}
    if "old_title" in columns and "old_value" not in columns:
        conn.execute("ALTER TABLE audit_log RENAME COLUMN old_title TO old_value")
        conn.execute("ALTER TABLE audit_log RENAME COLUMN new_title TO new_value")
        columns -= {"old_title", "new_title"}
        columns |= {"old_value", "new_value"}
    if "field_name" not in columns:
        conn.execute(
            "ALTER TABLE audit_log ADD COLUMN field_name TEXT NOT NULL DEFAULT 'title'"
        )


def _migrate_step_results_usage_columns(conn: sqlite3.Connection) -> None:
    """Add token-usage/cost columns to a step_results table created
    before cost tracking existed, in place, without losing history."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(step_results)")}
    if "prompt_tokens" not in columns:
        conn.execute("ALTER TABLE step_results ADD COLUMN prompt_tokens INTEGER")
    if "completion_tokens" not in columns:
        conn.execute("ALTER TABLE step_results ADD COLUMN completion_tokens INTEGER")
    if "estimated_cost_usd" not in columns:
        conn.execute("ALTER TABLE step_results ADD COLUMN estimated_cost_usd REAL")


def get_cached_step_result(
    conn: sqlite3.Connection, step_name: str, document_id: int, fingerprint: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM step_results WHERE step_name = ? AND document_id = ? AND fingerprint = ?",
        (step_name, document_id, fingerprint),
    ).fetchone()


def save_step_result(
    conn: sqlite3.Connection,
    step_name: str,
    document_id: int,
    fingerprint: str,
    output_value: str,
    provider: str,
    model: str,
    prompt_name: str,
    prompt_version: str,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    estimated_cost_usd: float | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO step_results
            (step_name, document_id, fingerprint, output_value, provider, model,
             prompt_name, prompt_version, prompt_tokens, completion_tokens,
             estimated_cost_usd, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            step_name,
            document_id,
            fingerprint,
            output_value,
            provider,
            model,
            prompt_name,
            prompt_version,
            prompt_tokens,
            completion_tokens,
            estimated_cost_usd,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def get_cached_candidates(
    conn: sqlite3.Connection, endpoint: str, max_age_seconds: float
) -> list[dict] | None:
    """Return a previously cached candidate list (e.g. Paperless tags or
    document types) for `endpoint`, if it was fetched within the last
    `max_age_seconds` - otherwise None, meaning the caller should fetch
    fresh and call save_candidates()."""
    row = conn.execute(
        "SELECT data, fetched_at FROM candidate_cache WHERE endpoint = ?", (endpoint,)
    ).fetchone()
    if row is None:
        return None
    fetched_at = datetime.fromisoformat(row["fetched_at"])
    age = (datetime.now(timezone.utc) - fetched_at).total_seconds()
    if age > max_age_seconds:
        return None
    return json.loads(row["data"])


def save_candidates(conn: sqlite3.Connection, endpoint: str, candidates: list[dict]) -> None:
    conn.execute(
        """
        INSERT INTO candidate_cache (endpoint, data, fetched_at)
        VALUES (?, ?, ?)
        ON CONFLICT(endpoint) DO UPDATE SET data = excluded.data, fetched_at = excluded.fetched_at
        """,
        (endpoint, json.dumps(candidates), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def append_audit_log(
    conn: sqlite3.Connection,
    document_id: int,
    field_name: str,
    old_value: str | None,
    new_value: str,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_log (document_id, field_name, old_value, new_value, changed_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            document_id,
            field_name,
            old_value,
            new_value,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
