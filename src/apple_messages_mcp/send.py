"""
The write path: composing and sending messages.

Two levels, deliberately
------------------------
Messages has no draft object, so there is no exact analogue of the Mail
extension's draft-first design.  The closest safe equivalent is a *prefilled
compose window*, so this module offers both levels and keeps them clearly
separate:

``compose``
    Opens Messages with the recipient and body already filled in, and stops.
    A human presses send.  Nothing leaves the machine without that action, so
    this is the safe default and the right tool for starting a new
    conversation.

``send_to_chat``
    Actually delivers, through the scripting interface's ``send`` command.
    Irreversible: Messages has no unsend-for-everyone via scripting.

Why sending targets a chat, never a raw handle
----------------------------------------------
``send`` accepts either a ``participant`` or a ``chat``.  Addressing an
existing chat by its GUID is the robust choice, because Messages then picks
the transport itself — iMessage, SMS or RCS — instead of the caller guessing
and silently sending an SMS to someone who is on iMessage, or failing outright
because a handle has no iMessage capability.  Starting a *new* conversation is
exactly the case where no chat exists yet, and that is what ``compose`` is
for.  So the split falls out of the API rather than being a limitation.

Why arguments are passed, not interpolated
------------------------------------------
Every script here is invoked as ``osascript -e 'on run argv' … -- body guid``,
so message text reaches AppleScript as an argument and is never spliced into
script source.  Building the script by string substitution would need exactly
correct quote and backslash escaping, and getting it wrong turns a message
body containing a double quote into either a syntax error or arbitrary
AppleScript.  Passing argv removes that class of bug entirely.
"""

from __future__ import annotations

import logging
import subprocess
import urllib.parse
from dataclasses import dataclass

logger = logging.getLogger("apple_messages_mcp.send")

_TIMEOUT_SECONDS = 30

# AppleScript error codes worth translating into something actionable.
_NOT_AUTHORIZED = "-1743"
_NOT_FOUND = "-1728"

_SEND_SCRIPT = (
    "on run argv",
    'set theBody to item 1 of argv',
    'set theGuid to item 2 of argv',
    'tell application "Messages"',
    "    set theChat to first chat whose id is theGuid",
    "    send theBody to theChat",
    "end tell",
    'return "ok"',
    "end run",
)


class SendError(RuntimeError):
    """Raised when a message could not be composed or sent."""


@dataclass
class ComposeResult:
    url: str
    handle: str
    service: str
    body: str

    def as_dict(self) -> dict:
        return {
            "opened": True,
            "url": self.url,
            "handle": self.handle,
            "service": self.service,
            "body": self.body,
            "note": (
                "Messages is open with this text prefilled. Nothing has been "
                "sent — press send in Messages to deliver it."
            ),
        }


def _run_osascript(script_lines: tuple[str, ...], *args: str) -> str:
    cmd: list[str] = ["osascript"]
    for line in script_lines:
        cmd += ["-e", line]
    cmd += list(args)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired as exc:
        raise SendError(
            f"Messages did not respond within {_TIMEOUT_SECONDS}s. It may be "
            "showing a dialog, or waiting on iCloud."
        ) from exc
    except OSError as exc:
        raise SendError(f"Could not run osascript: {exc}") from exc

    if result.returncode != 0:
        raise SendError(_explain(result.stderr.strip()))
    return result.stdout.strip()


def _explain(stderr: str) -> str:
    """Turn an AppleScript failure into something the user can act on."""
    if _NOT_AUTHORIZED in stderr:
        return (
            "Not authorized to control Messages. Open System Settings -> "
            "Privacy & Security -> Automation and enable Messages for this "
            "app, then try again.\n\n"
            f"osascript said: {stderr}"
        )
    if _NOT_FOUND in stderr:
        return (
            "Messages could not find that conversation. The chat GUID may be "
            "stale — call list_chats again to get a current one.\n\n"
            f"osascript said: {stderr}"
        )
    if "Application isn't running" in stderr or "-600" in stderr:
        return f"Messages is not running. Open Messages and try again.\n\n{stderr}"
    return f"Messages refused the request: {stderr}"


def compose(handle: str, body: str = "", service: str = "imessage") -> ComposeResult:
    """Open Messages with a recipient and body prefilled, without sending.

    ``service`` selects the URL scheme: ``imessage`` or ``sms``.  Both are
    registered by Messages.app; the scheme mainly influences which transport
    Messages preselects, and it will still fall back on its own if the
    recipient is not reachable that way.
    """
    handle = (handle or "").strip()
    if not handle:
        raise SendError("A recipient handle (phone number or email) is required.")

    scheme = service.strip().lower()
    if scheme not in ("imessage", "sms"):
        raise SendError(f"Unknown service {service!r} — use 'imessage' or 'sms'.")

    # Apple's convention joins the body with '&' rather than '?' here.
    url = f"{scheme}:{handle}"
    if body:
        url += "&body=" + urllib.parse.quote(body, safe="")

    try:
        result = subprocess.run(
            ["open", url], capture_output=True, text=True, timeout=_TIMEOUT_SECONDS
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise SendError(f"Could not open Messages: {exc}") from exc

    if result.returncode != 0:
        raise SendError(
            f"Could not open a compose window for {handle}: "
            f"{result.stderr.strip() or 'open failed'}"
        )

    logger.info("Opened compose window for %s via %s", handle, scheme)
    return ComposeResult(url=url, handle=handle, service=scheme, body=body)


def send_to_chat(chat_guid: str, body: str) -> dict:
    """Send ``body`` to an existing conversation, identified by its chat GUID.

    Irreversible.  Messages chooses the transport (iMessage / SMS / RCS) for
    the chat itself.
    """
    chat_guid = (chat_guid or "").strip()
    if not chat_guid:
        raise SendError("A chat GUID is required.")
    if not body or not body.strip():
        raise SendError("Refusing to send an empty message.")

    _run_osascript(_SEND_SCRIPT, body, chat_guid)
    logger.info("Sent %d chars to chat %s", len(body), chat_guid)
    return {
        "sent": True,
        "chat_guid": chat_guid,
        "body": body,
        "characters": len(body),
    }


def build_compose_url(handle: str, body: str = "", service: str = "imessage") -> str:
    """URL that :func:`compose` would open. Split out so it can be tested."""
    scheme = service.strip().lower()
    url = f"{scheme}:{handle.strip()}"
    if body:
        url += "&body=" + urllib.parse.quote(body, safe="")
    return url


def send_script_argv(chat_guid: str, body: str) -> list[str]:
    """The exact osascript argv :func:`send_to_chat` would run.

    Exposed for tests, so the command can be asserted on without a live send.
    """
    cmd: list[str] = ["osascript"]
    for line in _SEND_SCRIPT:
        cmd += ["-e", line]
    return cmd + [body, chat_guid]
