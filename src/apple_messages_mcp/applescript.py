"""
Contact-name resolution via Messages' scripting interface.

chat.db stores only raw handles (``+15551234567``, ``someone@icloud.com``).  The
Contacts database is itself TCC-protected, but Messages' ``participant`` class
exposes ``full name`` straight from the user's Contacts card — so we can label
handles with real names using Automation permission alone.

This is best-effort: if Messages is not running or Automation is denied, every
lookup simply returns ``None`` and the caller falls back to the raw handle.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Optional

logger = logging.getLogger("apple_messages_mcp.applescript")

# Enumerating participants across every chat takes a few seconds on a large
# history, so the map is built once and reused for the process lifetime.
_SCRIPT = """
tell application "Messages"
    set output to ""
    repeat with c in chats
        try
            repeat with p in participants of c
                try
                    set theName to full name of p
                on error
                    set theName to name of p
                end try
                set output to output & (handle of p) & tab & theName & linefeed
            end repeat
        end try
    end repeat
    return output
end tell
"""

_TIMEOUT_SECONDS = 30


class ContactResolver:
    """Lazily-built handle -> display-name map."""

    def __init__(self) -> None:
        self._map: Optional[dict[str, str]] = None

    def _build(self) -> dict[str, str]:
        try:
            result = subprocess.run(
                ["osascript", "-e", _SCRIPT],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("Contact resolution unavailable: %s", exc)
            return {}

        if result.returncode != 0:
            logger.warning("Contact resolution failed: %s", result.stderr.strip())
            return {}

        mapping: dict[str, str] = {}
        for line in result.stdout.splitlines():
            handle, _, name = line.partition("\t")
            handle, name = handle.strip(), name.strip()
            # Messages returns the handle itself when there is no Contacts card.
            if handle and name and name != handle:
                mapping[_normalize(handle)] = name
        logger.info("Resolved %d contact names", len(mapping))
        return mapping

    def name_for(self, handle: Optional[str]) -> Optional[str]:
        if not handle:
            return None
        if self._map is None:
            self._map = self._build()
        return self._map.get(_normalize(handle))

    def names_for(self, handles: list[str]) -> list[str]:
        return [self.name_for(h) or h for h in handles]


def _normalize(handle: str) -> str:
    """Normalize a handle so ``+1 (555) 123-4567`` matches ``+15551234567``."""
    handle = handle.strip().casefold()
    if "@" in handle:
        return handle
    digits = "".join(ch for ch in handle if ch.isdigit())
    # Compare on the last 10 digits so country-code differences don't split a
    # contact into two entries.
    return digits[-10:] if len(digits) >= 10 else digits or handle
