"""
Exercise db.py against a synthetic chat.db built with the real schema.

Lets the SQL be validated without Full Disk Access or a real message history.
Run:  python3 tests/test_db.py
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from apple_messages_mcp.db import APPLE_EPOCH_OFFSET, MessagesDB  # noqa: E402
from apple_messages_mcp.index import SearchIndex  # noqa: E402

SCHEMA = """
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT, service TEXT);
CREATE TABLE chat (
    ROWID INTEGER PRIMARY KEY, guid TEXT, chat_identifier TEXT,
    display_name TEXT, service_name TEXT, style INTEGER
);
CREATE TABLE message (
    ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, attributedBody BLOB,
    date INTEGER, date_read INTEGER, date_delivered INTEGER, date_edited INTEGER,
    is_from_me INTEGER, is_read INTEGER, service TEXT, handle_id INTEGER,
    cache_has_attachments INTEGER, associated_message_type INTEGER,
    thread_originator_guid TEXT
);
CREATE TABLE attachment (
    ROWID INTEGER PRIMARY KEY, filename TEXT, transfer_name TEXT,
    mime_type TEXT, total_bytes INTEGER, is_sticker INTEGER
);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
"""


def apple_ns(when: datetime) -> int:
    return int((when.timestamp() - APPLE_EPOCH_OFFSET) * 1e9)


def typedstream_blob(text: str) -> bytes:
    """Mimic the archive layout Messages writes into attributedBody."""
    raw = text.encode()
    if len(raw) < 0x81:
        length = bytes([len(raw)])
    else:
        length = b"\x81" + len(raw).to_bytes(2, "little")
    return (
        b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84"
        b"NSString\x01\x94\x84\x01+" + length + raw + b"\x86\x84\x02iI"
    )


def build(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)

    conn.execute("INSERT INTO handle VALUES (1, '+15551234567', 'iMessage')")
    conn.execute("INSERT INTO handle VALUES (2, '+15559876543', 'RCS')")
    conn.execute("INSERT INTO handle VALUES (3, 'friend@icloud.com', 'iMessage')")

    conn.execute("INSERT INTO chat VALUES (1, 'iMessage;-;+15551234567', '+15551234567', NULL, 'iMessage', 45)")
    conn.execute("INSERT INTO chat VALUES (2, 'iMessage;+;chat999', 'chat999', 'Dinner Crew', 'iMessage', 43)")
    conn.execute("INSERT INTO chat VALUES (3, 'RCS;-;+15559876543', '+15559876543', NULL, 'RCS', 45)")

    for chat_id, handle_id in [(1, 1), (2, 1), (2, 3), (3, 2)]:
        conn.execute("INSERT INTO chat_handle_join VALUES (?, ?)", (chat_id, handle_id))

    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 8, 5, 9, 30, tzinfo=timezone.utc)
    t2 = datetime(2026, 8, 10, 18, 45, tzinfo=timezone.utc)

    rows = [
        # plain text column populated
        (1, "g1", "Are we still on for dinner?", None, apple_ns(t0), apple_ns(t0),
         apple_ns(t0), None, 0, 1, "iMessage", 1, 0, 0, None),
        # body ONLY in attributedBody — the Ventura+ case
        (2, "g2", None, typedstream_blob("Yes! Booking the table now 🍝"),
         apple_ns(t1), None, None, None, 1, 1, "iMessage", None, 0, 0, None),
        # tapback (reaction), threaded reply
        (3, "g3", "Loved “Are we still on”", None, apple_ns(t1), None,
         None, None, 1, 1, "iMessage", None, 0, 2000, "g1"),
        # RCS message with an attachment, unread, edited
        (4, "g4", "Here's the menu", None, apple_ns(t2), None, None,
         apple_ns(t2), 0, 0, "RCS", 2, 1, 0, None),
        # group chat message
        (5, "g5", "dinner sounds great", None, apple_ns(t2), None, None,
         None, 0, 1, "iMessage", 3, 0, 0, None),
        # unsent message: no text, no blob
        (6, "g6", None, None, apple_ns(t2), None, None, None, 0, 1,
         "iMessage", 1, 0, 0, None),
    ]
    conn.executemany(
        "INSERT INTO message VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )

    for chat_id, msg_id in [(1, 1), (1, 2), (1, 3), (3, 4), (2, 5), (1, 6)]:
        conn.execute("INSERT INTO chat_message_join VALUES (?, ?)", (chat_id, msg_id))

    conn.execute(
        "INSERT INTO attachment VALUES (1, '~/Library/Messages/Attachments/ab/menu.pdf',"
        " 'menu.pdf', 'application/pdf', 51200, 0)"
    )
    conn.execute("INSERT INTO message_attachment_join VALUES (4, 1)")
    conn.commit()
    conn.close()


def build_deep(path: Path, filler: int = 3000) -> None:
    """A history with one old needle buried under many newer messages.

    Shaped like a real library in the way that matters: the filler rows carry a
    populated ``attributedBody``, which is the norm since Ventura and is what
    made the old search predicate match essentially every row.
    """
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO handle VALUES (1, '+15551234567', 'iMessage')")
    conn.execute(
        "INSERT INTO chat VALUES (1, 'iMessage;-;+15551234567', "
        "'+15551234567', NULL, 'iMessage', 45)"
    )
    conn.execute("INSERT INTO chat_handle_join VALUES (1, 1)")

    day = 86_400 * 1_000_000_000
    rows = [
        # ROWID 1: the oldest message, and the only "dentist" / "café" match.
        (1, "old", "Dentist appointment at the café Monday", None, 100 * day,
         None, None, None, 0, 1, "iMessage", 1, 0, 0, None),
    ]
    for i in range(2, filler + 2):
        rows.append(
            (i, f"f{i}", None, typedstream_blob(f"filler {i - 1}"),
             (100 + i) * day, None, None, None, 0, 1, "iMessage", 1, 0, 0, None)
        )
    conn.executemany(
        "INSERT INTO message VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    conn.executemany(
        "INSERT INTO chat_message_join VALUES (1, ?)",
        [(r[0],) for r in rows],
    )
    conn.commit()
    conn.close()


CHECKS: list[tuple[str, bool]] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    CHECKS.append((label, condition))
    print(f"{'PASS' if condition else 'FAIL'}  {label}" + (f"  -> {detail}" if detail else ""))


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "chat.db"
        build(path)
        # Always inject a throwaway index: the default lives in the user's real
        # ~/Library/Caches, and a test run must not touch it.
        db = MessagesDB(path, index=SearchIndex(Path(tmp) / "index.db"))

        print("== stats ==")
        s = db.stats()
        check("counts messages", s["total_messages"] == 6, str(s["total_messages"]))
        check("counts chats", s["total_chats"] == 3)
        check("detects group chat", s["group_chats"] == 1)
        check("counts unread", s["unread_messages"] == 1, str(s["unread_messages"]))
        check("RCS in service breakdown", s["by_service"].get("RCS") == 1, str(s["by_service"]))
        check("date range parsed", s["newest_message"] is not None, str(s["newest_message"]))

        print("\n== list_chats ==")
        chats = db.list_chats()
        check("returns all chats", len(chats) == 3)
        check("orders by recency", chats[0]["id"] in (2, 3), f"first={chats[0]['identifier']}")
        group = next(c for c in chats if c["id"] == 2)
        check("group flagged + named", group["is_group"] and group["display_name"] == "Dinner Crew")
        check("participants joined", sorted(group["participants"]) == ["+15551234567", "friend@icloud.com"],
              str(group["participants"]))
        direct = next(c for c in chats if c["id"] == 1)
        check("preview decoded from blob", direct["last_message_preview"] is not None,
              repr(direct["last_message_preview"]))

        print("\n== chat_messages ==")
        msgs = db.chat_messages(1)
        check("returns chat messages", len(msgs) == 4, str(len(msgs)))
        check("oldest first", msgs[0]["id"] == 1)
        blob_msg = next(m for m in msgs if m["id"] == 2)
        check("attributedBody decoded", blob_msg["text"] == "Yes! Booking the table now 🍝",
              repr(blob_msg["text"]))
        check("is_from_me honored", blob_msg["is_from_me"] is True)
        check("sender null when from me", blob_msg["sender"] is None)
        tapback = next(m for m in msgs if m["id"] == 3)
        check("tapback classified", tapback["tapback"] == "loved", str(tapback["tapback"]))
        check("reply threading kept", tapback["reply_to_guid"] == "g1")
        unsent = next(m for m in msgs if m["id"] == 6)
        check("unsent detected", unsent["is_unsent"] is True)

        print("\n== search ==")
        hits, truncated = db.search("dinner")
        check("finds across chats", len(hits) == 2, f"{[h['id'] for h in hits]}")
        check("not truncated", truncated is False)
        check("case-insensitive", any(h["id"] == 5 for h in hits))
        blob_hits, _ = db.search("Booking the table")
        check("searches inside attributedBody", len(blob_hits) == 1 and blob_hits[0]["id"] == 2,
              f"{[h['id'] for h in blob_hits]}")
        scoped, _ = db.search("dinner", chat_id=2)
        check("chat_id filter", len(scoped) == 1 and scoped[0]["id"] == 5)
        mine, _ = db.search("table", from_me=True)
        check("from_me filter", len(mine) == 1 and mine[0]["id"] == 2)
        ranged, _ = db.search("dinner", after=datetime(2026, 8, 8, tzinfo=timezone.utc))
        check("date filter", len(ranged) == 1 and ranged[0]["id"] == 5, str([h["id"] for h in ranged]))
        wild, _ = db.search("100%")
        check("LIKE wildcards escaped", wild == [])

        print("\n== search index ==")
        status = db.index_status()
        check("index built by first search", status["built"] is True)
        # Message 6 has neither text nor blob, so there is nothing to index.
        check("indexes only messages with a body", status["indexed_messages"] == 5,
              str(status["indexed_messages"]))
        check("watermark tracks max ROWID", status["watermark"] == 6, str(status["watermark"]))
        before = db.index_status()["indexed_messages"]
        report = db.refresh_index()
        check("re-refresh writes nothing",
              report["added"] == 0 and report["updated"] == 0
              and report["removed"] == 0 and not report["rebuilt"],
              str(report))
        check("count stable after no-op", db.index_status()["indexed_messages"] == before)
        rebuilt = db.refresh_index(rebuild=True)
        check("rebuild re-indexes everything", rebuilt["rebuilt"] and rebuilt["added"] == 5,
              str(rebuilt))

        # An edit reuses the message's ROWID, so the watermark alone would miss
        # it; the re-index window is what catches it. Messages stamps
        # date_edited on a real edit, and the index relies on that to find
        # changed rows without re-decoding the whole window on every search.
        edit = sqlite3.connect(path)
        edit.execute(
            "UPDATE message SET text = 'Are we still on for brunch?', "
            "date_edited = ? WHERE ROWID = 1",
            (apple_ns(datetime(2026, 8, 11, 9, 0, tzinfo=timezone.utc)),),
        )
        edit.commit()
        edit.close()
        db.close()
        db = MessagesDB(path, index=SearchIndex(Path(tmp) / "index.db"))
        edited = db.refresh_index()
        check("edit detected", edited["updated"] == 1 and edited["added"] == 0, str(edited))
        check("edited text searchable", len(db.search("brunch")[0]) == 1)
        check("pre-edit text gone", db.search("still on for dinner")[0] == [])

        # An unsend clears both columns; the stale body must not keep matching.
        unsend = sqlite3.connect(path)
        unsend.execute("UPDATE message SET text = NULL, attributedBody = NULL WHERE ROWID = 1")
        unsend.commit()
        unsend.close()
        db.close()
        db = MessagesDB(path, index=SearchIndex(Path(tmp) / "index.db"))
        gone = db.refresh_index()
        check("unsend removes the row", gone["removed"] == 1, str(gone))
        check("unsent body no longer matches", db.search("brunch")[0] == [])

        print("\n== search: full history (regression) ==")
        # The bug this guards against: the old implementation's SQL predicate
        # was `text LIKE ? OR attributedBody IS NOT NULL`, true for nearly
        # every modern row, so ORDER BY date DESC + LIMIT truncated the scan to
        # the newest few hundred messages and older matches vanished. Anything
        # that reintroduces a pre-filter LIMIT will fail here.
        deep = Path(tmp) / "deep.db"
        build_deep(deep, filler=3000)
        deep_db = MessagesDB(deep, index=SearchIndex(Path(tmp) / "deep-index.db"))
        old_hits, _ = deep_db.search("dentist")
        check("finds a match buried under 3000 newer messages",
              len(old_hits) == 1 and old_hits[0]["id"] == 1,
              f"{[h['id'] for h in old_hits]}")
        check("still finds recent matches", len(deep_db.search("filler 3000")[0]) == 1)
        capped, truncated_deep = deep_db.search("filler", limit=25)
        check("limit respected on a common term", len(capped) == 25, str(len(capped)))
        check("truncated flag set when more exist", truncated_deep is True)
        check("newest-first ordering", [h["id"] for h in capped] == sorted(
            (h["id"] for h in capped), reverse=True))
        folded, _ = deep_db.search("DENTIST")
        check("case-insensitive over the index", len(folded) == 1)
        accented, _ = deep_db.search("CAFÉ")
        check("case-insensitive for non-ASCII too", len(accented) == 1,
              f"{[h['id'] for h in accented]}")
        try:
            deep_db.search("   ")
            check("empty query rejected", False)
        except Exception:
            check("empty query rejected", True)
        deep_db.close()

        print("\n== get_message ==")
        detail = db.get_message(4)
        check("loads message", detail is not None and detail["text"] == "Here's the menu")
        check("RCS service surfaced", detail["service"] == "RCS")
        check("edited flagged", detail["is_edited"] is True)
        check("attachment joined", len(detail["attachments"]) == 1)
        check("attachment metadata", detail["attachments"][0]["transfer_name"] == "menu.pdf",
              str(detail["attachments"][0]))
        check("missing id returns None", db.get_message(9999) is None)

        print("\n== timestamps ==")
        check("nanoseconds decoded", str(db.to_datetime(apple_ns(t_ref := datetime(2026, 8, 10, 18, 45, tzinfo=timezone.utc)))).startswith("2026-08-10 18:45"),
              str(db.to_datetime(apple_ns(t_ref))))
        legacy = int(datetime(2015, 6, 1, tzinfo=timezone.utc).timestamp() - APPLE_EPOCH_OFFSET)
        check("legacy seconds decoded", str(db.to_datetime(legacy)).startswith("2015-06-01"),
              str(db.to_datetime(legacy)))
        check("null timestamp safe", db.to_datetime(None) is None)

        db.close()

    failed = [label for label, ok in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("FAILED: " + "; ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
