"""
Inline message preview cards (MCP Apps, SEP-1865).

Chat hosts that implement the MCP Apps extension -- Claude Desktop and
claude.ai among them -- can render a tool's result with an HTML view the
server ships as a ``ui://`` resource. The host reads the resource once,
loads it in a sandboxed iframe next to the tool call in the transcript,
and forwards the tool's ``structuredContent`` to it over postMessage.

This is the Messages half of the same design the Apple Mail connector uses
for ``preview_email``: one self-contained document in ``ui/``, the same host
tokens and handshake, the same card chrome. Here a single document serves
the four read tools and picks a view from the tool name the host reports
(falling back to the shape of the structured result):

  list_chats         -> a conversation list, like the Messages sidebar
  get_chat_messages  -> an iMessage-style bubble transcript
  search_messages    -> hit rows across chats, or a transcript when scoped
                        to one chat
  get_message        -> one bubble with delivery details and attachments

Hosts without MCP Apps ignore the ``_meta.ui`` hint and see the text
content only, so every tool still degrades to the JSON it always returned.
"""

from __future__ import annotations

from base64 import b64encode
from pathlib import Path
from typing import Any

from mcp.server.apps import Apps

# Identifier hosts use to find the view for the tools; must match the tools'
# ``_meta.ui.resourceUri``. Stable across releases so cached templates stay
# valid.
PREVIEW_URI = "ui://apple-messages/message-preview"

# Nested form is the spec; the flat key is the pre-GA format some hosts still
# read (the SDK tells hosts to check both). ``Apps.tool`` owns the nested one;
# this is merged in beside it.
LEGACY_UI_META: dict[str, Any] = {"ui/resourceUri": PREVIEW_URI}

_UI_DIR = Path(__file__).parent / "ui"
_HTML_PATH = _UI_DIR / "message_preview.html"
_ICON_PATH = _UI_DIR / "icon.png"
_ICON_PLACEHOLDER = "__ICON_DATA_URI__"


def preview_html() -> str:
    """The card's HTML document, with the Messages icon inlined.

    The sandbox blocks outside fetches, so the icon rides along as a data
    URI rather than a file reference.
    """
    html = _HTML_PATH.read_text(encoding="utf-8")
    data_uri = ""
    if _ICON_PATH.exists():
        data_uri = "data:image/png;base64," + b64encode(_ICON_PATH.read_bytes()).decode("ascii")
    return html.replace(_ICON_PLACEHOLDER, data_uri)


def build_apps() -> Apps:
    """The Apps extension with the preview resource registered.

    Tools opt in with ``@apps.tool(resource_uri=PREVIEW_URI, meta=LEGACY_UI_META)``;
    the instance is then handed to ``MCPServer(extensions=[apps])``.
    """
    apps = Apps()
    apps.add_html_resource(
        PREVIEW_URI,
        preview_html(),
        name="message_preview",
        title="Message preview card",
        description=(
            "Interactive card that renders Messages results inline in the chat: "
            "a conversation list, a bubble transcript, search hits, or one "
            "message with its delivery details. Rendered by hosts that support "
            "MCP Apps."
        ),
        # No CSP block: the card needs no network access at all, so the host's
        # restrictive default is exactly right. The card draws its own border,
        # like the Mail card, so ask the host not to add another.
        prefers_border=False,
    )
    return apps
