from __future__ import annotations

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
        CREATE TABLE IF NOT EXISTS title_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL,
            fingerprint TEXT NOT NULL,
            title TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_name TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (document_id, fingerprint)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL,
            old_title TEXT,
            new_title TEXT NOT NULL,
            changed_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def get_cached_title(
    conn: sqlite3.Connection, document_id: int, fingerprint: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM title_results WHERE document_id = ? AND fingerprint = ?",
        (document_id, fingerprint),
    ).fetchone()


def save_title_result(
    conn: sqlite3.Connection,
    document_id: int,
    fingerprint: str,
    title: str,
    provider: str,
    model: str,
    prompt_name: str,
    prompt_version: str,
) -> None:
    conn.execute(
        """
        INSERT INTO title_results
            (document_id, fingerprint, title, provider, model, prompt_name, prompt_version, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            document_id,
            fingerprint,
            title,
            provider,
            model,
            prompt_name,
            prompt_version,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def append_audit_log(
    conn: sqlite3.Connection, document_id: int, old_title: str | None, new_title: str
) -> None:
    conn.execute(
        """
        INSERT INTO audit_log (document_id, old_title, new_title, changed_at)
        VALUES (?, ?, ?, ?)
        """,
        (document_id, old_title, new_title, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
