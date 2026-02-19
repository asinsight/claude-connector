# iMessage & Telegram → Claude Code → Mac Control Agent

An iMessage + Telegram dual-channel agent that receives commands, runs them through Claude Code, and controls the Mac.

## Architecture

```
agent.py main()
  ├─ Main Thread: iMessage polling loop
  │     chat.db polling (SQLite read-only, every 3 seconds)
  │     → trigger filter (/c )
  │     → executor.py routing
  │         ├─ /c status       → AgentStats text
  │         ├─ /c !<cmd>       → direct shell execution
  │         └─ /c <natural language> → claude -p (Claude Code CLI)
  │     → [NEED_INPUT] → InteractiveSession (5 min timeout, cancel with new /c)
  │     → [SEND_FILE] / [SEND_SCREENSHOT] → response_parser.py
  │     → iMessage reply (AppleScript)
  │
  └─ Daemon Thread: Telegram bot (python-telegram-bot v20+, async)
        telegram_bot.py
        → same routing logic as iMessage (no /c prefix required)
        → same executor, memory, interactive session
        → replies via Telegram Bot API
        → typing indicator while processing

Shared resources (thread-safe via GIL + SQLite):
  executor.py, memory.py, interactive.py, file_handler.py,
  vision_analyzer.py, browser_helper.py, response_parser.py
```

## Source Files

| File | Role |
|---|---|
| `agent.py` | Main loop. Message polling, routing, session management, Telegram thread startup |
| `imessage_reader.py` | Read chat.db. `allowed_phone` supports both str and list[str] |
| `imessage_sender.py` | Send iMessage text via AppleScript (chunk 1500 chars, retry 3x) |
| `executor.py` | Command routing, deletion block regex, `claude -p` caller |
| `interactive.py` | `[NEED_INPUT:question]` handling, conversation history |
| `response_parser.py` | `[SEND_FILE:/path]`, `[SEND_SCREENSHOT:App]` parsing and execution. Accepts `send_file_fn` parameter for channel-agnostic file sending |
| `file_handler.py` | Attachment classification, inbox copy, PDF text extraction, HEIC→JPEG conversion |
| `vision_analyzer.py` | Anthropic Vision API (curl with stdin payload, no external packages) |
| `file_sender.py` | File/screenshot iMessage sending |
| `browser_helper.py` | Safari/Chrome page reading, form filling (AppleScript + JS) |
| `memory.py` | SQLite-based conversation memory (today's full messages + previous day summaries + permanent archive) |
| `telegram_bot.py` | Telegram bot channel. `TelegramChannel` class running in daemon thread with own asyncio event loop |
| `telegram_sender.py` | Send files/photos via Telegram Bot API (async + sync wrappers) |
| `install.sh` | Installer script (copy files, create config, register launchd) |

## Runtime Directory

```
~/.imessage-agent/
├── config.json       ← configuration
├── last_rowid.txt    ← last processed ROWID
├── memory.db         ← conversation memory (SQLite)
├── agent.log         ← main log
├── stdout.log / stderr.log
├── inbox/            ← received attachments
└── outbox/archive/   ← sent files older than 24h (never deleted)
```

## Current Installation Environment

### launchd plist
Executable binary in `~/Library/LaunchAgents/com.imessage-agent.plist`:
```
/opt/homebrew/Cellar/python@3.14/3.14.2_1/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python
```
> **Reason**: The binary launched directly by launchd must have its own Full Disk Access grant.
> `/usr/bin/python3` (system) cannot be added to FDA in the GUI → use Homebrew Python.app.

### Claude binary
Claude Code is installed in the VS Code extension path (contains spaces), resolved via symlink:
```
/opt/homebrew/bin/claude → ~/Library/Application Support/Claude/claude-code/<version>/claude
```

### Full Disk Access
- Target: `/opt/homebrew/Cellar/python@3.14/.../Resources/Python.app`
- Path contains spaces — use `⌘+Shift+G` in the file picker to paste the path

### Screen Recording (for screenshots)
- **System Settings → Privacy & Security → Screen Recording** → add Python.app
- Required for `screencapture` command to work from a launchd agent
- Screenshots will fail if the display is asleep (lid closed / screen off)

## config.json Key Reference

```json
{
  "allowed_phone": ["user@icloud.com", "+12125551234"],
  "trigger_prefix": "/c ",
  "poll_interval": 3,
  "claude_timeout": 300,
  "anthropic_api_key": "...",
  "vision_model": "claude-sonnet-4-6",
  "vision_enabled": true,

  "telegram_bot_token": "...",
  "allowed_telegram_ids": [123456789],
  "sender_identity_map": {
    "123456789": "user@icloud.com"
  }
}
```

- `allowed_phone`: **str or list** — the same Apple ID may appear as both email and phone number in iMessage, so register both
- `trigger_prefix`: default `/c ` (slash, c, space)
- `poll_interval`: default 3 seconds (reduced from 10s to mitigate WAL timing issues)
- `telegram_bot_token`: bot token from @BotFather. Empty = Telegram disabled
- `allowed_telegram_ids`: list of allowed Telegram user IDs (integers)
- `sender_identity_map`: maps Telegram user_id (as string key) to iMessage handle. Enables cross-channel shared memory. If no mapping, `telegram:<user_id>` is used (separate memory)

## Security Rules

### File deletion — two-layer block
1. `executor.py` `BLOCKED_PATTERNS` regex — `rm`, `rmdir`, `unlink`, `shutil.rmtree`, `os.remove`, `find -delete`, etc.
2. Claude Code system prompt — "Never delete files by any means"

### Credential masking
Strings matching mixed letters + digits + symbols (8+ chars) → logged as `[REDACTED_CREDENTIAL]`

## Python Compatibility

`from __future__ import annotations` is required — included at the top of every `.py` file.
- `str | None`, `X | Y` union type hints cause RuntimeError on Python 3.9 without it
- `from __future__ import annotations` enables lazy evaluation of type hints

## SQLite WAL Reading (imessage_reader.py)

chat.db is operated by iMessage in WAL mode. Read-only connection notes:

```python
# isolation_level=None → autocommit: reads latest WAL state on every query
conn = sqlite3.connect(db_uri, uri=True, check_same_thread=False,
                       isolation_level=None)
conn.execute("PRAGMA busy_timeout=5000;")   # wait up to 5s during iMessage lock
```

- `PRAGMA journal_mode=wal;` on a read-only connection causes snapshot timing issues → **not used**
- `isolation_level=None` (autocommit) prevents snapshots from getting stuck on old WAL state
- `busy_timeout=5000` waits during WAL checkpoint locks

## U+FFFC (Object Replacement Character)

When iMessage sends photos/files together with text, `message.text` includes `￼` (U+FFFC) — a placeholder for the inline attachment position.
Example: `￼/c what is this?`

`agent.py` strips U+FFFC before trigger check:
```python
text_clean = text.replace("\uFFFC", "").strip()
```
Without this, `text.startswith("/c ")` fails and attachment+command messages are ignored.

## is_from_me Handling (self-messages)

When sending iMessages to your own Apple ID, chat.db may record `is_from_me = 1`.
(Sometimes `is_from_me = 0` — inconsistent depending on iMessage routing)

- SQL query does **not** filter by `is_from_me = 0` — fetches both directions
- Python ignores messages with `is_from_me = 1` that lack the trigger prefix (`/c `) — these are the agent's own replies
- This ensures `/c` commands sent to yourself always work regardless of `is_from_me` value

## Message Routing Priority (agent.py)

```
1. /c trigger present (with attachments) → always new command (cancels waiting session)
2. /c trigger present (text only)        → always new command (cancels waiting session)
3. waiting_for_reply = True              → treat as answer to previous [NEED_INPUT]
4. attachments only (no trigger)         → ignore (iMessage) / process (Telegram)
5. none of the above                     → ignore
```

Key: `/c` commands always take priority. Even during an interactive session, sending a new `/c` command resets the session and processes the new command.

Empty response handling: when Claude returns an empty result, falls back to `"⚠️ No response received."` (prevents silent failures).

### Telegram routing differences
- No `/c` prefix required — all messages to the bot are treated as commands
- Attachments without text are still processed (unlike iMessage which requires `/c`)
- Typing indicator (`ChatAction.TYPING`) is sent every 4 seconds while processing

## Memory System (memory.py)

`~/.imessage-agent/memory.db` SQLite DB:

| Table | Contents |
|---|---|
| `conversations` | Today's full messages (sender, role, content, created_at) |
| `daily_summaries` | Per-day summaries (generated via `claude -p`, UNIQUE sender+date) |
| `conversation_archive` | Full archive (never deleted) |

On each agent start, `run_daily_maintenance()` is called:
- Summarises messages from before today via `claude -p`
- Saves to `daily_summaries`, removes from `conversations`
- Preserves originals in `conversation_archive`

`build_context_prefix(sender)` → returns past summaries + today's conversation as `[Conversation history with this user:]` block → prepended to Claude prompts.

### Cross-channel memory sharing
When `sender_identity_map` maps a Telegram user to an iMessage handle (e.g. `"123456789" → "user@icloud.com"`), both channels use the same sender key in memory.db. Conversations from iMessage are visible in Telegram context and vice versa.

## Key Patterns

### Claude response JSON parsing (`executor.py:call_claude_code`)
```python
cmd = ["claude", "-p", prompt,
       "--allowedTools", "Bash,Read,Write,Edit,MultiEdit",
       "--output-format", "json",
       "--system-prompt", system_prompt]
# Response: data["result"] or data["content"][]["text"]
```

### Multiple attachment handling (`imessage_reader.py`)
SQL LEFT JOIN creates one row per attachment → Python groups by ROWID

### [NEED_INPUT] session flow
```
Claude response contains [NEED_INPUT:question]
→ session.waiting_for_reply = True
→ next message (without /c) is sent as follow-up prompt to Claude
→ new /c command → session reset, processed as new command
→ 5 minute timeout
```

### [SEND_FILE] / [SEND_SCREENSHOT] flow
```
Parse Claude response text
→ send file/screenshot via iMessage or Telegram (channel-agnostic via send_file_fn)
→ replace directive with "📎 filename sent"
→ send text reply
```

### Telegram bot thread lifecycle
```python
# Cannot use Application.run_polling() in daemon thread (signal handler limitation)
# Instead: manual initialize() → start() → start_polling() → Event().wait()
async def _run_async():
    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(...))
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    await asyncio.Event().wait()  # block forever

# Cross-thread file sending: asyncio.run_coroutine_threadsafe()
# (run_in_executor callbacks cannot create new event loops for the bot)
```

## Agent Management Commands

```bash
# Live log
tail -f ~/.imessage-agent/agent.log

# Status check
launchctl list | grep imessage

# Restart
launchctl unload ~/Library/LaunchAgents/com.imessage-agent.plist
launchctl load   ~/Library/LaunchAgents/com.imessage-agent.plist

# Deploy after code changes (one-liner)
cp /Users/junheeyoon/Code/claude_connector/*.py ~/.imessage-agent/ && launchctl unload ~/Library/LaunchAgents/com.imessage-agent.plist && launchctl load ~/Library/LaunchAgents/com.imessage-agent.plist
```

## Development Notes

- Source code lives in `/Users/junheeyoon/Code/claude_connector/`
- After editing, copy to `~/.imessage-agent/` and restart the agent for changes to take effect
- `install.sh` generates the plist with `/usr/bin/python3` — must be manually changed to Homebrew Python.app path after install
- Running `claude -p` inside a Claude Code session causes `nested sessions` error (expected)
- `memory.py` is included in `install.sh`'s `PY_FILES` array (missing it causes startup failure)
- `python-telegram-bot` must be installed for the Homebrew Python that launchd uses (use `--break-system-packages` if needed)
