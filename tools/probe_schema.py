#!/usr/bin/env python3
"""
Verify the real chat.db on this Mac. Run after granting Full Disk Access.

Answers the questions that cannot be settled from documentation alone:
  1. Does the schema on this macOS version match what db.py assumes?
  2. How often is message.text NULL (i.e. how load-bearing is the
     attributedBody decoder)?
  3. Does RCS actually appear in message.service?
  4. Are timestamps in seconds or nanoseconds?

Usage:  python3 tools/probe_schema.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from apple_messages_mcp.db import MessagesDB, MessagesDBError  # noqa: E402
from apple_messages_mcp.typedstream import decode_attributed_body  # noqa: E402

EXPECTED_TABLES = {
    "message",
    "chat",
    "handle",
    "attachment",
    "chat_message_join",
    "chat_handle_join",
    "message_attachment_join",
}

EXPECTED_MESSAGE_COLUMNS = {
    "ROWID", "guid", "text", "attributedBody", "date", "date_read",
    "date_delivered", "date_edited", "is_from_me", "is_read", "service",
    "handle_id", "cache_has_attachments", "associated_message_type",
    "thread_originator_guid",
}


def main() -> int:
    db = MessagesDB()
    try:
        conn = db.connect()
    except MessagesDBError as exc:
        print(f"FAILED\n\n{exc}")
        return 1

    print(f"Opened {db.path} ({db.path.stat().st_size / 1e6:.0f} MB)\n")

    # 1. Schema ---------------------------------------------------------
    tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing_tables = EXPECTED_TABLES - tables
    print(f"[1] Tables: {len(tables)} present")
    print(f"    missing expected: {missing_tables or 'none'}")

    cols = {r[1] for r in conn.execute("PRAGMA table_info(message)")}
    missing_cols = EXPECTED_MESSAGE_COLUMNS - cols
    print(f"    message columns: {len(cols)}")
    print(f"    missing expected: {missing_cols or 'none'}\n")

    # 2. attributedBody prevalence ---------------------------------------
    row = conn.execute(
        """
        SELECT COUNT(*) total,
               SUM(CASE WHEN text IS NULL OR text = '' THEN 1 ELSE 0 END) no_text,
               SUM(CASE WHEN attributedBody IS NOT NULL THEN 1 ELSE 0 END) has_blob,
               SUM(CASE WHEN (text IS NULL OR text = '')
                         AND attributedBody IS NOT NULL THEN 1 ELSE 0 END) blob_only
        FROM message
        """
    ).fetchone()
    total = row[0] or 1
    print(f"[2] Messages: {row[0]:,}")
    print(f"    text NULL/empty:      {row[1]:,} ({row[1] / total:.1%})")
    print(f"    attributedBody set:   {row[2]:,} ({row[2] / total:.1%})")
    print(f"    ONLY in blob:         {row[3]:,} ({row[3] / total:.1%})  <-- decoder load")

    # Decoder accuracy on blob-only rows.
    sample = conn.execute(
        """
        SELECT attributedBody FROM message
        WHERE (text IS NULL OR text = '') AND attributedBody IS NOT NULL
        ORDER BY date DESC LIMIT 300
        """
    ).fetchall()
    decoded = [decode_attributed_body(r[0]) for r in sample]
    ok = sum(1 for d in decoded if d)
    print(f"    decoder success:      {ok}/{len(sample)} sampled")
    for d in [x for x in decoded if x][:3]:
        preview = " ".join(d.split())[:70]
        print(f"      e.g. {preview!r}")
    print()

    # 3. Services --------------------------------------------------------
    print("[3] message.service breakdown:")
    for service, n in conn.execute(
        "SELECT service, COUNT(*) FROM message GROUP BY service ORDER BY 2 DESC"
    ):
        print(f"    {str(service):12} {n:,}")
    rcs = conn.execute(
        "SELECT COUNT(*) FROM message WHERE service LIKE '%RCS%'"
    ).fetchone()[0]
    print(f"    RCS rows: {rcs:,}{'  <-- RCS confirmed in data' if rcs else ''}\n")

    # 4. Timestamps ------------------------------------------------------
    newest = conn.execute("SELECT MAX(date) FROM message").fetchone()[0]
    print(f"[4] max(date) raw = {newest}")
    print(f"    interpreted    = {db.to_datetime(newest)}")
    print(f"    units          = {'nanoseconds' if newest > 100_000_000_000 else 'seconds'}")

    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
