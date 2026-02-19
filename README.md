# iMessage & Telegram → Claude Code → Mac Control Agent

A daemon that watches your Mac's iMessage database and/or Telegram bot, forwards trigger messages to Claude Code, and replies with the result — including files and screenshots — all via the same channel.

```
[iPhone iMessage] ──┐
                    ├→ [command routing] → [Mac control] → [reply + files]
[Telegram Bot]   ───┘
```

## Features

- **Dual channel** — iMessage polling + Telegram bot running in parallel; both share the same memory
- **iMessage monitoring** — polls `~/Library/Messages/chat.db` every 3 seconds (read-only)
- **Telegram bot** — uses `python-telegram-bot` v20+ (async); no `/c` prefix required
- **Allowlist** — only processes messages from configured phone numbers / iCloud emails / Telegram user IDs
- **Trigger prefix** — iMessage only acts on messages starting with `/c ` (Telegram processes all messages)
- **Shell passthrough** — `/c !<cmd>` runs the shell command directly
- **AI control** — `/c <natural language>` is forwarded to `claude -p` (Claude Code)
- **Browser reading** — Claude can read the current Safari/Chrome tab via AppleScript
- **Interactive sessions** — when Claude needs more info it sends `[NEED_INPUT:question]`; the next reply (including attachments) is fed back as context. A new `/c` command always cancels and starts fresh.
- **Conversation memory** — past conversations are summarised daily and injected as context into every Claude call; shared across iMessage and Telegram via sender identity mapping
- **Deletion block** — two-layer guard (regex + system-prompt) prevents any file deletion
- **Auto-restart** — registered as a launchd LaunchAgent (`KeepAlive = true`)
- **Receive files/images** — photos and documents sent from phone/Telegram are copied to `inbox/` and processed
- **Image analysis** — photos (including HEIC) are analysed via Claude Vision API (optional)
- **Send files** — Claude can send files back via `[SEND_FILE:/path]`
- **Send screenshots** — Claude can capture and send the screen via `[SEND_SCREENSHOT]`
- **Typing indicator** — Telegram shows "typing..." while processing commands

## Project Structure

```
claude_connector/              ← source repo (run install.sh from here)
├── agent.py                   main daemon (polling loop, routing, session mgmt)
├── imessage_reader.py         read chat.db + attachment metadata (read-only SQLite)
├── imessage_sender.py         send iMessage text via AppleScript
├── file_sender.py             send files / screenshots via iMessage
├── file_handler.py            classify files, copy to inbox, extract PDF text, convert HEIC
├── vision_analyzer.py         Claude Vision API via curl (no external packages)
├── response_parser.py         parse [SEND_FILE:...] and [SEND_SCREENSHOT:...] directives
├── executor.py                command routing + deletion block + Claude Code caller
├── browser_helper.py          Safari / Chrome page reading and form filling
├── interactive.py             InteractiveSession class
├── memory.py                  SQLite-backed conversation memory + daily summarisation
├── telegram_bot.py            Telegram bot channel (async, daemon thread)
├── telegram_sender.py         send files/photos via Telegram Bot API
├── install.sh                 installer (copies files, creates config, registers launchd)
└── README.md

~/.imessage-agent/             ← runtime directory (created by install.sh)
├── agent.py  …                (copies of the above modules)
├── config.json
├── last_rowid.txt
├── memory.db                  conversation history (SQLite)
├── agent.log
├── stdout.log
├── stderr.log
├── inbox/                     received attachments
└── outbox/                    sent screenshots and files
    └── archive/               outbox files older than 24 h (never deleted)
```

## Requirements

| Requirement | Notes |
|---|---|
| macOS | AppleScript + Messages.app |
| Python 3.9+ | Standard library only for iMessage; `python-telegram-bot` for Telegram |
| Homebrew Python | Required for Full Disk Access (see below) |
| Claude Code CLI | `claude` must be on PATH — symlink required (see below) |
| iMessage account | Signed in to Messages.app |
| **Full Disk Access** | For `chat.db` — see setup below |
| Anthropic API key | Optional — only needed for image analysis |
| Telegram bot token | Optional — only needed for Telegram channel |

## Installation

```bash
git clone <this-repo>
cd claude_connector
bash install.sh
```

The script will:
1. Verify prerequisites (`python3`, `claude`)
2. Install `python-telegram-bot` via pip (optional — Telegram disabled if missing)
3. Create `~/.imessage-agent/` with `inbox/`, `outbox/`, `outbox/archive/`
4. Copy all Python modules there (including `telegram_bot.py`, `telegram_sender.py`)
5. Ask for the allowed phone number/iCloud email and (optionally) an Anthropic API key → write `config.json`
6. Seed `last_rowid.txt` with the current max ROWID (skips old messages)
7. Write `~/Library/LaunchAgents/com.imessage-agent.plist`
8. Load the agent with `launchctl`

### Full Disk Access (required)

The agent reads `~/Library/Messages/chat.db`, which is protected by macOS TCC.

The launchd process must run as the **Homebrew Python.app** binary so macOS recognises its FDA grant.

**Step 1 — Install Homebrew Python (if not already installed):**
```bash
brew install python@3.14   # or python@3.13
```

**Step 2 — Grant Full Disk Access to Python.app:**

1. **System Settings** → **Privacy & Security** → **Full Disk Access**
2. Click **+**
3. Press `⌘ + Shift + G` in the file picker and paste:
   ```
   /opt/homebrew/Cellar/python@3.14/3.14.2_1/Frameworks/Python.framework/Versions/3.14/Resources
   ```
   (adjust version number to match your install)
4. Select **Python.app** → **Open**
5. Toggle **ON**

**Step 3 — Verify the plist uses the correct binary:**

`~/Library/LaunchAgents/com.imessage-agent.plist` must point to the Python.app internal binary:
```xml
<string>/opt/homebrew/Cellar/python@3.14/3.14.2_1/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python</string>
```

### Claude Code CLI symlink (required)

Claude Code is installed inside the VS Code extension directory (path contains spaces). Create a symlink so the agent can find it:

```bash
ln -sf "/Users/$USER/Library/Application Support/Claude/claude-code/$(ls ~/Library/Application\ Support/Claude/claude-code | tail -1)/claude" \
    /opt/homebrew/bin/claude
```

Verify:
```bash
claude --version
```

## Configuration

`~/.imessage-agent/config.json`:

```json
{
  "allowed_phone":       ["user@icloud.com", "+12125551234"],
  "trigger_prefix":      "/c ",
  "poll_interval":       3,
  "max_response_length": 4500,
  "claude_timeout":      300,
  "shell_timeout":       60,
  "log_file":            "~/.imessage-agent/agent.log",

  "anthropic_api_key":   "",
  "vision_model":        "claude-sonnet-4-6",
  "vision_enabled":      true,
  "max_image_size_mb":   20,
  "max_file_size_mb":    100,

  "telegram_bot_token":       "",
  "allowed_telegram_ids":     [123456789],
  "sender_identity_map": {
    "123456789": "user@icloud.com"
  }
}
```

### General settings

| Key | Description |
|---|---|
| `allowed_phone` | Phone number(s) and/or iCloud email(s) to accept. String or JSON array. iMessage may record the same sender as either a phone number or iCloud email depending on the device — list both to be safe. |
| `poll_interval` | Seconds between chat.db polls. Default 3 — lower values give faster response but increase DB read frequency. |
| `anthropic_api_key` | Anthropic API key for image analysis. Leave empty to disable. |
| `vision_model` | Model ID for Vision API calls. Default `claude-sonnet-4-6`. |
| `vision_enabled` | Set `false` to skip image analysis even if a key is present. |
| `max_image_size_mb` | Images larger than this are rejected before sending to Vision API. |
| `max_file_size_mb` | Files larger than this are ignored entirely. |

### Telegram settings

| Key | Description |
|---|---|
| `telegram_bot_token` | Bot token from @BotFather. Leave empty to disable Telegram. |
| `allowed_telegram_ids` | List of allowed Telegram user IDs (integers). Get your ID by messaging the bot and checking the log. |
| `sender_identity_map` | Maps Telegram user IDs (as strings) to iMessage handles. This enables shared conversation memory across channels. If no mapping exists, `telegram:<user_id>` is used (separate memory). |

> **Tip — iCloud email vs phone number:** When you send iMessages from an iPhone to yourself (same Apple ID), macOS may store the sender handle as the iCloud email instead of the phone number. Add both to `allowed_phone` to cover all cases.

> **Tip — Self-messages and `is_from_me`:** iMessage may record messages to yourself as `is_from_me=1` (instead of `0`). The agent handles this by including both directions in the DB query and filtering by trigger prefix in Python — so `/c` commands always work regardless of the `is_from_me` flag.

> **Tip — Finding your Telegram user ID:** Send any message to the bot. If your ID isn't in `allowed_telegram_ids`, the agent logs: `Telegram: unauthorized user_id=XXXXXXX`. Add that number to the config.

### Screen Recording (for screenshots)

If you want the agent to take screenshots (`[SEND_SCREENSHOT]`), grant **Screen Recording** permission to Python.app:

1. **System Settings** → **Privacy & Security** → **Screen Recording**
2. Add the same **Python.app** used for Full Disk Access
3. Note: screenshots will fail if the display is asleep (lid closed / screen off)

## Usage

### iMessage commands

| Message | Behaviour |
|---|---|
| `/c hello` | Forwarded to Claude Code → AI reply |
| `/c !df -h` | Direct shell execution |
| `/c !rm -rf /` | **Blocked** — deletion guard |
| `/c status` | Agent uptime, command count, last command |
| `/c open Safari and go to google.com` | Claude controls Safari via AppleScript |
| `/c what's open in Safari?` | Claude reads the current tab via `browser_helper` |
| `/c take a screenshot` | Claude captures the screen and sends it back |
| `/c send me ~/Desktop/report.pdf` | Claude sends the file via iMessage |

### Telegram commands

All messages sent to the Telegram bot are treated as commands — no `/c` prefix needed:

| Message | Behaviour |
|---|---|
| `hello` | Forwarded to Claude Code → AI reply |
| `!df -h` | Direct shell execution |
| `status` | Agent uptime, command count, last command |
| Photo + `what is this?` | Image downloaded → Vision API → description |
| Document + `review this` | File downloaded → Claude Code analysis |

### Receiving files from phone/Telegram

Attach a file (or photo) to a message and the agent will process it automatically:

| What you send | What happens |
|---|---|
| Photo + `/c what is this?` | HEIC converted → Vision API → description sent back |
| `error.png` + `/c explain this error` | Image analysed in context of the question |
| `script.py` + `/c review this code` | File read by Claude Code → code review |
| `report.pdf` + `/c summarize in 3 points` | Text extracted → Claude Code summarises |
| `data.csv` + `/c plot a chart` | File processed by Claude Code |
| Any file (no `/c`) | Ignored on iMessage — processed on Telegram |

Received files are copied to `~/.imessage-agent/inbox/` before processing.

### Sending files from Mac to phone/Telegram

Claude can include special directives in its response to trigger file sends:

| Directive | Effect |
|---|---|
| `[SEND_FILE:/path/to/file]` | Sends the file via iMessage or Telegram |
| `[SEND_SCREENSHOT]` | Captures the full screen and sends it |
| `[SEND_SCREENSHOT:AppName]` | Brings AppName to front, captures, sends |

These are stripped from the text before it is delivered, replaced with a short status line (e.g. `📎 report.pdf sent`).

### Interactive session example

```
You:  /c log in to GitHub
Bot:  Opened the GitHub login page.
      ❓ Please enter your password.
You:  myP@ssw0rd
Bot:  ✅ Logged in successfully.
```

```
You:  [screenshot of Python error] /c fix this
Bot:  🖼️ Image analysis:
      TypeError on line 42 — caption_list is None. Needs initialisation.
      ❓ Should I fix it in the file directly?
You:  yes
Bot:  ✅ Fixed caption_list initialisation in caption_script.py. [SEND_FILE:~/caption_script.py]
      📎 caption_script.py sent
```

The session times out after **5 minutes** of no reply. Sending a new `/c` command at any time cancels the interactive session and starts fresh.

### Conversation memory

The agent maintains a per-sender conversation history in `~/.imessage-agent/memory.db`:

- **Today's messages** are stored in full and prepended to each Claude call as context
- **Previous days** are summarised overnight (via `claude -p`) into one sentence per day
- **Full archive** is kept permanently in `conversation_archive` table
- **Cross-channel** — when `sender_identity_map` maps a Telegram user to an iMessage handle, both channels share the same conversation history

This gives Claude continuity across sessions without blowing the context window.

## Architecture

### Threading model

```
agent.py main()
  ├─ Main Thread: iMessage polling loop
  │     imessage_reader → _dispatch() → imessage_sender
  │
  └─ Daemon Thread: Telegram bot (python-telegram-bot, async)
        telegram_bot.py → _dispatch() → Telegram Bot API

Shared resources (thread-safe):
  executor.py, memory.py, interactive.py, file_handler.py,
  vision_analyzer.py, browser_helper.py
```

- The iMessage polling loop runs in the main thread
- The Telegram bot runs in a daemon thread with its own asyncio event loop
- Both share the `sessions` dict, `stats`, and `memory.db`
- Python's GIL protects dict/set operations; SQLite handles concurrent reads/writes

## Security

### File deletion — two-layer block

1. **Regex guard** (`executor.py`) — blocks patterns like `rm `, `rmdir`, `unlink`, `shutil.rmtree`, `os.remove`, `find … -delete`, `truncate`, etc. before any execution.
2. **System prompt** — every Claude Code call includes: *"Never delete files or directories by any means. If asked, refuse and explain."*

### Credential masking

Replies that look like passwords (mixed letters + digits + symbols, ≥ 8 chars) are logged as `[REDACTED_CREDENTIAL]`.

### Outbox archiving

Files in `outbox/` that are older than 24 hours are moved to `outbox/archive/`. Nothing is ever deleted by the agent itself.

### Scope

- **iMessage:** Only messages from senders listed in `allowed_phone` are processed. The agent never writes to `chat.db`.
- **Telegram:** Only messages from user IDs listed in `allowed_telegram_ids` are processed. Unauthorized users are logged and ignored.

## Useful Commands

```bash
# Live log
tail -f ~/.imessage-agent/agent.log

# Agent status
launchctl list | grep imessage

# Stop / start
launchctl unload  ~/Library/LaunchAgents/com.imessage-agent.plist
launchctl load    ~/Library/LaunchAgents/com.imessage-agent.plist

# Edit config (e.g. add allowed sender or API key)
nano ~/.imessage-agent/config.json

# Deploy after code changes
cp /path/to/claude_connector/*.py ~/.imessage-agent/
launchctl unload ~/Library/LaunchAgents/com.imessage-agent.plist
launchctl load   ~/Library/LaunchAgents/com.imessage-agent.plist

# View received files
ls ~/.imessage-agent/inbox/

# View sent screenshots
ls ~/.imessage-agent/outbox/
```

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.imessage-agent.plist
rm -f ~/Library/LaunchAgents/com.imessage-agent.plist
rm -rf ~/.imessage-agent
```
