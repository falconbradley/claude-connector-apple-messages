"""
Read-only access to the Messages database (``~/Library/Messages/chat.db``).

Why SQLite and not scripting
----------------------------
Messages' AppleScript dictionary exposes only ``account``, ``chat``,
``participant`` and ``file transfer`` — there is **no message class**.  Message
bodies simply cannot be read through scripting, so the database is the only
read path.  That is why this extension needs Full Disk Access while the Apple
Mail extension does not.

Everything here is strictly read-only: connections are opened ``mode=ro`` and
no statement in this module mutates the database.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .typedstream import OBJECT_REPLACEMENT, message_text

logger = logging.getLogger("apple_messages_mcp.db")

DEFAULT_DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"

# Cocoa reference date: 2001-01-01T00:00:00Z, as Unix epoch seconds.
APPLE_EPOCH_OFFSET = 978_307_200

# Messages stored seconds before macOS 13, nanoseconds since.  Any plausible
# second-valued timestamp is far below this; any nanosecond one is far above.
_NANOSECOND_THRESHOLD = 100_000_000_000

# associated_message_type -> human label.  The 3xxx range is the "removed"
# counterpart of each 2xxx reaction.
_TAPBACK_TYPES: dict[int, str] = {
    2000: "loved",
    2001: "liked",
    2002: "disliked",
    2003: "laughed",
    2004: "emphasized",
    2005: "questioned",
    2006: "reacted",          # arbitrary emoji reaction (macOS 14+)
    2007: "sticker",
}


class MessagesDBError(RuntimeError):
    """Raised when the database cannot be opened or queried."""


def _is_permission_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "authorization denied" in text or "unable to open database" in text


class MessagesDB:
    """Thin read-only wrapper around chat.db."""

    def __init__(self, path: Path | str = DEFAULT_DB_PATH) -> None:
        self.path = Path(path).expanduser()
        self._conn: Optional[sqlite3.Connection] = None
        self._snapshot_dir: Optional[str] = None

    # -- connection ------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn

        if not self.path.exists():
            raise MessagesDBError(
                f"No Messages database at {self.path}. Messages.app may never "
                "have been set up on this Mac."
            )

        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        except sqlite3.Error as exc:
            if _is_permission_error(exc):
                raise MessagesDBError(self._permission_help()) from exc
            # A live WAL that SQLite wants to recover cannot be handled on a
            # read-only handle; fall back to a private snapshot.
            logger.info("Direct read-only open failed (%s); snapshotting", exc)
            conn = self._connect_snapshot()

        conn.row_factory = sqlite3.Row
        self._conn = conn
        return conn

    def _connect_snapshot(self) -> sqlite3.Connection:
        """Copy the database (and its WAL sidecars) somewhere we can read."""
        self._snapshot_dir = tempfile.mkdtemp(prefix="apple-messages-mcp-")
        target = Path(self._snapshot_dir) / "chat.db"
        try:
            shutil.copy2(self.path, target)
            for suffix in ("-wal", "-shm"):
                sidecar = self.path.with_name(self.path.name + suffix)
                if sidecar.exists():
                    shutil.copy2(sidecar, target.with_name(target.name + suffix))
        except OSError as exc:
            if _is_permission_error(exc) or isinstance(exc, PermissionError):
                raise MessagesDBError(self._permission_help()) from exc
            raise MessagesDBError(f"Could not snapshot chat.db: {exc}") from exc

        return sqlite3.connect(f"file:{target}?mode=ro", uri=True)

    def _permission_help(self) -> str:
        return (
            "Cannot read the Messages database — Full Disk Access is not "
            "granted.\n\n"
            "Open System Settings -> Privacy & Security -> Full Disk Access, "
            "enable the app hosting this extension (Claude), then quit and "
            "reopen it. macOS caches this permission at launch, so the restart "
            "is required.\n\n"
            "Unlike the Apple Mail extension, this cannot be avoided: Messages' "
            "scripting interface exposes no message class, so message bodies "
            "are only readable from the database."
        )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._snapshot_dir:
            shutil.rmtree(self._snapshot_dir, ignore_errors=True)
            self._snapshot_dir = None

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        conn = self.connect()
        try:
            return conn.execute(sql, tuple(params)).fetchall()
        except sqlite3.Error as exc:
            raise MessagesDBError(f"Query failed: {exc}") from exc

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def to_datetime(raw: Optional[int]) -> Optional[datetime]:
        """Convert an Apple-epoch timestamp (sec or nsec) to a datetime."""
        if not raw:
            return None
        seconds = raw / 1e9 if raw > _NANOSECOND_THRESHOLD else float(raw)
        try:
            return datetime.fromtimestamp(seconds + APPLE_EPOCH_OFFSET, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    @staticmethod
    def _row_text(row: sqlite3.Row) -> Optional[str]:
        return message_text(row["text"], row["attributedBody"])

    @staticmethod
    def _tapback(row: sqlite3.Row) -> Optional[str]:
        kind = row["associated_message_type"] or 0
        if kind in _TAPBACK_TYPES:
            return _TAPBACK_TYPES[kind]
        if 3000 <= kind < 4000:
            base = _TAPBACK_TYPES.get(kind - 1000)
            return f"removed {base}" if base else "removed reaction"
        return None

    # Shared projection so summaries are identical across every query.
    _MESSAGE_COLUMNS = """
        m.ROWID              AS id,
        m.guid               AS guid,
        m.text               AS text,
        m.attributedBody     AS attributedBody,
        m.date               AS date,
        m.date_read          AS date_read,
        m.date_delivered     AS date_delivered,
        m.date_edited        AS date_edited,
        m.is_from_me         AS is_from_me,
        m.is_read            AS is_read,
        m.service            AS service,
        m.cache_has_attachments AS has_attachments,
        m.associated_message_type AS associated_message_type,
        m.thread_originator_guid  AS thread_originator_guid,
        h.id                 AS sender
    """

    def _to_summary(self, row: sqlite3.Row) -> dict[str, Any]:
        keys = row.keys()
        return {
            "id": row["id"],
            "guid": row["guid"],
            "chat_id": row["chat_id"] if "chat_id" in keys else None,
            "chat_name": row["chat_name"] if "chat_name" in keys else None,
            "text": self._row_text(row),
            "date": self.to_datetime(row["date"]),
            "is_from_me": bool(row["is_from_me"]),
            "sender": None if row["is_from_me"] else row["sender"],
            "service": row["service"],
            "has_attachments": bool(row["has_attachments"]),
            "is_read": bool(row["is_read"]),
            "is_edited": bool(row["date_edited"]),
            "is_unsent": row["text"] is None and row["attributedBody"] is None,
            "reply_to_guid": row["thread_originator_guid"],
            "tapback": self._tapback(row),
        }

    # -- queries ---------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        totals = self._query(
            """
            SELECT (SELECT COUNT(*) FROM message)    AS messages,
                   (SELECT COUNT(*) FROM chat)       AS chats,
                   (SELECT COUNT(*) FROM chat WHERE style = 43) AS groups,
                   (SELECT COUNT(*) FROM attachment) AS attachments,
                   (SELECT COUNT(*) FROM message
                     WHERE is_read = 0 AND is_from_me = 0) AS unread,
                   (SELECT MIN(date) FROM message WHERE date > 0) AS oldest,
                   (SELECT MAX(date) FROM message)   AS newest
            """
        )[0]

        by_service = {
            row["service"]: row["n"]
            for row in self._query(
                "SELECT service, COUNT(*) AS n FROM message "
                "WHERE service IS NOT NULL GROUP BY service ORDER BY n DESC"
            )
        }

        return {
            "total_messages": totals["messages"],
            "total_chats": totals["chats"],
            "group_chats": totals["groups"],
            "unread_messages": totals["unread"],
            "attachments": totals["attachments"],
            "by_service": by_service,
            "oldest_message": self.to_datetime(totals["oldest"]),
            "newest_message": self.to_datetime(totals["newest"]),
            "database_bytes": os.path.getsize(self.path),
        }

    def list_chats(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        rows = self._query(
            """
            SELECT c.ROWID AS id, c.guid, c.chat_identifier, c.display_name,
                   c.service_name, c.style,
                   COUNT(cmj.message_id) AS message_count,
                   MAX(m.date)           AS last_date
            FROM chat c
            JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
            JOIN message m             ON m.ROWID = cmj.message_id
            GROUP BY c.ROWID
            ORDER BY last_date DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )

        chats: list[dict[str, Any]] = []
        for row in rows:
            chats.append(
                {
                    "id": row["id"],
                    "guid": row["guid"],
                    "identifier": row["chat_identifier"],
                    "display_name": row["display_name"] or None,
                    "service": row["service_name"],
                    "is_group": row["style"] == 43,
                    "participants": self.chat_participants(row["id"]),
                    "message_count": row["message_count"],
                    "last_message_date": self.to_datetime(row["last_date"]),
                    "last_message_preview": self._chat_preview(row["id"]),
                }
            )
        return chats

    def chat_participants(self, chat_id: int) -> list[str]:
        return [
            row["id"]
            for row in self._query(
                """
                SELECT h.id FROM handle h
                JOIN chat_handle_join chj ON chj.handle_id = h.ROWID
                WHERE chj.chat_id = ?
                """,
                (chat_id,),
            )
        ]

    def _chat_preview(self, chat_id: int, length: int = 120) -> Optional[str]:
        """Preview of the newest message in a chat that actually has content.

        The newest row is often unsent, an attachment with no caption, or a
        body we cannot decode, so we walk back a short window rather than
        showing an empty preview for an otherwise busy conversation.
        """
        rows = self._query(
            """
            SELECT m.text, m.attributedBody, m.cache_has_attachments
            FROM message m
            JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            WHERE cmj.chat_id = ?
            ORDER BY m.date DESC
            LIMIT 10
            """,
            (chat_id,),
        )
        for row in rows:
            text = self._row_text(row)
            if text:
                # Strip the placeholder Messages uses for inline attachments.
                text = " ".join(text.replace(OBJECT_REPLACEMENT, " ").split())
            if not text:
                if row["cache_has_attachments"]:
                    return "[attachment]"
                continue
            return text if len(text) <= length else text[: length - 1] + "…"
        return None

    def chat_messages(
        self, chat_id: int, limit: int = 50, before_id: Optional[int] = None
    ) -> list[dict[str, Any]]:
        """Most recent messages in a chat, returned oldest-first for reading."""
        clause = "AND m.ROWID < ?" if before_id else ""
        params: list[Any] = [chat_id]
        if before_id:
            params.append(before_id)
        params.append(limit)

        rows = self._query(
            f"""
            SELECT {self._MESSAGE_COLUMNS},
                   c.ROWID AS chat_id,
                   COALESCE(c.display_name, c.chat_identifier) AS chat_name
            FROM message m
            JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            JOIN chat c                ON c.ROWID = cmj.chat_id
            LEFT JOIN handle h         ON h.ROWID = m.handle_id
            WHERE cmj.chat_id = ? {clause}
            ORDER BY m.date DESC
            LIMIT ?
            """,
            params,
        )
        return [self._to_summary(row) for row in reversed(rows)]

    def search(
        self,
        query: str,
        limit: int = 50,
        chat_id: Optional[int] = None,
        from_me: Optional[bool] = None,
        after: Optional[datetime] = None,
        before: Optional[datetime] = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Substring search over message bodies.

        chat.db ships no FTS index, so this is a ``LIKE`` scan.  Rows whose body
        lives only in ``attributedBody`` are invisible to SQL, so we over-fetch
        and re-filter in Python against the decoded text.
        """
        where = ["(m.text LIKE ? ESCAPE '\\' OR m.attributedBody IS NOT NULL)"]
        params: list[Any] = [f"%{_escape_like(query)}%"]

        if chat_id is not None:
            where.append("cmj.chat_id = ?")
            params.append(chat_id)
        if from_me is not None:
            where.append("m.is_from_me = ?")
            params.append(1 if from_me else 0)
        if after:
            where.append("m.date >= ?")
            params.append(_to_apple_ns(after))
        if before:
            where.append("m.date <= ?")
            params.append(_to_apple_ns(before))

        # Over-fetch so the Python-side filter still has enough to fill `limit`.
        scan_limit = max(limit * 20, 500)
        params.append(scan_limit)

        rows = self._query(
            f"""
            SELECT {self._MESSAGE_COLUMNS},
                   c.ROWID AS chat_id,
                   COALESCE(c.display_name, c.chat_identifier) AS chat_name
            FROM message m
            JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            JOIN chat c                ON c.ROWID = cmj.chat_id
            LEFT JOIN handle h         ON h.ROWID = m.handle_id
            WHERE {' AND '.join(where)}
            ORDER BY m.date DESC
            LIMIT ?
            """,
            params,
        )

        needle = query.casefold()
        matches: list[dict[str, Any]] = []
        for row in rows:
            summary = self._to_summary(row)
            if summary["text"] and needle in summary["text"].casefold():
                matches.append(summary)
                if len(matches) > limit:
                    break

        truncated = len(matches) > limit or len(rows) >= scan_limit
        return matches[:limit], truncated

    def get_message(self, message_id: int) -> Optional[dict[str, Any]]:
        rows = self._query(
            f"""
            SELECT {self._MESSAGE_COLUMNS},
                   c.ROWID AS chat_id,
                   COALESCE(c.display_name, c.chat_identifier) AS chat_name
            FROM message m
            LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            LEFT JOIN chat c                ON c.ROWID = cmj.chat_id
            LEFT JOIN handle h              ON h.ROWID = m.handle_id
            WHERE m.ROWID = ?
            LIMIT 1
            """,
            (message_id,),
        )
        if not rows:
            return None

        row = rows[0]
        detail = self._to_summary(row)
        detail["date_read"] = self.to_datetime(row["date_read"])
        detail["date_delivered"] = self.to_datetime(row["date_delivered"])
        detail["attachments"] = self.message_attachments(message_id)
        return detail

    def message_attachments(self, message_id: int) -> list[dict[str, Any]]:
        return [
            {
                "id": row["id"],
                "message_id": message_id,
                "filename": row["filename"],
                "transfer_name": row["transfer_name"],
                "mime_type": row["mime_type"],
                "size": row["total_bytes"] or 0,
                "is_sticker": bool(row["is_sticker"]),
            }
            for row in self._query(
                """
                SELECT a.ROWID AS id, a.filename, a.transfer_name,
                       a.mime_type, a.total_bytes, a.is_sticker
                FROM attachment a
                JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
                WHERE maj.message_id = ?
                """,
                (message_id,),
            )
        ]

    def get_attachment_path(self, attachment_id: int) -> Optional[str]:
        rows = self._query(
            "SELECT filename FROM attachment WHERE ROWID = ? LIMIT 1",
            (attachment_id,),
        )
        return rows[0]["filename"] if rows else None


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards so a literal % or _ in the query stays literal."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _to_apple_ns(when: datetime) -> int:
    """Convert a datetime to an Apple-epoch nanosecond timestamp."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return int((when.timestamp() - APPLE_EPOCH_OFFSET) * 1e9)
