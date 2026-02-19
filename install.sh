#!/usr/bin/env bash
# ============================================================
# iMessage Agent – installer
# ============================================================
# Usage:
#   cd /path/to/this/repo
#   bash install.sh
#
# What it does:
#   1. Checks prerequisites (python3, claude)
#   2. Creates ~/.imessage-agent/ and copies Python modules
#   3. Creates config.json interactively (if absent)
#   4. Sets last_rowid to current MAX to skip old messages
#   5. Creates a launchd LaunchAgent plist
#   6. Loads (starts) the agent via launchctl
# ============================================================

set -euo pipefail

AGENT_DIR="$HOME/.imessage-agent"
PLIST_LABEL="com.imessage-agent"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
RESET='\033[0m'

ok()   { echo -e "${GREEN}✅  $*${RESET}"; }
warn() { echo -e "${YELLOW}⚠️   $*${RESET}"; }
err()  { echo -e "${RED}❌  $*${RESET}"; }

echo ""
echo "=========================================="
echo "  iMessage Agent Installer"
echo "=========================================="
echo ""

# ── 1. Prerequisites ──────────────────────────────────────────────────────────

if ! command -v python3 &>/dev/null; then
    err "python3 is not installed."
    exit 1
fi
ok "Python3: $(python3 --version)"

echo "Installing Python dependencies…"
pip3 install --user python-telegram-bot 2>/dev/null && ok "python-telegram-bot installed" || warn "pip install failed — Telegram bot will be disabled"

CLAUDE_PATH=""
# Search common install locations
for candidate in \
        "$(command -v claude 2>/dev/null || true)" \
        "$HOME/.claude/local/claude" \
        "/usr/local/bin/claude" \
        "/opt/homebrew/bin/claude"; do
    if [[ -x "$candidate" ]]; then
        CLAUDE_PATH="$candidate"
        break
    fi
done

if [[ -z "$CLAUDE_PATH" ]]; then
    warn "'claude' command not found."
    warn "Install Claude Code first: https://claude.ai/code"
    warn "Continuing without it (only /c ! direct shell commands will work)…"
    CLAUDE_PATH="/usr/local/bin/claude"   # placeholder for plist PATH
else
    ok "Claude Code: $CLAUDE_PATH"
fi

# ── 2. Create agent directory and copy files ──────────────────────────────────

mkdir -p "$AGENT_DIR" "$AGENT_DIR/inbox" "$AGENT_DIR/outbox" "$AGENT_DIR/outbox/archive"
ok "Directory: $AGENT_DIR (inbox/ outbox/ created)"

PY_FILES=(agent.py imessage_reader.py imessage_sender.py executor.py browser_helper.py interactive.py file_handler.py vision_analyzer.py file_sender.py response_parser.py memory.py telegram_bot.py telegram_sender.py)

for file in "${PY_FILES[@]}"; do
    src="$SCRIPT_DIR/$file"
    if [[ ! -f "$src" ]]; then
        err "Source file not found: $src"
        exit 1
    fi
    cp "$src" "$AGENT_DIR/$file"
    chmod +x "$AGENT_DIR/$file"
    ok "Copied: $file"
done

# ── 3. Config ─────────────────────────────────────────────────────────────────

if [[ -f "$AGENT_DIR/config.json" ]]; then
    ok "config.json already exists – skipping"
else
    echo ""
    echo "── Initial Setup ───────────────────────────"
    read -r -p "Allowed phone number (e.g. +12125551234): " PHONE
    if [[ -z "$PHONE" ]]; then
        err "No phone number entered."
        exit 1
    fi
    echo ""
    echo "  (Optional) Anthropic API key for image analysis via Claude Vision."
    echo "  Leave blank to disable image analysis."
    read -r -p "Anthropic API key: " ANTHROPIC_KEY

    cat > "$AGENT_DIR/config.json" <<JSON
{
  "allowed_phone": "$PHONE",
  "trigger_prefix": "/c ",
  "poll_interval": 10,
  "max_response_length": 4500,
  "claude_timeout": 300,
  "shell_timeout": 60,
  "log_file": "$AGENT_DIR/agent.log",
  "anthropic_api_key": "$ANTHROPIC_KEY",
  "vision_model": "claude-sonnet-4-5-20250514",
  "vision_enabled": true,
  "max_image_size_mb": 20,
  "max_file_size_mb": 100
}
JSON
    ok "config.json created"
fi

# ── 4. Seed last_rowid to current max (skip history) ─────────────────────────

if [[ ! -f "$AGENT_DIR/last_rowid.txt" ]]; then
    CURRENT_ROWID=$(python3 - <<'PYEOF'
import sqlite3, os
from pathlib import Path
db = Path.home() / "Library" / "Messages" / "chat.db"
try:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message")
    print(cur.fetchone()[0])
    conn.close()
except Exception:
    print(0)
PYEOF
    )
    echo "$CURRENT_ROWID" > "$AGENT_DIR/last_rowid.txt"
    ok "Start ROWID set to $CURRENT_ROWID (older messages will be skipped)"
else
    ok "last_rowid.txt already exists – skipping"
fi

# ── 5. launchd plist ──────────────────────────────────────────────────────────

# Determine PATH to inject (include brew and claude locations)
LAUNCH_PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:$(dirname "$CLAUDE_PATH")"

mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>${AGENT_DIR}/agent.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${AGENT_DIR}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${LAUNCH_PATH}</string>
        <key>HOME</key>
        <string>${HOME}</string>
        <key>LANG</key>
        <string>ko_KR.UTF-8</string>
    </dict>

    <!-- Start when the plist is loaded and keep alive on exit -->
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>${AGENT_DIR}/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${AGENT_DIR}/stderr.log</string>

    <!-- Throttle rapid restarts -->
    <key>ThrottleInterval</key>
    <integer>10</integer>
</dict>
</plist>
PLIST

ok "LaunchAgent plist: $PLIST_PATH"

# ── 6. Load the agent ─────────────────────────────────────────────────────────

# Unload first in case it was already registered
launchctl unload "$PLIST_PATH" 2>/dev/null || true
sleep 1
launchctl load "$PLIST_PATH"
ok "LaunchAgent registered and started"

# ── 7. Post-install guidance ──────────────────────────────────────────────────

echo ""
echo "=========================================="
echo -e "${YELLOW}  ⚠️  Full Disk Access required!${RESET}"
echo "=========================================="
echo ""
echo "  1. Apple menu → System Preferences (or System Settings)"
echo "  2. Security & Privacy → Privacy tab"
echo "  3. Select 'Full Disk Access' in the left list"
echo "  4. Click the lock icon → enter admin password"
echo "  5. Click '+' → add /usr/bin/python3 (or Terminal.app)"
echo ""
echo "  After granting access, restart the agent:"
echo "    launchctl unload $PLIST_PATH"
echo "    launchctl load   $PLIST_PATH"
echo ""
echo "=========================================="
echo "  Useful commands"
echo "=========================================="
echo ""
echo "  Live log:       tail -f $AGENT_DIR/agent.log"
echo "  Stdout log:     tail -f $AGENT_DIR/stdout.log"
echo "  Agent status:   launchctl list | grep imessage"
echo "  Stop agent:     launchctl unload $PLIST_PATH"
echo "  Start agent:    launchctl load   $PLIST_PATH"
echo "  Edit config:    nano $AGENT_DIR/config.json"
echo ""
echo "  Test: send '/c hello' from the allowed phone number via iMessage"
echo ""
