"""
Apple Messages MCP Server
=========================
Read-only access to iMessage, SMS, and RCS conversations on macOS.

Architecture
------------
Unlike the Apple Mail extension, this one is a hybrid and cannot be pure
scripting.  Messages' AppleScript dictionary exposes ``account``, ``chat``,
``participant`` and ``file transfer`` — but **no message class** — so message
bodies are unreadable through scripting.  Therefore:

  * reading / searching  -> SQLite over ~/Library/Messages/chat.db (needs
    Full Disk Access, which macOS cannot prompt for programmatically)
  * contact names        -> Messages scripting (needs Automation permission,
    which macOS does prompt for)

Searching
---------
Message bodies usually live only in ``attributedBody``, a typedstream blob SQL
cannot look inside, so searching chat.db directly is either incomplete or very
slow.  Bodies are therefore decoded once into a local index (see ``index.py``)
and searched there.

Writing
-------
Messages has no draft object, so the write path comes in two levels:
``compose_message`` prefills a window and lets the user press send, while
``send_message`` delivers immediately.  Prefer the former.

Tools provided
--------------
  get_stats             - Totals, per-service breakdown, date range
  list_chats            - Conversations, most-recently-active first
  get_chat_messages     - Messages in one conversation, oldest-first, paginated
  search_messages       - Substring search with chat/sender/date filters
  get_message           - One message with attachments and delivery timestamps
  get_attachment        - Attachment bytes as base64
  refresh_search_index  - Warm or rebuild the search index
  compose_message       - Open Messages with text prefilled, without sending
  send_message          - Send to an existing conversation (irreversible)
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .applescript import ContactResolver
from .db import MessagesDB, MessagesDBError
from .models import (
    AttachmentData,
    ChatSummary,
    ComposeResult,
    MessageDetail,
    MessagesStats,
    MessageSummary,
    SearchIndexStatus,
    SearchResult,
    SendResult,
)
from .send import SendError, compose as compose_window, send_to_chat

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("apple_messages_mcp")

mcp = FastMCP("apple-messages")

# Lazily initialised: opening chat.db (and especially the contact scan) is slow
# enough that doing it at import time would stall the MCP initialize response.
_db: Optional[MessagesDB] = None
_contacts: Optional[ContactResolver] = None

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


def _get_db() -> MessagesDB:
    global _db
    if _db is None:
        _db = MessagesDB()
    return _db


def _get_contacts() -> ContactResolver:
    global _contacts
    if _contacts is None:
        _contacts = ContactResolver()
    return _contacts


def _summary(row: dict, resolver: ContactResolver) -> MessageSummary:
    return MessageSummary(**row, sender_name=resolver.name_for(row.get("sender")))


@mcp.tool()
def get_stats() -> MessagesStats:
    """Overview of the Messages database: message and chat totals, unread
    count, attachment count, a per-service breakdown (iMessage / SMS / RCS),
    and the date range covered."""
    try:
        return MessagesStats(**_get_db().stats())
    except MessagesDBError as exc:
        raise RuntimeError(str(exc)) from exc


@mcp.tool()
def list_chats(limit: int = 30, offset: int = 0) -> list[ChatSummary]:
    """List conversations, most recently active first.

    Args:
        limit: Maximum conversations to return (default 30).
        offset: Conversations to skip, for paging through the list.
    """
    try:
        rows = _get_db().list_chats(limit=limit, offset=offset)
    except MessagesDBError as exc:
        raise RuntimeError(str(exc)) from exc

    resolver = _get_contacts()
    return [
        ChatSummary(**row, contact_names=resolver.names_for(row["participants"]))
        for row in rows
    ]


@mcp.tool()
def get_chat_messages(
    chat_id: int, limit: int = 50, before_id: Optional[int] = None
) -> list[MessageSummary]:
    """Read messages from one conversation, returned oldest-first.

    Args:
        chat_id: Conversation id from `list_chats`.
        limit: Maximum messages to return (default 50).
        before_id: Return messages older than this message id, to page back
            through history.
    """
    try:
        rows = _get_db().chat_messages(chat_id, limit=limit, before_id=before_id)
    except MessagesDBError as exc:
        raise RuntimeError(str(exc)) from exc

    resolver = _get_contacts()
    return [_summary(row, resolver) for row in rows]


@mcp.tool()
def search_messages(
    query: str,
    limit: int = 30,
    chat_id: Optional[int] = None,
    from_me: Optional[bool] = None,
    after: Optional[datetime] = None,
    before: Optional[datetime] = None,
) -> SearchResult:
    """Search message bodies across every conversation, over all history.

    Backed by a local index of decoded message bodies, which is brought up to
    date automatically. The first search on a large history has to build that
    index and may take a while; later searches are fast.

    Args:
        query: Text to look for (case-insensitive substring match).
        limit: Maximum messages to return (default 30).
        chat_id: Restrict to one conversation from `list_chats`.
        from_me: True for only messages you sent, False for only received.
        after: Only messages at or after this time.
        before: Only messages at or before this time.
    """
    try:
        rows, truncated = _get_db().search(
            query,
            limit=limit,
            chat_id=chat_id,
            from_me=from_me,
            after=after,
            before=before,
        )
    except MessagesDBError as exc:
        raise RuntimeError(str(exc)) from exc

    resolver = _get_contacts()
    return SearchResult(
        query=query,
        total=len(rows),
        truncated=truncated,
        messages=[_summary(row, resolver) for row in rows],
    )


@mcp.tool()
def get_message(message_id: int) -> MessageDetail:
    """Read one message in full, including attachments and delivery times.

    Args:
        message_id: Message id from `search_messages` or `get_chat_messages`.
    """
    try:
        row = _get_db().get_message(message_id)
    except MessagesDBError as exc:
        raise RuntimeError(str(exc)) from exc

    if row is None:
        raise ValueError(f"No message with id {message_id}")
    return MessageDetail(**row, sender_name=_get_contacts().name_for(row.get("sender")))


@mcp.tool()
def get_attachment(attachment_id: int) -> AttachmentData:
    """Retrieve an attachment's bytes, base64-encoded.

    Args:
        attachment_id: Attachment id from `get_message`.
    """
    try:
        raw_path = _get_db().get_attachment_path(attachment_id)
    except MessagesDBError as exc:
        raise RuntimeError(str(exc)) from exc

    if not raw_path:
        raise ValueError(f"No attachment with id {attachment_id}")

    path = Path(os.path.expanduser(raw_path))
    if not path.exists():
        raise ValueError(
            f"Attachment file is missing from disk: {raw_path}. It may not have "
            "been downloaded from iCloud yet."
        )

    size = path.stat().st_size
    if size > MAX_ATTACHMENT_BYTES:
        raise ValueError(
            f"Attachment is {size / 1e6:.1f} MB, over the "
            f"{MAX_ATTACHMENT_BYTES / 1e6:.0f} MB limit."
        )

    try:
        data = path.read_bytes()
    except PermissionError as exc:
        raise RuntimeError(
            "Cannot read the attachment — Full Disk Access is required for "
            "~/Library/Messages/Attachments."
        ) from exc

    return AttachmentData(
        filename=path.name,
        mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        size=size,
        data_base64=base64.b64encode(data).decode("ascii"),
    )


@mcp.tool()
def refresh_search_index(rebuild: bool = False) -> SearchIndexStatus:
    """Update the local search index, and report on it.

    `search_messages` keeps this current on its own, so calling this is
    optional. It is useful for warming the index deliberately — the first
    build on a large history decodes every message and takes a while — and for
    forcing a full rebuild.

    Args:
        rebuild: Discard the index and decode every message again. Needed only
            to pick up edits to messages older than the recent-edit window.
    """
    db = _get_db()
    try:
        report = db.refresh_index(rebuild=rebuild)
        status = db.index_status()
    except MessagesDBError as exc:
        raise RuntimeError(str(exc)) from exc
    return SearchIndexStatus(**{**status, **report})


@mcp.tool()
def compose_message(
    handle: str, body: str = "", service: str = "imessage"
) -> ComposeResult:
    """Open Messages with a recipient and text prefilled, WITHOUT sending.

    This is the safe way to write a message: it puts the text in front of the
    user in Messages and stops, so they read it and press send themselves.
    Prefer it over `send_message`, and use it whenever the conversation does
    not exist yet — `send_message` can only reply to an existing one.

    Args:
        handle: Recipient phone number or email, e.g. "+15551234567".
        body: Text to prefill. May be empty to just open the conversation.
        service: "imessage" or "sms" — which transport Messages preselects.
    """
    try:
        result = compose_window(handle, body, service)
    except SendError as exc:
        raise RuntimeError(str(exc)) from exc
    return ComposeResult(**result.as_dict())


@mcp.tool()
def send_message(chat_id: int, body: str, confirm: bool = False) -> SendResult:
    """Send a message to an existing conversation. This delivers immediately.

    IRREVERSIBLE — the message goes out as soon as this runs, and scripting
    cannot unsend it. Confirm the exact recipient and wording with the user
    before calling, and pass confirm=True to acknowledge that. If they have
    not clearly asked for it to be sent, use `compose_message` instead and let
    them press send.

    Messages picks the transport (iMessage, SMS, or RCS) for the conversation
    itself, which is why this addresses a chat rather than a raw handle. To
    start a new conversation, use `compose_message`.

    Args:
        chat_id: Conversation id from `list_chats`.
        body: Text to send.
        confirm: Must be True. A guard against sending by accident.
    """
    if not confirm:
        raise ValueError(
            "send_message requires confirm=True. Show the user the exact "
            "recipient and message text and get their agreement first, or use "
            "compose_message to let them press send themselves."
        )

    db = _get_db()
    try:
        guid = db.chat_guid(chat_id)
    except MessagesDBError as exc:
        raise RuntimeError(str(exc)) from exc
    if not guid:
        raise ValueError(f"No conversation with id {chat_id}. Try list_chats.")

    try:
        result = send_to_chat(guid, body)
    except SendError as exc:
        raise RuntimeError(str(exc)) from exc

    return SendResult(
        sent=True,
        chat_id=chat_id,
        chat_guid=guid,
        body=result["body"],
        characters=result["characters"],
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
