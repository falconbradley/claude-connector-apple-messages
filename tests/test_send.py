"""
Exercise send.py without sending anything.

Everything here is either a guard clause, a pure string builder, or an
assertion about the argv that *would* be handed to osascript.  No test in this
file delivers a message or opens a window, so it is safe to run anywhere —
which also means the one thing it cannot cover is whether Apple's ``send``
command actually works on this OS build.  That needs a live test by hand; see
the README.

Run:  python3 tests/test_send.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from apple_messages_mcp.send import (  # noqa: E402
    SendError,
    _explain,
    build_compose_url,
    compose,
    send_script_argv,
    send_to_chat,
)

CHECKS: list[tuple[str, bool]] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    CHECKS.append((label, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'}  {label}" + (f"  -> {detail}" if detail else ""))


def raises(label: str, fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except SendError as exc:
        check(label, True, str(exc).split("\n")[0][:60])
    except Exception as exc:  # wrong exception type is still a failure
        check(label, False, f"raised {type(exc).__name__}")
    else:
        check(label, False, "no exception")


def main() -> int:
    print("== compose URLs ==")
    check(
        "imessage scheme",
        build_compose_url("+15551234567", "hi") == "imessage:+15551234567&body=hi",
        build_compose_url("+15551234567", "hi"),
    )
    check(
        "sms scheme",
        build_compose_url("+15551234567", "hi", "sms") == "sms:+15551234567&body=hi",
        build_compose_url("+15551234567", "hi", "sms"),
    )
    check(
        "no body means no query",
        build_compose_url("+15551234567") == "imessage:+15551234567",
        build_compose_url("+15551234567"),
    )
    spaced = build_compose_url("+15551234567", "see you at 5 & bring cash")
    check("spaces and ampersands encoded",
          "%20" in spaced and "%26" in spaced and spaced.count("&") == 1, spaced)
    unicode_url = build_compose_url("+15551234567", "café ☕")
    check("non-ASCII percent-encoded", "%C3%A9" in unicode_url, unicode_url)
    check("email handle survives",
          build_compose_url("friend@icloud.com", "yo").startswith("imessage:friend@icloud.com"),
          build_compose_url("friend@icloud.com", "yo"))

    print("\n== compose guards ==")
    raises("empty handle rejected", compose, "", "hi")
    raises("whitespace handle rejected", compose, "   ", "hi")
    raises("unknown service rejected", compose, "+15551234567", "hi", "telepathy")

    print("\n== send guards ==")
    # These must all fail before osascript is ever invoked.
    raises("empty guid rejected", send_to_chat, "", "hello")
    raises("empty body rejected", send_to_chat, "iMessage;-;+15551234567", "")
    raises("whitespace body rejected", send_to_chat, "iMessage;-;+15551234567", "   ")

    print("\n== argv safety ==")
    # The whole point of passing argv: a body that looks like AppleScript must
    # never reach the interpreter as source. If this regresses, a message
    # containing a quote becomes either a syntax error or arbitrary code.
    hostile = '" & (do shell script "echo pwned") & "'
    argv = send_script_argv("iMessage;-;+15551234567", hostile)
    statements = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    check("body is not spliced into any script statement",
          not any(hostile in s for s in statements), f"{len(statements)} statements")
    check("body is the final-but-one argv element", argv[-2] == hostile)
    check("guid is the last argv element", argv[-1] == "iMessage;-;+15551234567")
    check("script reads from argv", any("item 1 of argv" in s for s in statements))
    check("osascript is the program", argv[0] == "osascript")

    quoted = send_script_argv("guid", 'she said "hi" \\ then left')
    check("quotes and backslashes pass through untouched",
          quoted[-2] == 'she said "hi" \\ then left', quoted[-2])

    newlines = send_script_argv("guid", "line one\nline two")
    check("newlines preserved in body", newlines[-2] == "line one\nline two")

    print("\n== error translation ==")
    check("automation denial explained",
          "Automation" in _explain("execution error: Not authorized (-1743)"))
    check("stale chat explained",
          "list_chats" in _explain("execution error: Can't get chat (-1728)"))
    check("app not running explained",
          "not running" in _explain("execution error: Application isn't running (-600)"))
    check("unknown error still surfaced",
          "wat" in _explain("execution error: wat (-9999)"))

    failed = [label for label, ok in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("FAILED: " + "; ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
