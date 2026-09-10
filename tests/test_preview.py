"""
Exercise the MCP Apps preview wiring end to end, in-process.

Drives the real server over the SDK's in-memory transport against a synthetic
chat.db, and checks the three things a host needs to render a card:

  * the read tools advertise ``_meta.ui.resourceUri``
  * that URI reads back as an ``text/html;profile=mcp-app`` document
  * each tool's ``structuredContent`` has the shape the document dispatches on

Nothing here touches the real chat.db, the real search index, or Messages.app.
Run:  python3 tests/test_preview.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[0] / "src"))
sys.path.insert(0, str(HERE))

from mcp.client import Client  # noqa: E402

import apple_messages_mcp.server as server  # noqa: E402
from apple_messages_mcp.db import MessagesDB  # noqa: E402
from apple_messages_mcp.index import SearchIndex  # noqa: E402
from apple_messages_mcp.preview import LEGACY_UI_META, PREVIEW_URI, preview_html  # noqa: E402
from test_db import build  # noqa: E402

PREVIEW_TOOLS = {"list_chats", "get_chat_messages", "search_messages", "get_message"}
PLAIN_TOOLS = {"get_stats", "get_attachment", "refresh_search_index", "compose_message", "send_message"}


class StubContacts:
    """Stands in for ContactResolver so the test never scripts Messages.app."""

    NAMES = {"+15551234567": "Alex Rivera", "friend@icloud.com": "Sam"}

    def name_for(self, handle: Optional[str]) -> Optional[str]:
        return self.NAMES.get(handle or "")

    def names_for(self, handles: list[str]) -> list[str]:
        return [n for n in (self.name_for(h) for h in handles) if n]


CHECKS: list[tuple[str, bool]] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    CHECKS.append((label, condition))
    print(f"{'PASS' if condition else 'FAIL'}  {label}" + (f"  -> {detail}" if detail else ""))


def tool_meta(tool) -> dict:
    return tool.meta or {}


async def run(tmp: Path) -> None:
    path = tmp / "chat.db"
    build(path)
    server._db = MessagesDB(path, index=SearchIndex(tmp / "index.db"))
    server._contacts = StubContacts()  # type: ignore[assignment]

    async with Client(server.mcp) as client:
        print("== tools/list ==")
        tools = {t.name: t for t in (await client.list_tools()).tools}
        check("all nine tools registered", set(tools) == PREVIEW_TOOLS | PLAIN_TOOLS, str(sorted(tools)))
        for name in sorted(PREVIEW_TOOLS):
            meta = tool_meta(tools[name])
            ui = meta.get("ui") or {}
            check(f"{name} carries the preview resource", ui.get("resourceUri") == PREVIEW_URI, str(ui))
            check(f"{name} also advertises the legacy flat key", meta.get("ui/resourceUri") == PREVIEW_URI)
        for name in sorted(PLAIN_TOOLS):
            check(f"{name} has no UI binding", "ui" not in tool_meta(tools[name]))
        check("docstring survives extension registration",
              (tools["search_messages"].description or "").startswith("Search message bodies"),
              (tools["search_messages"].description or "")[:60])
        check("search_messages still declares its output schema",
              bool(tools["search_messages"].output_schema))

        print("\n== resources ==")
        resources = {str(r.uri): r for r in (await client.list_resources()).resources}
        check("preview resource is listed", PREVIEW_URI in resources, str(sorted(resources)))
        res_meta = (resources[PREVIEW_URI].meta or {}).get("ui") or {}
        check("card draws its own border, like the Mail card", res_meta.get("prefersBorder") is False, str(res_meta))
        read = await client.read_resource(PREVIEW_URI)
        content = read.contents[0]
        check("served as an MCP App", content.mime_type == "text/html;profile=mcp-app", str(content.mime_type))
        html = content.text
        check("document speaks the Apps handshake", "ui/initialize" in html and "ui/notifications/initialized" in html)
        check("document uses the same protocol version as the Mail card", 'PROTOCOL_VERSION = "2026-01-26"' in html)
        check("document listens for tool results", "ui/notifications/tool-result" in html)
        check("icon was inlined", "data:image/png;base64," in html and "__ICON_DATA_URI__" not in html)
        check("no external references", "http://" not in html and "https://" not in html)
        check("read matches preview_html()", html == preview_html())

        print("\n== structured results the card dispatches on ==")
        chats = await client.call_tool("list_chats", {})
        sc = chats.structured_content or {}
        rows = sc.get("result", sc)
        check("list_chats -> list of chats", isinstance(rows, list) and len(rows) == 3, str(type(rows)))
        check("chat rows carry participants (the shape hint)", all("participants" in r for r in rows))
        check("contact names resolved", any("Alex Rivera" in r.get("contact_names", []) for r in rows))

        transcript = await client.call_tool("get_chat_messages", {"chat_id": 1})
        sc = transcript.structured_content or {}
        rows = sc.get("result", sc)
        check("get_chat_messages -> list of messages", isinstance(rows, list) and len(rows) == 4, str(len(rows) if isinstance(rows, list) else rows))
        check("messages carry chat_name for the card title", all("chat_name" in r for r in rows))
        check("tapback surfaced", any(r.get("tapback") == "loved" for r in rows))
        check("unsent surfaced", any(r.get("is_unsent") for r in rows))

        search = await client.call_tool("search_messages", {"query": "dinner"})
        sc = search.structured_content or {}
        check("search_messages -> query + messages", sc.get("query") == "dinner" and isinstance(sc.get("messages"), list), str(sorted(sc)))
        check("search finds both dinner messages", len(sc["messages"]) == 2, str([m.get("text") for m in sc["messages"]]))
        check("search hit names its sender", any(m.get("sender_name") == "Alex Rivera" for m in sc["messages"]))

        detail = await client.call_tool("get_message", {"message_id": 4})
        sc = detail.structured_content or {}
        check("get_message -> attachments key (the shape hint)", isinstance(sc.get("attachments"), list), str(sorted(sc)))
        check("attachment listed", sc["attachments"] and sc["attachments"][0].get("transfer_name") == "menu.pdf")
        check("text content still present for non-Apps hosts", any(c.type == "text" for c in detail.content))

        print("\n== errors still surface as errors ==")
        missing = await client.call_tool("get_message", {"message_id": 999})
        check("unknown message is an error result", missing.is_error is True)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(run(Path(tmp)))
    failed = [label for label, ok in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    for label in failed:
        print(f"FAILED: {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
