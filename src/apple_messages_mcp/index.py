"""
Full-history search index for message bodies.

Why this exists
---------------
Two properties of chat.db make searching it directly a losing game:

  * There is no text index, so any match is a full scan.
  * Since Ventura most bodies live only in ``message.attributedBody`` as a
    typedstream blob.  SQL cannot see inside it, so a ``LIKE`` against
    ``message.text`` silently misses the majority of messages.

The first implementation tried to paper over the second point by widening the
predicate to ``m.text LIKE ? OR m.attributedBody IS NOT NULL`` and re-filtering
the decoded text in Python.  That predicate is true for nearly every modern
row, so the query's ``LIMIT`` truncated the scan to the newest few hundred
messages: any older match became invisible.  That is a silent wrong answer,
not merely a slow one.

The fix is to decode once instead of per query.  This module mirrors decoded
bodies into a separate database that we own and can index, so the substring
match runs in SQL over the *complete* history and the other filters, the
ordering, and the ``LIMIT`` all mean what they say.

Design notes
------------
**Casefolded only.**  The mirror stores ``str.casefold()`` of each body and
nothing else.  Display text still comes from chat.db, so the mirror is purely a
matching oracle — which halves its size and, more importantly, gives exact
case-insensitive matching for non-ASCII text.  SQLite's ``LIKE`` folds case for
ASCII alone, so relying on it would have made ``Ä`` and ``ä`` different needles.

**Plain table, not FTS5.**  The README originally planned an FTS5 mirror.  FTS5
buys tokenized matching, which is a different and *narrower* thing than the
substring semantics ``search_messages`` documents: ``MATCH 'dentist'`` does not
find "mydentist", because FTS5 matches whole tokens.  Since a substring scan
over compact casefolded text is already milliseconds-fast (the mirror is a few
tens of MB even for a large history, versus 916 MB for chat.db), FTS5 would
have doubled the on-disk index for semantics we cannot use.  If a query ever
does prove slow, adding an FTS5 table alongside this one is a contained change.

**Pure cache.**  It lives under ~/Library/Caches and can be deleted at any
time; the next search rebuilds it.  Nothing here writes to chat.db.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .typedstream import message_text

logger = logging.getLogger("apple_messages_mcp.index")

DEFAULT_CACHE_DIR = Path.home() / "Library" / "Caches" / "apple-messages-mcp"
INDEX_FILENAME = "search-index.db"

# Bump when the stored shape changes, to force a rebuild on upgrade.
SCHEMA_VERSION = 1

# Rows are keyed by message.ROWID, which is monotonic, so new messages are
# found with a simple watermark.  Edits and unsends reuse an existing ROWID and
# would otherwise be missed, so each refresh also re-reads this many of the
# newest rows.  Recent history is where edits realistically happen, and 2000
# rows costs milliseconds.
REINDEX_WINDOW = 2000

_BATCH = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS body (
    message_id INTEGER PRIMARY KEY,
    folded     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class SearchIndexError(RuntimeError):
    """Raised when the index cannot be created or updated."""


@dataclass
class RefreshReport:
    """What a call to :meth:`SearchIndex.refresh` actually did."""

    indexed_messages: int   # rows currently in the mirror
    scanned: int            # source rows read this pass
    added: int              # message ids not previously indexed
    updated: int            # existing rows whose body had changed (an edit)
    removed: int            # rows whose body went away (an unsend)
    rebuilt: bool           # whether the mirror was dropped and rebuilt
    seconds: float
    index_bytes: int

    @property
    def changed(self) -> int:
        return self.added + self.updated + self.removed

    def as_dict(self) -> dict:
        return {
            "indexed_messages": self.indexed_messages,
            "scanned": self.scanned,
            "added": self.added,
            "updated": self.updated,
            "removed": self.removed,
            "rebuilt": self.rebuilt,
            "seconds": round(self.seconds, 3),
            "index_bytes": self.index_bytes,
        }


class SearchIndex:
    """A casefolded mirror of decoded message bodies, keyed by message ROWID."""

    def __init__(self, path: Path | str | None = None) -> None:
        if path is None:
            path = DEFAULT_CACHE_DIR / INDEX_FILENAME
        self.path = Path(path).expanduser()

    # -- plumbing --------------------------------------------------------

    def _open_writer(self) -> sqlite3.Connection:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path)
        except (OSError, sqlite3.Error) as exc:
            raise SearchIndexError(
                f"Cannot open the search index at {self.path}: {exc}"
            ) from exc

        # The mirror is disposable, so durability is not worth the fsyncs.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=OFF")
        conn.executescript(_SCHEMA)
        return conn

    def attach_uri(self) -> str:
        """Read-only URI for ATTACHing the mirror to a chat.db connection."""
        return f"file:{self.path}?mode=ro"

    def exists(self) -> bool:
        return self.path.exists()

    def _get_meta(self, conn: sqlite3.Connection, key: str) -> Optional[str]:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, conn: sqlite3.Connection, key: str, value) -> None:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    def _size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    # -- status ----------------------------------------------------------

    def status(self) -> dict:
        """Describe the mirror without touching chat.db."""
        if not self.exists():
            return {
                "built": False,
                "indexed_messages": 0,
                "watermark": 0,
                "index_bytes": 0,
                "path": str(self.path),
            }
        conn = self._open_writer()
        try:
            count = conn.execute("SELECT COUNT(*) FROM body").fetchone()[0]
            watermark = int(self._get_meta(conn, "watermark") or 0)
            built_at = self._get_meta(conn, "built_at")
        finally:
            conn.close()
        return {
            "built": True,
            "indexed_messages": count,
            "watermark": watermark,
            "built_at": built_at,
            "index_bytes": self._size(),
            "path": str(self.path),
        }

    # -- refresh ---------------------------------------------------------

    def refresh(
        self, source: sqlite3.Connection, rebuild: bool = False
    ) -> RefreshReport:
        """Bring the mirror up to date with ``source`` (an open chat.db).

        Incremental: only rows newer than the stored watermark are decoded,
        plus a small window of recent rows to catch edits.  Pass
        ``rebuild=True`` to discard the mirror and decode everything again,
        which is the escape hatch for edits older than that window.
        """
        started = time.monotonic()
        conn = self._open_writer()
        try:
            stored_version = int(self._get_meta(conn, "schema_version") or 0)
            watermark = int(self._get_meta(conn, "watermark") or 0)

            source_max = source.execute(
                "SELECT COALESCE(MAX(ROWID), 0) FROM message"
            ).fetchone()[0]

            # A source whose newest ROWID sits below our watermark is not the
            # database we indexed — restored from backup, or a fresh library.
            # Stale rows would linger forever, so start over.
            replaced = source_max < watermark
            if rebuild or replaced or stored_version != SCHEMA_VERSION:
                if replaced:
                    logger.info(
                        "chat.db appears replaced (max ROWID %d < watermark %d); "
                        "rebuilding index",
                        source_max,
                        watermark,
                    )
                # Only call it a rebuild if there was something to discard —
                # the very first build is not a rebuild.
                rebuilt = bool(
                    conn.execute("SELECT EXISTS(SELECT 1 FROM body)").fetchone()[0]
                )
                conn.execute("DELETE FROM body")
                self._set_meta(conn, "schema_version", SCHEMA_VERSION)
                watermark = 0
            else:
                rebuilt = False

            window_start = max(0, watermark - REINDEX_WINDOW)
            scanned, added, updated, removed = self._ingest(
                conn, source, watermark, window_start
            )

            self._set_meta(conn, "watermark", max(watermark, source_max))
            self._set_meta(conn, "built_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
            conn.commit()

            total = conn.execute("SELECT COUNT(*) FROM body").fetchone()[0]
        except sqlite3.Error as exc:
            raise SearchIndexError(f"Could not update the search index: {exc}") from exc
        finally:
            conn.close()

        report = RefreshReport(
            indexed_messages=total,
            scanned=scanned,
            added=added,
            updated=updated,
            removed=removed,
            rebuilt=rebuilt,
            seconds=time.monotonic() - started,
            index_bytes=self._size(),
        )
        if report.changed or report.rebuilt:
            logger.info(
                "Search index: %d indexed (+%d new, %d edited, %d removed)%s in %.2fs",
                report.indexed_messages,
                report.added,
                report.updated,
                report.removed,
                " [full rebuild]" if report.rebuilt else "",
                report.seconds,
            )
        return report

    def _ingest(
        self,
        conn: sqlite3.Connection,
        source: sqlite3.Connection,
        watermark: int,
        window_start: int,
    ) -> tuple[int, int, int, int]:
        """Decode changed source rows into the mirror.

        Two kinds of row need reading: everything newer than ``watermark``, and
        anything in the recent window that may have changed under an existing
        ROWID.  The second kind is narrowed in SQL to rows that are actually
        edited or now empty, rather than re-reading the whole window: pulling
        2000 ``attributedBody`` blobs back through the decoder on every single
        search is real work on a large history, and almost all of it would find
        nothing changed.

        Rows already present are rewritten only when their text truly differs,
        so a warm refresh writes nothing.  A row that has lost its body — an
        unsend — is dropped, or its stale text would keep matching forever.
        """
        # What we already hold for the window, so changes can be detected.
        existing: dict[int, str] = dict(
            conn.execute(
                "SELECT message_id, folded FROM body WHERE message_id > ?",
                (window_start,),
            ).fetchall()
        )

        cursor = source.execute(
            """
            SELECT ROWID, text, attributedBody FROM message
            WHERE ROWID > ?
               OR (ROWID > ? AND (date_edited > 0
                                  OR (text IS NULL AND attributedBody IS NULL)))
            ORDER BY ROWID
            """,
            (watermark, window_start),
        )

        scanned = added = updated = removed = 0
        writes: list[tuple[int, str]] = []
        deletes: list[tuple[int]] = []

        def flush() -> None:
            if writes:
                conn.executemany(
                    "INSERT INTO body (message_id, folded) VALUES (?, ?) "
                    "ON CONFLICT(message_id) DO UPDATE SET folded = excluded.folded",
                    writes,
                )
                writes.clear()
            if deletes:
                conn.executemany("DELETE FROM body WHERE message_id = ?", deletes)
                deletes.clear()
            conn.commit()

        while True:
            rows = cursor.fetchmany(_BATCH)
            if not rows:
                break
            for rowid, text, blob in rows:
                scanned += 1
                body = message_text(text, blob)
                folded = body.casefold().strip() if body else ""
                prior = existing.get(rowid)

                if not folded:
                    # Nothing searchable: unsent, a bare attachment, or a blob
                    # we cannot decode. The watermark still advances past it.
                    if prior is not None:
                        deletes.append((rowid,))
                        removed += 1
                    continue
                if prior is None:
                    writes.append((rowid, folded))
                    added += 1
                elif prior != folded:
                    writes.append((rowid, folded))
                    updated += 1

            flush()
            if scanned % (_BATCH * 10) == 0:
                logger.info("Search index: %d rows scanned…", scanned)

        flush()
        return scanned, added, updated, removed
