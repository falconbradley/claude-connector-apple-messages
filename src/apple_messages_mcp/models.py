"""Pydantic models for Apple Messages MCP server."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class ChatSummary(BaseModel):
    id: int                          # chat.ROWID — stable handle for other tools
    guid: str                        # e.g. "iMessage;-;+15551234567"
    identifier: str                  # phone number, email, or "chat<digits>"
    display_name: Optional[str] = None   # group chat name, if the user set one
    service: Optional[str] = None    # "iMessage" | "SMS" | "RCS"
    is_group: bool = False
    participants: list[str] = []     # handle strings (numbers / emails)
    contact_names: list[str] = []    # Contacts-resolved names, when available
    message_count: int = 0
    last_message_date: Optional[datetime] = None
    last_message_preview: Optional[str] = None


class MessageSummary(BaseModel):
    id: int                          # message.ROWID
    guid: str
    chat_id: Optional[int] = None
    chat_name: Optional[str] = None
    text: Optional[str] = None
    date: Optional[datetime] = None
    is_from_me: bool = False
    sender: Optional[str] = None     # handle of the sender, None when from me
    sender_name: Optional[str] = None
    service: Optional[str] = None    # "iMessage" | "SMS" | "RCS"
    has_attachments: bool = False
    is_read: bool = False
    is_edited: bool = False
    is_unsent: bool = False
    reply_to_guid: Optional[str] = None   # thread_originator_guid
    tapback: Optional[str] = None    # e.g. "loved", "liked" — set for reactions


class MessageDetail(MessageSummary):
    attachments: list["AttachmentInfo"] = []
    date_read: Optional[datetime] = None
    date_delivered: Optional[datetime] = None


class AttachmentInfo(BaseModel):
    id: int                          # attachment.ROWID
    message_id: int
    filename: Optional[str] = None   # on-disk path
    transfer_name: Optional[str] = None  # original name as sent
    mime_type: Optional[str] = None
    size: int = 0
    is_sticker: bool = False


class AttachmentData(BaseModel):
    filename: str
    mime_type: str
    size: int
    data_base64: str


class SearchResult(BaseModel):
    query: str
    total: int                       # messages returned (not total in database)
    truncated: bool = False          # more matches existed beyond the limit
    messages: list[MessageSummary] = []


class ComposeResult(BaseModel):
    """A compose window was opened; nothing was sent."""

    opened: bool = True
    sent: bool = False               # always False — a human presses send
    handle: str
    service: str                     # "imessage" | "sms"
    body: str = ""
    url: str                         # the URL scheme link that was opened
    note: str = ""


class SendResult(BaseModel):
    """A message was actually delivered."""

    sent: bool = True
    chat_id: int
    chat_guid: str
    chat_name: Optional[str] = None
    body: str
    characters: int = 0


class SearchIndexStatus(BaseModel):
    """State of the local search mirror (see index.py)."""

    built: bool = False
    indexed_messages: int = 0
    watermark: int = 0               # highest message ROWID indexed
    built_at: Optional[str] = None
    index_bytes: int = 0
    path: str = ""
    # Populated only by refresh_search_index, not by a plain status read.
    scanned: Optional[int] = None
    added: Optional[int] = None      # message ids not previously indexed
    updated: Optional[int] = None    # bodies that had changed (an edit)
    removed: Optional[int] = None    # bodies that went away (an unsend)
    rebuilt: Optional[bool] = None
    seconds: Optional[float] = None


class MessagesStats(BaseModel):
    total_messages: int
    total_chats: int
    group_chats: int
    unread_messages: int
    attachments: int
    by_service: dict[str, int] = {}      # {"iMessage": n, "SMS": n, "RCS": n}
    oldest_message: Optional[datetime] = None
    newest_message: Optional[datetime] = None
    database_bytes: int = 0


MessageDetail.model_rebuild()
