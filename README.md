# Apple Messages MCP

Read and search your iMessage, SMS, and RCS conversations from Claude, on macOS.

Companion to
[claude-connector-apple-mail](https://github.com/falconbradley/claude-connector-apple-mail)
and
[claude-connector-apple-reminders](https://github.com/falconbradley/claude-connector-apple-reminders).

**Status: reading and searching are solid. Sending works but is unproven on a
live send** — the scripting call is implemented and its syntax verified, but
Apple has broken `send` before, so treat the first real send as a test. See
[Sending messages](#sending-messages).

## Tools

| Tool | Description |
| --- | --- |
| `get_stats` | Totals, unread count, per-service breakdown (iMessage/SMS/RCS), date range |
| `list_chats` | Conversations, most recently active first, with participants and a preview. Renders as a card |
| `get_chat_messages` | Messages in one conversation, oldest-first, paged. Renders as a bubble transcript |
| `search_messages` | Substring search over all history, filtered by chat, sender, and date range. The search term is optional — pass filters alone to read a conversation. Renders as a card |
| `get_message` | One message in full, with attachments and delivery timestamps. Renders as a card |
| `get_attachment` | Attachment bytes, base64-encoded |
| `refresh_search_index` | Warm or rebuild the local search index |
| `compose_message` | Open Messages with text prefilled — **you** press send |
| `send_message` | Send to an existing conversation; delivers immediately |

## Previews in the chat

The four read tools render an inline card in the transcript, the way the Gmail
and Superhuman connectors show an email instead of a wall of JSON:

| Tool | Card |
| --- | --- |
| `list_chats` | A conversation list like the Messages sidebar — avatar, name, last-message preview, relative date, a green dot for SMS/RCS |
| `get_chat_messages` | An iMessage-style transcript — blue/green bubbles for you, grey for them, day separators, sender names in groups, tapbacks, attachments, edited and unsent markers |
| `search_messages` | Hit rows across conversations with the match highlighted; scoped to one `chat_id`, a transcript instead |
| `get_message` | One bubble plus delivery details and the attachment list |

This is [MCP Apps](https://modelcontextprotocol.io/specification/draft/extensions/apps)
(`io.modelcontextprotocol/ui`), built the same way as the Apple Mail
connector's `preview_email` so the two cards read as one family: the same host
tokens and fallback palette, the same header/footer chrome, avatar colours and
date formatting, the same handshake. Each of those tools carries
`_meta.ui.resourceUri` (and the pre-GA flat `_meta["ui/resourceUri"]`, which
some hosts still read) pointing at `ui://apple-messages/message-preview`, a
single self-contained HTML document the server serves as
`text/html;profile=mcp-app`. Claude Desktop renders it in a sandboxed iframe
and streams the tool call's arguments and result to it over `postMessage`
(`ui/initialize`, `ui/notifications/tool-input`, `ui/notifications/tool-result`).
The document reads the host's theme and CSS variables so it matches light and
dark mode, reports its own height so the card fits its content, and collapses
long transcripts behind a *Show all* button.

Nothing about the tool results changed: a host without Apps support ignores
the metadata and sees the same JSON as before, which is also what the model
reads. One document serves all four tools — it picks a view from the tool name
the host reports, falling back to the shape of the structured result.

The document lives at `src/apple_messages_mcp/ui/message_preview.html`, with
the Messages icon inlined as a data URI at load time (the sandbox blocks
outside fetches, so the page must be self-contained). Like the Mail card it
draws its own border (`prefersBorder: false`) and declares no CSP allowances,
so the host runs it under the strictest default: no network, no nested frames.

The "Widget from Apple Messages" label above the card is the host's own chrome
for MCP Apps, not something the connector controls. `tests/test_preview.py` drives
the real server over the SDK's in-memory transport and checks the tool
metadata, the resource, and the result shapes the card dispatches on.

## Requirements

- macOS 13 Ventura or later. RCS requires macOS 26 or later.
- **Full Disk Access** for the `uv` that Claude Desktop launches the extension
  with — required for reading. Enabling Claude itself is not enough.
- Automation permission for Messages — required for sending and for contact
  names. macOS prompts for this one automatically.

## Installation

Download the latest `.mcpb` from [Releases](../../releases), then **double-click** to install.

### Granting Full Disk Access

Claude Desktop launches extension servers through a helper that makes the
spawned `uv` responsible for their permissions, so macOS checks Full Disk
Access against that `uv`, not against Claude.

1. System Settings → Privacy & Security → Full Disk Access → **+**
2. Press ⌘⇧G and paste the `uv` path. The extension's error message prints the
   exact one; it has the form
   `~/Library/Application Support/Claude/uv-runtime/<version>/uv`.
3. **Quit and reopen Claude (⌘Q).** macOS caches this permission at process
   launch, so the restart is mandatory — the extension will keep failing
   without it.

### Using several Apple connectors

This connector is one of a family of Claude Desktop extensions for Apple apps — [Mail](https://github.com/falconbradley/claude-connector-apple-mail), **Messages**, [Contacts](https://github.com/falconbradley/claude-connector-apple-contacts), [Calendar](https://github.com/falconbradley/claude-connector-apple-calendar), [Reminders](https://github.com/falconbradley/claude-connector-apple-reminders), [Notes](https://github.com/falconbradley/claude-connector-apple-notes) — and they share the same setup quirks:

- **Install them one at a time.** Opening several `.mcpb` files at once can leave Claude Desktop showing only the last install dialog, so the others silently never install. Approve each dialog before opening the next, then check **Settings → Extensions**.
- **Permissions belong to `uv`, not Claude.** Claude Desktop launches every extension through the same bundled `uv` and macOS attributes their privacy grants to it. Full Disk Access granted once to `~/Library/Application Support/Claude/uv-runtime/<version>/uv` covers Mail, Notes, Messages, and Reminders together; the Contacts, Calendars, and Reminders panes list the connectors as **uv**. Permission errors print the exact path in use, ready to paste.
- **Re-grant after Claude Desktop updates `uv`.** The `<version>` folder changes and macOS treats the new binary as a new app. Symptoms: Mail and Notes searches report `"engine": "applescript"` and get slow, Messages reads and Reminders tags fail with a Full Disk Access error.
- **Restart after granting.** Quit Claude (⌘Q) and reopen it — macOS reads Full Disk Access only at launch.
- **Verify.** Ask Claude for each connector's stats (`get_stats`). For Mail and Notes, a search result's `engine` should be `"sqlite"`.


## Why this needs Full Disk Access when Apple Mail doesn't

The Apple Mail extension talks to Mail.app entirely through scripting, so it
needs no special permissions. Messages cannot work that way.

Messages' AppleScript dictionary exposes exactly four classes — `account`,
`chat`, `participant`, `file transfer` — and **no message class**. Verified on
macOS 26.5.2:

```
$ osascript -e 'tell application "Messages" to get every text message of first chat'
syntax error: Expected "from", etc. but found identifier. (-2741)
```

Chats and participants enumerate fine; message *bodies* are simply not exposed.
So the only read path is SQLite over `~/Library/Messages/chat.db`, which is
TCC-protected. Unlike Automation, Full Disk Access cannot be requested
programmatically — the user must grant it by hand.

The extension therefore uses both permissions for different jobs:

| Concern | Mechanism | Permission |
| --- | --- | --- |
| Messages, chats, search, attachments | SQLite on `chat.db` | Full Disk Access |
| Contact names for raw handles | Messages scripting | Automation |
| Sending | Messages scripting (`send`) | Automation |
| Compose window, prefilled | `imessage:` / `sms:` URL scheme | none |

Contact names come from Messages' `participant` class (`full name`), which reads
the user's Contacts card. That sidesteps the separately-protected AddressBook
database — if Automation is denied, handles simply render as raw numbers.

## Implementation notes

**`attributedBody`.** Since Ventura, `message.text` is frequently NULL and the
body lives in `message.attributedBody` as an Apple *typedstream* — the legacy
`NSArchiver` format, which `plistlib` cannot read. `typedstream.py` decodes it in
pure Python, so the bundle needs no PyObjC dependency. It anchors on the
`NSString`/`NSMutableString` class name and reads the length-prefixed UTF-8
payload after the `+` type marker. Decoding is total: an undecodable body yields
`None` rather than failing the query.

**Timestamps.** `message.date` is Apple-epoch (2001-01-01), in *seconds* before
macOS 13 and *nanoseconds* since. Both are detected and handled.

**Search.** chat.db ships no text index, and most bodies live only in
`attributedBody`, where SQL cannot see them. That combination is nastier than
it looks.

The first implementation widened its predicate to
`m.text LIKE ? OR m.attributedBody IS NOT NULL` and re-filtered the decoded
text in Python. Because that second clause is true for nearly every modern
row, the query's `LIMIT` truncated the scan to the newest few hundred messages
before the Python filter ever ran — so any older match silently disappeared. A
search for a real message returned zero results rather than being slow. On a
916 MB history that meant search effectively covered only the last few days.

The fix is to decode once instead of per query. `index.py` mirrors decoded,
casefolded bodies into `~/Library/Caches/apple-messages-mcp/search-index.db`,
which searches then join against — so the match, the filters, the ordering and
the `LIMIT` all apply to the complete history in SQL. The mirror is:

- **Incremental.** New messages are found by a `message.ROWID` watermark.
  Edits and unsends reuse an existing ROWID, so each refresh also looks at the
  most recent 2000 rows — but only at those with `date_edited` set or with both
  body columns now NULL, since re-decoding 2000 blobs on every search is real
  work that almost always finds nothing. An edit that Messages somehow did not
  stamp, or one older than that window, needs
  `refresh_search_index(rebuild=True)`. Improving the `attributedBody` decoder
  also warrants a rebuild; bumping `SCHEMA_VERSION` forces one.
- **Casefolded, and only that.** Display text still comes from chat.db, so the
  mirror is purely a matching oracle. Storing `str.casefold()` halves its size
  and makes case-insensitive matching correct for non-ASCII — SQLite's `LIKE`
  folds case for ASCII alone.
- **Disposable.** It lives in `~/Library/Caches` and rebuilds if deleted.
  Nothing here writes to chat.db.

Not FTS5, despite the earlier plan here: FTS5 matches whole tokens, so
`MATCH 'dentist'` never finds "mydentist", which is narrower than the substring
semantics `search_messages` documents. A substring scan over compact casefolded
text is already fast, so FTS5 would have doubled the index for semantics we
cannot use. Adding it later is a contained change if a query ever does drag.

**Read-only and non-locking.** Connections open `mode=ro` and no statement
mutates the database. If SQLite cannot open the live WAL read-only, it falls
back to a private snapshot copy so a running Messages.app is never disturbed.

**Tapbacks, edits, replies.** Reactions are decoded from
`associated_message_type` (2000–2007, with the 3000-range as their removals),
threaded replies from `thread_originator_guid`, and edits from `date_edited`.

## Sending messages

Messages has no draft object, so there is no exact analogue of the Mail
extension's draft-first design. The write path therefore comes at two levels,
and they are deliberately not equivalent.

**`compose_message` — the safe default.** Opens Messages with the recipient and
body prefilled via the `imessage:` / `sms:` URL scheme, and stops. A human
reads it and presses send, so nothing leaves the machine on Claude's say-so.
This is also the only way to start a *new* conversation. Needs no permission at
all.

**`send_message` — delivers immediately.** Uses the scripting interface's
`send`, and cannot be unsent. It takes a `chat_id` rather than a phone number,
which is not a limitation but the point: the dictionary accepts either a
`participant` or a `chat`, and addressing an existing chat by GUID lets
*Messages* choose the transport (iMessage / SMS / RCS) instead of the caller
guessing and silently sending an SMS to someone on iMessage. It also requires
`confirm=True`, purely as a guard against being triggered casually.

### What is verified, and what is not

Confirmed on macOS 26.5.2 — the dictionary exposes

```
send : direct-parameter (file | text), to: (participant | chat)
```

the `service type` enumeration is `SMS`, `iMessage`, **`RCS`**, `chat` has a
GUID `id` property to address, and the generated AppleScript compiles.

**Not confirmed: that a live send actually delivers.** Apple has broken
AppleScript `send` before, and its presence in the dictionary has never been
proof that it works. Nothing in the test suite delivers a message, so the first
real send is the experiment. If it fails, `shortcuts run` with a "Send Message"
action is the fallback worth trying next.

Message text reaches AppleScript as an `osascript` argument (`on run argv`)
rather than being interpolated into script source, so a body containing a
double quote is inert rather than a syntax error or an injection.

Sending attachments is not wired up, though the `file` direct parameter means
it is in reach.

## Development

```bash
python3 tests/test_db.py       # SQL, decoder, and search-index tests
python3 tests/test_send.py     # compose URLs, send guards, argv safety
uv run python tests/test_preview.py   # MCP Apps preview: tool metadata, resource, result shapes
python3 tools/probe_schema.py  # verify the real chat.db (needs Full Disk Access)
./build.sh                     # test, validate manifest, pack the .mcpb
```

`tests/test_db.py` builds throwaway databases with the real schema, so the SQL
can be validated without Full Disk Access or a real message history. One of them
buries a match under 3000 newer messages, which is the regression test for the
truncated-search bug described above.

`tests/test_send.py` never sends anything or opens a window: it covers the URL
builder, the guard clauses, and the exact `osascript` argv — so it is safe
anywhere, and correspondingly cannot tell you whether Apple's `send` works.

`tests/test_preview.py` runs the server in-process through the MCP SDK's
in-memory transport (so it needs the project environment, hence `uv run`),
against the same synthetic database, with a stub in place of the contact
resolver so it never scripts Messages.app.

None of the suites touch the real search index; each injects a temporary one.

## Releasing

Every connector in the family releases the same way:

1. Bump the version in `pyproject.toml`, `manifest.json`, and `src/apple_messages_mcp/__init__.py`, then run `uv lock` so `uv.lock` matches. CI fails if the three disagree.
2. Add a section for the version to [CHANGELOG.md](CHANGELOG.md).
3. Commit, tag `vX.Y.Z`, and push the tag: `git push origin main vX.Y.Z`.

The [release workflow](.github/workflows/release.yml) then runs the tests, checks the tag matches all three version files, builds with `./build.sh`, and publishes `apple-messages.mcpb` and `apple-messages-X.Y.Z.mcpb` to a GitHub release whose notes are that version's CHANGELOG section.

## License

MIT
