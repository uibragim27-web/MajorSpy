import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from config import DATA_DIR

DB_PATH = DATA_DIR / "messages.db"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _quote_identifier(value: str) -> str:
    if not _IDENTIFIER_RE.match(value):
        raise ValueError("Unsafe SQL identifier")

    return '"' + value + '"'


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    table = _quote_identifier(table_name)
    rows = conn.execute("PRAGMA table_info(" + table + ")").fetchall()
    return {row["name"] for row in rows}


def _ensure_column(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    column_name: str,
    column_sql: str,
) -> None:
    if column_name in _table_columns(conn, table_name):
        return

    table = _quote_identifier(table_name)
    column = _quote_identifier(column_name)
    conn.execute("ALTER TABLE " + table + " ADD COLUMN " + column + " " + column_sql)


def _create_indexes(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        DELETE FROM business_media
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM business_media
            GROUP BY business_connection_id, chat_id, message_id, media_type
        )
        """
    )
    statements = [
        """
        CREATE INDEX IF NOT EXISTS idx_business_messages_connection
        ON business_messages (business_connection_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_business_messages_chat
        ON business_messages (chat_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_business_messages_message
        ON business_messages (message_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_business_messages_user
        ON business_messages (user_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_business_messages_created
        ON business_messages (created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_business_messages_deleted
        ON business_messages (is_deleted)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_business_media_connection
        ON business_media (business_connection_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_business_media_chat
        ON business_media (chat_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_business_media_message
        ON business_media (message_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_business_media_user
        ON business_media (user_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_business_media_created
        ON business_media (created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_business_media_deleted
        ON business_media (is_deleted)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_business_media_unique_message_type
        ON business_media (business_connection_id, chat_id, message_id, media_type)
        """,
    ]
    for statement in statements:
        conn.execute(statement)


def _run_migrations(conn: sqlite3.Connection) -> None:
    connection_columns = _table_columns(conn, "business_connections")
    if "business_connection_id" not in connection_columns:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS business_connections_new (
                business_connection_id TEXT PRIMARY KEY,
                owner_user_id INTEGER NOT NULL,
                owner_username TEXT,
                owner_full_name TEXT,
                is_enabled INTEGER,
                can_reply INTEGER,
                rights TEXT,
                updated_at TEXT
            )
            """
        )
        if "id" in connection_columns and "user_id" in connection_columns:
            conn.execute(
                """
                INSERT OR REPLACE INTO business_connections_new (
                    business_connection_id, owner_user_id, owner_username,
                    owner_full_name, is_enabled, can_reply, updated_at
                )
                SELECT id, user_id, username, full_name, is_enabled, can_reply, updated_at
                FROM business_connections
                WHERE id IS NOT NULL AND user_id IS NOT NULL
                """
            )
        conn.execute("DROP TABLE business_connections")
        conn.execute("ALTER TABLE business_connections_new RENAME TO business_connections")
    else:
        business_connection_columns = {
            "owner_user_id": "INTEGER",
            "owner_username": "TEXT",
            "owner_full_name": "TEXT",
            "is_enabled": "INTEGER",
            "can_reply": "INTEGER",
            "rights": "TEXT",
            "updated_at": "TEXT",
        }
        for column_name, column_sql in business_connection_columns.items():
            _ensure_column(
                conn,
                table_name="business_connections",
                column_name=column_name,
                column_sql=column_sql,
            )

    business_message_columns = {
        "chat_title": "TEXT",
        "user_id": "INTEGER",
        "username": "TEXT",
        "full_name": "TEXT",
        "text": "TEXT",
        "caption": "TEXT",
        "created_at": "TEXT",
        "updated_at": "TEXT",
        "is_edited": "INTEGER DEFAULT 0",
        "is_deleted": "INTEGER DEFAULT 0",
        "deleted_at": "TEXT",
    }
    for column_name, column_sql in business_message_columns.items():
        _ensure_column(
            conn,
            table_name="business_messages",
            column_name=column_name,
            column_sql=column_sql,
        )

    business_media_columns = {
        "chat_title": "TEXT",
        "user_id": "INTEGER",
        "username": "TEXT",
        "full_name": "TEXT",
        "file_id": "TEXT",
        "file_unique_id": "TEXT",
        "file_name": "TEXT",
        "mime_type": "TEXT",
        "file_size": "INTEGER",
        "duration": "INTEGER",
        "caption": "TEXT",
        "local_path": "TEXT",
        "created_at": "TEXT",
        "is_deleted": "INTEGER DEFAULT 0",
        "deleted_at": "TEXT",
        "is_unavailable": "INTEGER DEFAULT 0",
        "cleanup_warned_at": "TEXT",
        "media_cleaned_at": "TEXT",
        "quota_warned_at": "TEXT",
        "quota_delete_after": "TEXT",
    }
    for column_name, column_sql in business_media_columns.items():
        _ensure_column(
            conn,
            table_name="business_media",
            column_name=column_name,
            column_sql=column_sql,
        )

    deleted_columns = {
        "user_id": "INTEGER",
        "deleted_text": "TEXT",
        "media_id": "INTEGER",
        "deleted_at": "TEXT",
    }
    for column_name, column_sql in deleted_columns.items():
        _ensure_column(
            conn,
            table_name="business_deleted_messages",
            column_name=column_name,
            column_sql=column_sql,
        )


def init_db() -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                chat_title TEXT,
                message_id INTEGER NOT NULL,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                text TEXT,
                created_at TEXT,
                updated_at TEXT,
                is_edited INTEGER DEFAULT 0,
                is_deleted INTEGER DEFAULT 0,
                UNIQUE(chat_id, message_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS message_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER,
                old_text TEXT,
                new_text TEXT,
                edited_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS business_connections (
                business_connection_id TEXT PRIMARY KEY,
                owner_user_id INTEGER NOT NULL,
                owner_username TEXT,
                owner_full_name TEXT,
                is_enabled INTEGER,
                can_reply INTEGER,
                rights TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS business_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_connection_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                chat_title TEXT,
                message_id INTEGER NOT NULL,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                text TEXT,
                caption TEXT,
                created_at TEXT,
                updated_at TEXT,
                is_edited INTEGER DEFAULT 0,
                is_deleted INTEGER DEFAULT 0,
                deleted_at TEXT,
                UNIQUE(business_connection_id, chat_id, message_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS business_message_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_connection_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER,
                old_text TEXT,
                new_text TEXT,
                edited_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS business_deleted_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_connection_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER,
                deleted_text TEXT,
                deleted_at TEXT,
                media_id INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS business_media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_connection_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                chat_title TEXT,
                message_id INTEGER NOT NULL,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                media_type TEXT NOT NULL,
                file_id TEXT,
                file_unique_id TEXT,
                file_name TEXT,
                mime_type TEXT,
                file_size INTEGER,
                duration INTEGER,
                caption TEXT,
                local_path TEXT,
                created_at TEXT,
                is_deleted INTEGER DEFAULT 0,
                deleted_at TEXT,
                is_unavailable INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_subscriptions (
                owner_user_id INTEGER PRIMARY KEY,
                trial_started_at TEXT,
                trial_ends_at TEXT,
                paid_until TEXT,
                plan TEXT,
                updated_at TEXT
            )
            """
        )
        _run_migrations(conn)
        _create_indexes(conn)
        conn.commit()

    logging.info("Database initialized")


def ensure_trial_subscription(owner_user_id: int, trial_days: int = 7) -> dict[str, Any]:
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    trial_ends_at = (now_dt + timedelta(days=trial_days)).isoformat()
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO user_subscriptions (
                owner_user_id, trial_started_at, trial_ends_at, paid_until, plan, updated_at
            )
            VALUES (?, ?, ?, NULL, 'trial', ?)
            """,
            (owner_user_id, now, trial_ends_at, now),
        )
        row = conn.execute(
            """
            SELECT *
            FROM user_subscriptions
            WHERE owner_user_id = ?
            """,
            (owner_user_id,),
        ).fetchone()
        conn.commit()

    return dict(row) if row else {}


def get_user_subscription(owner_user_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM user_subscriptions
            WHERE owner_user_id = ?
            """,
            (owner_user_id,),
        ).fetchone()
    return dict(row) if row else None


def save_message(
    *,
    chat_id: int,
    chat_title: str | None,
    message_id: int,
    user_id: int | None,
    username: str | None,
    full_name: str | None,
    text: str,
    created_at: str | None = None,
) -> None:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO messages (
                chat_id, chat_title, message_id, user_id, username, full_name,
                text, created_at, updated_at, is_edited, is_deleted
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
            ON CONFLICT(chat_id, message_id) DO UPDATE SET
                chat_title = excluded.chat_title,
                user_id = excluded.user_id,
                username = excluded.username,
                full_name = excluded.full_name,
                text = excluded.text,
                updated_at = excluded.updated_at
            """,
            (
                chat_id,
                chat_title,
                message_id,
                user_id,
                username,
                full_name,
                text,
                created_at or now,
                now,
            ),
        )
        conn.commit()


def get_message(chat_id: int, message_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM messages
            WHERE chat_id = ? AND message_id = ?
            """,
            (chat_id, message_id),
        ).fetchone()

    return dict(row) if row else None


def update_message_text(chat_id: int, message_id: int, new_text: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE messages
            SET text = ?,
                updated_at = ?,
                is_edited = 1
            WHERE chat_id = ? AND message_id = ?
            """,
            (new_text, utc_now(), chat_id, message_id),
        )
        conn.commit()


def save_message_version(
    *,
    chat_id: int,
    message_id: int,
    user_id: int | None,
    old_text: str | None,
    new_text: str,
    edited_at: str | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO message_versions (
                chat_id, message_id, user_id, old_text, new_text, edited_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (chat_id, message_id, user_id, old_text, new_text, edited_at or utc_now()),
        )
        conn.commit()


def save_business_connection(
    *,
    business_connection_id: str,
    owner_user_id: int,
    owner_username: str | None,
    owner_full_name: str | None,
    is_enabled: bool,
    can_reply: bool | None,
    rights: str | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO business_connections (
                business_connection_id, owner_user_id, owner_username,
                owner_full_name, is_enabled, can_reply, rights, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(business_connection_id) DO UPDATE SET
                owner_user_id = excluded.owner_user_id,
                owner_username = excluded.owner_username,
                owner_full_name = excluded.owner_full_name,
                is_enabled = excluded.is_enabled,
                can_reply = excluded.can_reply,
                rights = excluded.rights,
                updated_at = excluded.updated_at
            """,
            (
                business_connection_id,
                owner_user_id,
                owner_username,
                owner_full_name,
                int(is_enabled),
                None if can_reply is None else int(can_reply),
                rights,
                utc_now(),
            ),
        )
        conn.commit()


def get_connection_owner(business_connection_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT business_connection_id, owner_user_id, owner_username,
                   owner_full_name, is_enabled, can_reply, rights, updated_at
            FROM business_connections
            WHERE business_connection_id = ?
            """,
            (business_connection_id,),
        ).fetchone()

    return dict(row) if row else None


def is_connected_owner(owner_user_id: int) -> bool:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM business_connections
            WHERE owner_user_id = ? AND is_enabled = 1
            LIMIT 1
            """,
            (owner_user_id,),
        ).fetchone()

    return row is not None


def get_owner_connections(owner_user_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT business_connection_id, owner_user_id, owner_username,
                   owner_full_name, is_enabled, can_reply, rights, updated_at
            FROM business_connections
            WHERE owner_user_id = ? AND is_enabled = 1
            ORDER BY updated_at DESC
            """,
            (owner_user_id,),
        ).fetchall()

    return [dict(row) for row in rows]


def _normalize_dialog_query(dialog_query: str) -> tuple[str, int | None, str | None]:
    query = dialog_query.strip()
    numeric_query = None
    username_query = None
    if query.lstrip("-").isdigit():
        numeric_query = int(query)
    elif query.startswith("@") and len(query) > 1:
        username_query = query[1:].lower()
    else:
        username_query = query.lower()

    return query, numeric_query, username_query


def get_dialogs_for_owner(owner_user_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            WITH dialog_rows AS (
                SELECT business_connection_id, chat_id, chat_title, message_id,
                       user_id, username, full_name, created_at
                FROM business_messages
                UNION ALL
                SELECT business_connection_id, chat_id, chat_title, message_id,
                       user_id, username, full_name, created_at
                FROM business_media
            )
            SELECT
                dr.business_connection_id,
                dr.chat_id,
                MAX(dr.chat_title) AS chat_title,
                MAX(dr.username) AS username,
                MAX(dr.full_name) AS full_name,
                COUNT(DISTINCT dr.message_id) AS messages_count,
                MAX(dr.created_at) AS last_message_at
            FROM dialog_rows dr
            WHERE EXISTS (
                SELECT 1
                FROM business_connections bc
                WHERE bc.business_connection_id = dr.business_connection_id
                  AND bc.owner_user_id = ?
                  AND bc.is_enabled = 1
            )
            GROUP BY dr.business_connection_id, dr.chat_id
            ORDER BY last_message_at DESC, dr.chat_id DESC
            """,
            (owner_user_id,),
        ).fetchall()

    return [dict(row) for row in rows]


def find_dialog_for_owner(owner_user_id: int, query: str) -> dict[str, Any] | None:
    _, numeric_query, username_query = _normalize_dialog_query(query)
    with connect() as conn:
        if numeric_query is not None:
            row = conn.execute(
                """
                WITH dialog_rows AS (
                    SELECT business_connection_id, chat_id, chat_title, message_id,
                           user_id, username, full_name, created_at
                    FROM business_messages
                    UNION ALL
                    SELECT business_connection_id, chat_id, chat_title, message_id,
                           user_id, username, full_name, created_at
                    FROM business_media
                )
                SELECT
                    dr.business_connection_id,
                    dr.chat_id,
                    MAX(dr.chat_title) AS chat_title,
                    MAX(dr.username) AS username,
                    MAX(dr.full_name) AS full_name,
                    COUNT(DISTINCT dr.message_id) AS messages_count,
                    MAX(dr.created_at) AS last_message_at
                FROM dialog_rows dr
                WHERE EXISTS (
                    SELECT 1
                    FROM business_connections bc
                    WHERE bc.business_connection_id = dr.business_connection_id
                      AND bc.owner_user_id = ?
                      AND bc.is_enabled = 1
                )
                  AND (dr.chat_id = ? OR dr.user_id = ?)
                GROUP BY dr.business_connection_id, dr.chat_id
                ORDER BY last_message_at DESC, dr.chat_id DESC
                LIMIT 1
                """,
                (owner_user_id, numeric_query, numeric_query),
            ).fetchone()
        else:
            row = conn.execute(
                """
                WITH dialog_rows AS (
                    SELECT business_connection_id, chat_id, chat_title, message_id,
                           user_id, username, full_name, created_at
                    FROM business_messages
                    UNION ALL
                    SELECT business_connection_id, chat_id, chat_title, message_id,
                           user_id, username, full_name, created_at
                    FROM business_media
                )
                SELECT
                    dr.business_connection_id,
                    dr.chat_id,
                    MAX(dr.chat_title) AS chat_title,
                    MAX(dr.username) AS username,
                    MAX(dr.full_name) AS full_name,
                    COUNT(DISTINCT dr.message_id) AS messages_count,
                    MAX(dr.created_at) AS last_message_at
                FROM dialog_rows dr
                WHERE EXISTS (
                    SELECT 1
                    FROM business_connections bc
                    WHERE bc.business_connection_id = dr.business_connection_id
                      AND bc.owner_user_id = ?
                      AND bc.is_enabled = 1
                )
                  AND LOWER(COALESCE(dr.username, '')) = ?
                GROUP BY dr.business_connection_id, dr.chat_id
                ORDER BY last_message_at DESC, dr.chat_id DESC
                LIMIT 1
                """,
                (owner_user_id, username_query),
            ).fetchone()

    return dict(row) if row else None


def find_dialog_globally(query: str) -> dict[str, Any] | None:
    _, numeric_query, username_query = _normalize_dialog_query(query)
    with connect() as conn:
        if numeric_query is not None:
            row = conn.execute(
                """
                WITH dialog_rows AS (
                    SELECT business_connection_id, chat_id, chat_title, message_id,
                           user_id, username, full_name, created_at
                    FROM business_messages
                    UNION ALL
                    SELECT business_connection_id, chat_id, chat_title, message_id,
                           user_id, username, full_name, created_at
                    FROM business_media
                )
                SELECT
                    dr.business_connection_id,
                    bc.owner_user_id,
                    dr.chat_id,
                    MAX(dr.chat_title) AS chat_title,
                    MAX(dr.username) AS username,
                    MAX(dr.full_name) AS full_name,
                    COUNT(DISTINCT dr.message_id) AS messages_count,
                    MAX(dr.created_at) AS last_message_at
                FROM dialog_rows dr
                JOIN business_connections bc
                  ON bc.business_connection_id = dr.business_connection_id
                WHERE bc.is_enabled = 1
                  AND (dr.chat_id = ? OR dr.user_id = ?)
                GROUP BY dr.business_connection_id, bc.owner_user_id, dr.chat_id
                ORDER BY last_message_at DESC, dr.chat_id DESC
                LIMIT 1
                """,
                (numeric_query, numeric_query),
            ).fetchone()
        else:
            row = conn.execute(
                """
                WITH dialog_rows AS (
                    SELECT business_connection_id, chat_id, chat_title, message_id,
                           user_id, username, full_name, created_at
                    FROM business_messages
                    UNION ALL
                    SELECT business_connection_id, chat_id, chat_title, message_id,
                           user_id, username, full_name, created_at
                    FROM business_media
                )
                SELECT
                    dr.business_connection_id,
                    bc.owner_user_id,
                    dr.chat_id,
                    MAX(dr.chat_title) AS chat_title,
                    MAX(dr.username) AS username,
                    MAX(dr.full_name) AS full_name,
                    COUNT(DISTINCT dr.message_id) AS messages_count,
                    MAX(dr.created_at) AS last_message_at
                FROM dialog_rows dr
                JOIN business_connections bc
                  ON bc.business_connection_id = dr.business_connection_id
                WHERE bc.is_enabled = 1
                  AND LOWER(COALESCE(dr.username, '')) = ?
                GROUP BY dr.business_connection_id, bc.owner_user_id, dr.chat_id
                ORDER BY last_message_at DESC, dr.chat_id DESC
                LIMIT 1
                """,
                (username_query,),
            ).fetchone()

    return dict(row) if row else None


def _find_dialog_for_connection(business_connection_id: str, query: str) -> dict[str, Any] | None:
    _, numeric_query, username_query = _normalize_dialog_query(query)
    with connect() as conn:
        if numeric_query is not None:
            row = conn.execute(
                """
                WITH dialog_rows AS (
                    SELECT business_connection_id, chat_id, chat_title, message_id,
                           user_id, username, full_name, created_at
                    FROM business_messages
                    UNION ALL
                    SELECT business_connection_id, chat_id, chat_title, message_id,
                           user_id, username, full_name, created_at
                    FROM business_media
                )
                SELECT
                    business_connection_id,
                    chat_id,
                    MAX(chat_title) AS chat_title,
                    MAX(username) AS username,
                    MAX(full_name) AS full_name,
                    COUNT(DISTINCT message_id) AS messages_count,
                    MAX(created_at) AS last_message_at
                FROM dialog_rows
                WHERE business_connection_id = ?
                  AND (chat_id = ? OR user_id = ?)
                GROUP BY business_connection_id, chat_id
                ORDER BY last_message_at DESC, chat_id DESC
                LIMIT 1
                """,
                (business_connection_id, numeric_query, numeric_query),
            ).fetchone()
        else:
            row = conn.execute(
                """
                WITH dialog_rows AS (
                    SELECT business_connection_id, chat_id, chat_title, message_id,
                           user_id, username, full_name, created_at
                    FROM business_messages
                    UNION ALL
                    SELECT business_connection_id, chat_id, chat_title, message_id,
                           user_id, username, full_name, created_at
                    FROM business_media
                )
                SELECT
                    business_connection_id,
                    chat_id,
                    MAX(chat_title) AS chat_title,
                    MAX(username) AS username,
                    MAX(full_name) AS full_name,
                    COUNT(DISTINCT message_id) AS messages_count,
                    MAX(created_at) AS last_message_at
                FROM dialog_rows
                WHERE business_connection_id = ?
                  AND LOWER(COALESCE(username, '')) = ?
                GROUP BY business_connection_id, chat_id
                ORDER BY last_message_at DESC, chat_id DESC
                LIMIT 1
                """,
                (business_connection_id, username_query),
            ).fetchone()

    return dict(row) if row else None


def export_dialog_data(
    business_connection_id: str,
    dialog_query: str,
    limit: int,
) -> dict[str, Any] | None:
    dialog = _find_dialog_for_connection(business_connection_id, dialog_query)
    if not dialog:
        return None

    chat_id = int(dialog["chat_id"])
    limit = max(1, int(limit))
    with connect() as conn:
        total_messages = conn.execute(
            """
            WITH base AS (
                SELECT business_connection_id, chat_id, message_id
                FROM business_messages
                WHERE business_connection_id = ? AND chat_id = ?
                UNION
                SELECT business_connection_id, chat_id, message_id
                FROM business_media
                WHERE business_connection_id = ? AND chat_id = ?
            )
            SELECT COUNT(*)
            FROM base
            """,
            (business_connection_id, chat_id, business_connection_id, chat_id),
        ).fetchone()[0]
        message_rows = conn.execute(
            """
            WITH base AS (
                SELECT business_connection_id, chat_id, message_id, created_at
                FROM business_messages
                WHERE business_connection_id = ? AND chat_id = ?
                UNION
                SELECT business_connection_id, chat_id, message_id, created_at
                FROM business_media
                WHERE business_connection_id = ? AND chat_id = ?
            ),
            limited AS (
                SELECT business_connection_id, chat_id, message_id,
                       MIN(COALESCE(created_at, '')) AS created_at
                FROM base
                GROUP BY business_connection_id, chat_id, message_id
                ORDER BY created_at DESC, message_id DESC
                LIMIT ?
            )
            SELECT bm.*
            FROM limited lm
            JOIN business_messages bm
              ON bm.business_connection_id = lm.business_connection_id
             AND bm.chat_id = lm.chat_id
             AND bm.message_id = lm.message_id
            ORDER BY COALESCE(lm.created_at, ''), lm.message_id
            """,
            (business_connection_id, chat_id, business_connection_id, chat_id, limit),
        ).fetchall()
        media_rows = conn.execute(
            """
            WITH base AS (
                SELECT business_connection_id, chat_id, message_id, created_at
                FROM business_messages
                WHERE business_connection_id = ? AND chat_id = ?
                UNION
                SELECT business_connection_id, chat_id, message_id, created_at
                FROM business_media
                WHERE business_connection_id = ? AND chat_id = ?
            ),
            limited AS (
                SELECT business_connection_id, chat_id, message_id,
                       MIN(COALESCE(created_at, '')) AS created_at
                FROM base
                GROUP BY business_connection_id, chat_id, message_id
                ORDER BY created_at DESC, message_id DESC
                LIMIT ?
            )
            SELECT bm.*
            FROM limited lm
            JOIN business_media bm
              ON bm.business_connection_id = lm.business_connection_id
             AND bm.chat_id = lm.chat_id
             AND bm.message_id = lm.message_id
            ORDER BY COALESCE(lm.created_at, ''), lm.message_id, bm.id
            """,
            (business_connection_id, chat_id, business_connection_id, chat_id, limit),
        ).fetchall()
        version_rows = conn.execute(
            """
            WITH base AS (
                SELECT business_connection_id, chat_id, message_id, created_at
                FROM business_messages
                WHERE business_connection_id = ? AND chat_id = ?
                UNION
                SELECT business_connection_id, chat_id, message_id, created_at
                FROM business_media
                WHERE business_connection_id = ? AND chat_id = ?
            ),
            limited AS (
                SELECT business_connection_id, chat_id, message_id,
                       MIN(COALESCE(created_at, '')) AS created_at
                FROM base
                GROUP BY business_connection_id, chat_id, message_id
                ORDER BY created_at DESC, message_id DESC
                LIMIT ?
            )
            SELECT bmv.*
            FROM limited lm
            JOIN business_message_versions bmv
              ON bmv.business_connection_id = lm.business_connection_id
             AND bmv.chat_id = lm.chat_id
             AND bmv.message_id = lm.message_id
            ORDER BY COALESCE(bmv.edited_at, ''), bmv.id
            """,
            (business_connection_id, chat_id, business_connection_id, chat_id, limit),
        ).fetchall()
        deleted_rows = conn.execute(
            """
            WITH base AS (
                SELECT business_connection_id, chat_id, message_id, created_at
                FROM business_messages
                WHERE business_connection_id = ? AND chat_id = ?
                UNION
                SELECT business_connection_id, chat_id, message_id, created_at
                FROM business_media
                WHERE business_connection_id = ? AND chat_id = ?
            ),
            limited AS (
                SELECT business_connection_id, chat_id, message_id,
                       MIN(COALESCE(created_at, '')) AS created_at
                FROM base
                GROUP BY business_connection_id, chat_id, message_id
                ORDER BY created_at DESC, message_id DESC
                LIMIT ?
            )
            SELECT bdm.*
            FROM limited lm
            JOIN business_deleted_messages bdm
              ON bdm.business_connection_id = lm.business_connection_id
             AND bdm.chat_id = lm.chat_id
             AND bdm.message_id = lm.message_id
            ORDER BY COALESCE(bdm.deleted_at, ''), bdm.id
            """,
            (business_connection_id, chat_id, business_connection_id, chat_id, limit),
        ).fetchall()

    return {
        "dialog": dialog,
        "messages": [dict(row) for row in message_rows],
        "media": [dict(row) for row in media_rows],
        "versions": [dict(row) for row in version_rows],
        "deleted": [dict(row) for row in deleted_rows],
        "total_messages": total_messages,
        "limit": limit,
        "truncated": total_messages > limit,
    }


def save_business_message(
    *,
    business_connection_id: str,
    chat_id: int,
    chat_title: str | None,
    message_id: int,
    user_id: int | None,
    username: str | None,
    full_name: str | None,
    text: str | None,
    caption: str | None = None,
    created_at: str | None = None,
) -> None:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO business_messages (
                business_connection_id, chat_id, chat_title, message_id,
                user_id, username, full_name, text, caption, created_at,
                updated_at, is_edited, is_deleted
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
            ON CONFLICT(business_connection_id, chat_id, message_id) DO UPDATE SET
                chat_title = excluded.chat_title,
                user_id = excluded.user_id,
                username = excluded.username,
                full_name = excluded.full_name,
                text = excluded.text,
                caption = excluded.caption,
                updated_at = excluded.updated_at
            """,
            (
                business_connection_id,
                chat_id,
                chat_title,
                message_id,
                user_id,
                username,
                full_name,
                text,
                caption,
                created_at or now,
                now,
            ),
        )
        conn.commit()


def get_business_message(
    business_connection_id: str,
    chat_id: int,
    message_id: int,
) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM business_messages
            WHERE business_connection_id = ? AND chat_id = ? AND message_id = ?
            """,
            (business_connection_id, chat_id, message_id),
        ).fetchone()

    return dict(row) if row else None


def update_business_message_content(
    business_connection_id: str,
    chat_id: int,
    message_id: int,
    *,
    text: str | None,
    caption: str | None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE business_messages
            SET text = ?,
                caption = ?,
                updated_at = ?,
                is_edited = 1
            WHERE business_connection_id = ? AND chat_id = ? AND message_id = ?
            """,
            (text, caption, utc_now(), business_connection_id, chat_id, message_id),
        )
        conn.commit()


def save_business_message_version(
    *,
    business_connection_id: str,
    chat_id: int,
    message_id: int,
    user_id: int | None,
    old_text: str | None,
    new_text: str | None,
    edited_at: str | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO business_message_versions (
                business_connection_id, chat_id, message_id, user_id,
                old_text, new_text, edited_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                business_connection_id,
                chat_id,
                message_id,
                user_id,
                old_text,
                new_text,
                edited_at or utc_now(),
            ),
        )
        conn.commit()


def save_business_media(
    *,
    business_connection_id: str,
    chat_id: int,
    chat_title: str | None,
    message_id: int,
    user_id: int | None,
    username: str | None,
    full_name: str | None,
    media_type: str,
    file_id: str | None,
    file_unique_id: str | None,
    file_name: str | None,
    mime_type: str | None,
    file_size: int | None,
    duration: int | None,
    caption: str | None,
    local_path: str | None,
    created_at: str | None = None,
    is_unavailable: bool = False,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO business_media (
                business_connection_id, chat_id, chat_title, message_id,
                user_id, username, full_name, media_type, file_id,
                file_unique_id, file_name, mime_type, file_size, duration,
                caption, local_path, created_at, is_deleted, is_unavailable
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(business_connection_id, chat_id, message_id, media_type)
            DO UPDATE SET
                chat_title = excluded.chat_title,
                user_id = excluded.user_id,
                username = excluded.username,
                full_name = excluded.full_name,
                file_id = excluded.file_id,
                file_unique_id = excluded.file_unique_id,
                file_name = excluded.file_name,
                mime_type = excluded.mime_type,
                file_size = excluded.file_size,
                duration = excluded.duration,
                caption = excluded.caption,
                local_path = excluded.local_path,
                created_at = excluded.created_at,
                is_unavailable = excluded.is_unavailable
            """,
            (
                business_connection_id,
                chat_id,
                chat_title,
                message_id,
                user_id,
                username,
                full_name,
                media_type,
                file_id,
                file_unique_id,
                file_name,
                mime_type,
                file_size,
                duration,
                caption,
                local_path,
                created_at or utc_now(),
                int(is_unavailable),
            ),
        )
        conn.commit()


def get_business_media(
    business_connection_id: str,
    chat_id: int,
    message_id: int,
) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM business_media
            WHERE business_connection_id = ? AND chat_id = ? AND message_id = ?
            ORDER BY id
            """,
            (business_connection_id, chat_id, message_id),
        ).fetchall()

    return [dict(row) for row in rows]


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def get_media_cleanup_warning_candidates(retention_days: int) -> list[dict[str, Any]]:
    warn_before = datetime.now(timezone.utc) - timedelta(days=max(1, retention_days - 1))
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT bm.*, bc.owner_user_id
            FROM business_media bm
            JOIN business_connections bc
              ON bc.business_connection_id = bm.business_connection_id
            WHERE bm.local_path IS NOT NULL
              AND bm.media_cleaned_at IS NULL
              AND bm.cleanup_warned_at IS NULL
              AND bc.is_enabled = 1
            ORDER BY bm.created_at
            """
        ).fetchall()

    result = []
    for row in rows:
        item = dict(row)
        created_at = _parse_datetime(item.get("created_at"))
        if created_at and created_at <= warn_before:
            result.append(item)
    return result


def mark_media_cleanup_warned(media_ids: list[int]) -> None:
    if not media_ids:
        return

    now = utc_now()
    placeholders = ",".join("?" for _ in media_ids)
    with connect() as conn:
        conn.execute(
            f"UPDATE business_media SET cleanup_warned_at = ? WHERE id IN ({placeholders})",
            (now, *media_ids),
        )
        conn.commit()


def get_media_cleanup_delete_candidates(retention_days: int) -> list[dict[str, Any]]:
    delete_before = datetime.now(timezone.utc) - timedelta(days=retention_days)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT bm.*, bc.owner_user_id
            FROM business_media bm
            JOIN business_connections bc
              ON bc.business_connection_id = bm.business_connection_id
            WHERE bm.local_path IS NOT NULL
              AND bm.media_cleaned_at IS NULL
              AND bc.is_enabled = 1
            ORDER BY bm.created_at
            """
        ).fetchall()

    result = []
    for row in rows:
        item = dict(row)
        created_at = _parse_datetime(item.get("created_at"))
        if created_at and created_at <= delete_before:
            result.append(item)
    return result


def mark_media_cleaned(media_ids: list[int]) -> None:
    if not media_ids:
        return

    now = utc_now()
    placeholders = ",".join("?" for _ in media_ids)
    with connect() as conn:
        conn.execute(
            f"UPDATE business_media SET media_cleaned_at = ? WHERE id IN ({placeholders})",
            (now, *media_ids),
        )
        conn.commit()


def get_active_media_with_owners() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT bm.*, bc.owner_user_id
            FROM business_media bm
            JOIN business_connections bc
              ON bc.business_connection_id = bm.business_connection_id
            WHERE bm.local_path IS NOT NULL
              AND bm.media_cleaned_at IS NULL
              AND bc.is_enabled = 1
            ORDER BY bc.owner_user_id, COALESCE(bm.file_size, 0), bm.created_at
            """
        ).fetchall()
    return [dict(row) for row in rows]


def mark_media_quota_warned(media_ids: list[int], delete_after: str) -> None:
    if not media_ids:
        return

    now = utc_now()
    placeholders = ",".join("?" for _ in media_ids)
    with connect() as conn:
        conn.execute(
            f"""
            UPDATE business_media
            SET quota_warned_at = ?,
                quota_delete_after = ?
            WHERE id IN ({placeholders})
            """,
            (now, delete_after, *media_ids),
        )
        conn.commit()


def update_business_media_caption(
    business_connection_id: str,
    chat_id: int,
    message_id: int,
    caption: str | None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE business_media
            SET caption = ?
            WHERE business_connection_id = ? AND chat_id = ? AND message_id = ?
            """,
            (caption, business_connection_id, chat_id, message_id),
        )
        conn.commit()


def resolve_deleted_business_messages(
    *,
    business_connection_id: str,
    chat_id: int,
    message_ids: list[int],
) -> list[dict[str, Any]]:
    deleted_at = utc_now()
    result: list[dict[str, Any]] = []

    with connect() as conn:
        for message_id in message_ids:
            message_row = conn.execute(
                """
                SELECT *
                FROM business_messages
                WHERE business_connection_id = ? AND chat_id = ? AND message_id = ?
                """,
                (business_connection_id, chat_id, message_id),
            ).fetchone()
            media_rows = conn.execute(
                """
                SELECT *
                FROM business_media
                WHERE business_connection_id = ? AND chat_id = ? AND message_id = ?
                ORDER BY id
                """,
                (business_connection_id, chat_id, message_id),
            ).fetchall()

            message = dict(message_row) if message_row else None
            media = [dict(row) for row in media_rows]
            deleted_text = None
            if message:
                deleted_text = message.get("text") or message.get("caption") or "[нет текста]"

            user_id = message.get("user_id") if message else None
            if user_id is None and media:
                user_id = media[0].get("user_id")

            conn.execute(
                """
                UPDATE business_messages
                SET is_deleted = 1,
                    deleted_at = ?,
                    updated_at = ?
                WHERE business_connection_id = ? AND chat_id = ? AND message_id = ?
                """,
                (deleted_at, deleted_at, business_connection_id, chat_id, message_id),
            )
            conn.execute(
                """
                UPDATE business_media
                SET is_deleted = 1,
                    deleted_at = ?
                WHERE business_connection_id = ? AND chat_id = ? AND message_id = ?
                """,
                (deleted_at, business_connection_id, chat_id, message_id),
            )

            if not message and not media:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO business_messages (
                        business_connection_id, chat_id, message_id, created_at,
                        updated_at, is_edited, is_deleted, deleted_at
                    )
                    VALUES (?, ?, ?, ?, ?, 0, 1, ?)
                    """,
                    (business_connection_id, chat_id, message_id, deleted_at, deleted_at, deleted_at),
                )

            conn.execute(
                """
                INSERT INTO business_deleted_messages (
                    business_connection_id, chat_id, message_id, user_id,
                    deleted_text, deleted_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (business_connection_id, chat_id, message_id, user_id, deleted_text, deleted_at),
            )

            for item in media:
                conn.execute(
                    """
                    INSERT INTO business_deleted_messages (
                        business_connection_id, chat_id, message_id, user_id,
                        deleted_text, deleted_at, media_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        business_connection_id,
                        chat_id,
                        message_id,
                        item.get("user_id"),
                        item.get("caption"),
                        deleted_at,
                        item.get("id"),
                    ),
                )

            result.append(
                {
                    "business_connection_id": business_connection_id,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "message": message,
                    "media": media,
                    "deleted_text": deleted_text,
                    "deleted_at": deleted_at,
                    "found_text": message is not None and deleted_text is not None,
                    "found_media": bool(media),
                    "found_any": message is not None or bool(media),
                }
            )

        conn.commit()

    return result


def get_stats() -> dict[str, int]:
    with connect() as conn:
        stats = {
            "total_messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "edited_messages": conn.execute(
                "SELECT COUNT(*) FROM messages WHERE is_edited = 1"
            ).fetchone()[0],
            "versions": conn.execute("SELECT COUNT(*) FROM message_versions").fetchone()[0],
            "unique_chats": conn.execute(
                "SELECT COUNT(DISTINCT chat_id) FROM messages"
            ).fetchone()[0],
            "unique_users": conn.execute(
                "SELECT COUNT(DISTINCT user_id) FROM messages WHERE user_id IS NOT NULL"
            ).fetchone()[0],
            "business_connections": conn.execute(
                "SELECT COUNT(*) FROM business_connections"
            ).fetchone()[0],
            "business_messages": conn.execute("SELECT COUNT(*) FROM business_messages").fetchone()[0],
            "business_edited_messages": conn.execute(
                "SELECT COUNT(*) FROM business_messages WHERE is_edited = 1"
            ).fetchone()[0],
            "business_versions": conn.execute(
                "SELECT COUNT(*) FROM business_message_versions"
            ).fetchone()[0],
            "business_deleted_messages": conn.execute(
                "SELECT COUNT(*) FROM business_deleted_messages"
            ).fetchone()[0],
            "business_unique_chats": conn.execute(
                "SELECT COUNT(DISTINCT chat_id) FROM business_messages"
            ).fetchone()[0],
            "business_unique_users": conn.execute(
                "SELECT COUNT(DISTINCT user_id) FROM business_messages WHERE user_id IS NOT NULL"
            ).fetchone()[0],
            "business_media": conn.execute("SELECT COUNT(*) FROM business_media").fetchone()[0],
            "business_deleted_media": conn.execute(
                "SELECT COUNT(*) FROM business_media WHERE is_deleted = 1"
            ).fetchone()[0],
        }

    return stats


def get_stats_for_owner(owner_user_id: int) -> dict[str, int]:
    with connect() as conn:
        stats = {
            "business_connections": conn.execute(
                """
                SELECT COUNT(*)
                FROM business_connections
                WHERE owner_user_id = ? AND is_enabled = 1
                """,
                (owner_user_id,),
            ).fetchone()[0],
            "business_messages": conn.execute(
                """
                SELECT COUNT(*)
                FROM business_messages bm
                WHERE EXISTS (
                    SELECT 1
                    FROM business_connections bc
                    WHERE bc.business_connection_id = bm.business_connection_id
                      AND bc.owner_user_id = ?
                      AND bc.is_enabled = 1
                )
                """,
                (owner_user_id,),
            ).fetchone()[0],
            "business_media": conn.execute(
                """
                SELECT COUNT(*)
                FROM business_media bm
                WHERE EXISTS (
                    SELECT 1
                    FROM business_connections bc
                    WHERE bc.business_connection_id = bm.business_connection_id
                      AND bc.owner_user_id = ?
                      AND bc.is_enabled = 1
                )
                """,
                (owner_user_id,),
            ).fetchone()[0],
            "business_deleted_messages": conn.execute(
                """
                SELECT COUNT(*)
                FROM business_deleted_messages bdm
                WHERE EXISTS (
                    SELECT 1
                    FROM business_connections bc
                    WHERE bc.business_connection_id = bdm.business_connection_id
                      AND bc.owner_user_id = ?
                      AND bc.is_enabled = 1
                )
                """,
                (owner_user_id,),
            ).fetchone()[0],
            "business_versions": conn.execute(
                """
                SELECT COUNT(*)
                FROM business_message_versions bmv
                WHERE EXISTS (
                    SELECT 1
                    FROM business_connections bc
                    WHERE bc.business_connection_id = bmv.business_connection_id
                      AND bc.owner_user_id = ?
                      AND bc.is_enabled = 1
                )
                """,
                (owner_user_id,),
            ).fetchone()[0],
            "business_unique_chats": conn.execute(
                """
                SELECT COUNT(DISTINCT bm.chat_id)
                FROM business_messages bm
                WHERE EXISTS (
                    SELECT 1
                    FROM business_connections bc
                    WHERE bc.business_connection_id = bm.business_connection_id
                      AND bc.owner_user_id = ?
                      AND bc.is_enabled = 1
                )
                """,
                (owner_user_id,),
            ).fetchone()[0],
            "business_unique_users": conn.execute(
                """
                SELECT COUNT(DISTINCT bm.user_id)
                FROM business_messages bm
                WHERE bm.user_id IS NOT NULL
                  AND EXISTS (
                    SELECT 1
                    FROM business_connections bc
                    WHERE bc.business_connection_id = bm.business_connection_id
                      AND bc.owner_user_id = ?
                      AND bc.is_enabled = 1
                )
                """,
                (owner_user_id,),
            ).fetchone()[0],
        }

    return stats
