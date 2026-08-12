# Apple Messages MCP

Read and search your iMessage, SMS, and RCS conversations from Claude, on macOS.

Companion to [claude-connector-apple-mail](../claude-connector-apple-mail) and
[claude-connector-apple-reminders](../claude-connector-apple-reminders).

**Status: read-only prototype.** Sending is not implemented yet — see
[Sending messages](#sending-messages-not-yet-implemented).

## Tools

| Tool | Description |
| --- | --- |
| `get_stats` | Totals, unread count, per-service breakdown (iMessage/SMS/RCS), date range |
| `list_chats` | Conversations, most recently active first, with participants and a preview |
| `get_chat_messages` | Messages in one conversation, oldest-first, paged |
| `search_messages` | Substring search, filtered by chat, sender, and date range |
| `get_message` | One message in full, with attachments and delivery timestamps |
| `get_attachment` | Attachment bytes, base64-encoded |

## Requirements

- macOS 13 Ventura or later. RCS requires macOS 26 or later.
- **Full Disk Access** for the Claude app.
- Automation permission for Messages — optional, used only to resolve contact names.

### Granting Full Disk Access

1. System Settings → Privacy & Security → Full Disk Access
2. Enable **Claude** (add `/Applications/Claude.app` with **+** if it isn't listed)
3. **Quit and reopen Claude.** macOS caches this permission at process launch, so
   the restart is mandatory — the extension will keep failing without it.

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
| Contact names for raw handles | Messages scripting | Automation (optional) |

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

**Search.** chat.db ships no FTS index, so search is a `LIKE` scan. Rows whose
body exists only in `attributedBody` are invisible to SQL, so the query
over-fetches and re-filters in Python against decoded text. On a large history
(~860 MB here) this is the main performance concern; an FTS5 mirror is the
obvious next step if it proves slow.

**Read-only and non-locking.** Connections open `mode=ro` and no statement
mutates the database. If SQLite cannot open the live WAL read-only, it falls
back to a private snapshot copy so a running Messages.app is never disturbed.

**Tapbacks, edits, replies.** Reactions are decoded from
`associated_message_type` (2000–2007, with the 3000-range as their removals),
threaded replies from `thread_originator_guid`, and edits from `date_edited`.

## Sending messages (not yet implemented)

Sending is feasible — `Messages.app`'s dictionary does expose:

```
send : direct-parameter (file | text), to: (participant | chat)
```

and the `service type` enumeration on macOS 26 is `SMS`, `iMessage`, **`RCS`**.
Addressing an existing `chat` by GUID is the robust approach, because Messages
picks the transport itself rather than the caller guessing between iMessage, SMS,
and RCS. The `file` direct parameter means attachments are in scope too.

Two things are worth settling before shipping a write path:

1. Apple has repeatedly broken AppleScript `send`; its presence in the dictionary
   is not proof that it works. Needs an empirical test.
2. Messages has no draft concept, so there is no exact analogue to the Mail
   extension's draft-first design. The closest safe equivalent is the
   `sms:`/`imessage:` URL scheme, which opens a compose window with text
   prefilled and lets the user press send. `shortcuts run` with a "Send Message"
   action is a third option if AppleScript send is unreliable.

## Development

```bash
python3 tests/test_db.py       # SQL + decoder tests against a synthetic chat.db
python3 tools/probe_schema.py  # verify the real chat.db (needs Full Disk Access)
./build.sh                     # validate manifest and pack the .mcpb
```

`tests/test_db.py` builds a throwaway database with the real schema, so the SQL
can be validated without Full Disk Access or a real message history.

## License

MIT
