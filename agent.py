#!/usr/bin/env python3
"""
iMessage → Claude Code → Mac Control Agent
Main daemon process.

Startup:
  python3 ~/.imessage-agent/agent.py

Requires:
  - Full Disk Access for chat.db
  - Claude Code CLI (`claude`) on PATH
  - Messages.app signed-in with iMessage account
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

# Ensure sibling modules are importable when launched by launchd
_AGENT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_AGENT_DIR))

from imessage_reader import get_new_messages
from imessage_sender import send_imessage
from executor import execute_command, call_claude_code, process_incoming_file
from interactive import InteractiveSession
from response_parser import parse_and_execute_response
from memory import init_db, save_message, build_context_prefix, run_daily_maintenance

# ── Paths ─────────────────────────────────────────────────────────────────────

CONFIG_FILE = _AGENT_DIR / "config.json"
LAST_ROWID_FILE = _AGENT_DIR / "last_rowid.txt"
LOG_FILE = _AGENT_DIR / "agent.log"

DEFAULT_CONFIG: dict = {
    "allowed_phone": "",
    "trigger_prefix": "/c ",
    "poll_interval": 3,
    "max_response_length": 4500,
    "claude_timeout": 300,
    "shell_timeout": 60,
    "log_file": str(LOG_FILE),
    # Vision / file transfer settings
    "anthropic_api_key": "",
    "vision_model": "claude-sonnet-4-6",
    "vision_enabled": True,
    "max_image_size_mb": 20,
    "max_file_size_mb": 100,
    # Telegram settings
    "telegram_bot_token": "",
    "allowed_telegram_ids": [],
    "sender_identity_map": {},
}


# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging(log_file: str) -> None:
    log_path = Path(log_file).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(str(log_path), encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ── Sensitive-data masking ────────────────────────────────────────────────────

_CREDENTIAL_RE = re.compile(
    r"^(?=.*[A-Za-z])(?=.*\d)(?=.*[^A-Za-z\d]).{8,}$"
)


def sanitize_for_log(text: str) -> str:
    """Mask probable passwords/credentials before writing to log."""
    if _CREDENTIAL_RE.match(text.strip()):
        return "[REDACTED_CREDENTIAL]"
    return text


# ── Config ────────────────────────────────────────────────────────────────────

def load_or_create_config() -> dict:
    """Load config.json, or create it interactively if missing."""
    _AGENT_DIR.mkdir(parents=True, exist_ok=True)

    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
        # Backfill any missing keys
        for k, v in DEFAULT_CONFIG.items():
            config.setdefault(k, v)
        return config

    # ── Interactive first-run setup ───────────────────────────────────────────
    print("=== iMessage Agent Initial Setup ===\n")
    print("⚠️  Full Disk Access permission is required.")
    print("   System Preferences → Security & Privacy → Full Disk Access")
    print("   and add Terminal (or Python3).\n")

    phone = input("Allowed phone number (e.g. +12125551234): ").strip()
    if not phone:
        print("No phone number entered. Exiting.")
        sys.exit(1)

    config = DEFAULT_CONFIG.copy()
    config["allowed_phone"] = phone

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    print(f"\n✅ Config saved: {CONFIG_FILE}\n")
    return config


# ── ROWID persistence ─────────────────────────────────────────────────────────

def load_last_rowid() -> int:
    if LAST_ROWID_FILE.exists():
        try:
            return int(LAST_ROWID_FILE.read_text().strip())
        except (ValueError, OSError):
            return 0
    return 0


def save_last_rowid(rowid: int) -> None:
    try:
        LAST_ROWID_FILE.write_text(str(rowid))
    except OSError as exc:
        logging.error("Failed to save ROWID: %s", exc)


# ── Agent statistics ──────────────────────────────────────────────────────────

class AgentStats:
    def __init__(self):
        self.start_time = datetime.now()
        self.processed_count = 0
        self.last_command: str | None = None
        self.last_command_time: datetime | None = None

    def record(self, command: str) -> None:
        self.processed_count += 1
        self.last_command = command
        self.last_command_time = datetime.now()

    def status_message(self) -> str:
        now = datetime.now()
        uptime = now - self.start_time
        hours, rem = divmod(int(uptime.total_seconds()), 3600)
        minutes = rem // 60

        lines = [
            "🤖 Agent Status",
            f"Uptime: {hours}h {minutes}m",
            f"Commands processed: {self.processed_count}",
        ]
        if self.last_command and self.last_command_time:
            elapsed_min = int((now - self.last_command_time).total_seconds() // 60)
            preview = (
                self.last_command[:50] + "…"
                if len(self.last_command) > 50
                else self.last_command
            )
            lines.append(f"Last command: {preview} ({elapsed_min}m ago)")
        else:
            lines.append("Last command: none")

        return "\n".join(lines)


# ── Shutdown ──────────────────────────────────────────────────────────────────

_running = True


def _handle_shutdown(signum, frame):
    global _running
    logging.info("Signal %d received → graceful shutdown", signum)
    _running = False


# ── Command dispatch ──────────────────────────────────────────────────────────

def _dispatch(command: str, config: dict, session: InteractiveSession,
              stats: AgentStats, sender: str = ""):
    """
    Route a single command string (after the /c prefix).
    Returns (response, question).
    """
    stats.record(command)

    # Built-in status command
    if command.strip().lower() == "status":
        return stats.status_message(), None

    # Build memory context and pass to executor
    context_prefix = build_context_prefix(sender) if sender else ""
    return execute_command(command, config, session, context_prefix=context_prefix)


def _handle_interactive_reply(
    text: str,
    attachments: list,
    config: dict,
    session: InteractiveSession,
    sender: str = "",
) -> tuple[str | None, str | None]:
    """Handle a reply message (with optional attachments) while waiting for interactive input."""
    if session.is_timed_out():
        return "⏰ Response timeout. Interactive session ended.", None

    logged_text = sanitize_for_log(text)
    logging.info("Interactive reply: %s (attachments: %d)", logged_text[:80], len(attachments))

    # If the reply includes files, copy them to inbox and mention paths in the prompt
    reply_content = text
    if attachments:
        from file_handler import copy_to_inbox
        inbox_paths = []
        for att in attachments:
            local = copy_to_inbox(att["path"])
            if local:
                inbox_paths.append(local)
        if inbox_paths:
            file_list = ", ".join(inbox_paths)
            reply_content = (
                f"{text}\n[Attached files: {file_list}]" if text
                else f"[Attached files: {file_list}]"
            )

    followup = session.build_followup_prompt(reply_content)
    context_prefix = build_context_prefix(sender) if sender else ""
    raw = call_claude_code(followup, config, context_prefix=context_prefix)
    return session.process_response(raw)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    global _running

    config = load_or_create_config()
    setup_logging(config.get("log_file", str(LOG_FILE)))

    logging.info("=== iMessage Agent started ===")
    logging.info("Allowed phone: %s", config["allowed_phone"])
    logging.info("Trigger prefix: %r", config["trigger_prefix"])
    logging.info("Poll interval: %ds", config["poll_interval"])

    print("\n⚠️  Full Disk Access permission is required.")
    print("   System Preferences → Security & Privacy → Full Disk Access → add Terminal\n")

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    # Initialise memory DB
    init_db()

    stats = AgentStats()
    last_rowid = load_last_rowid()
    logging.info("Start ROWID: %d", last_rowid)

    # One InteractiveSession per sender phone number
    sessions: dict[str, InteractiveSession] = {}
    # Track which senders had maintenance run this session
    _maintenance_done: set[str] = set()

    # ── Start Telegram bot if configured ─────────────────────────────────────
    if config.get("telegram_bot_token"):
        try:
            from telegram_bot import TelegramChannel
            tg = TelegramChannel(config, stats, sessions, _maintenance_done)
            tg.start_in_thread()
        except ImportError:
            logging.warning(
                "Telegram bot token configured but python-telegram-bot not installed. "
                "Run: pip3 install python-telegram-bot"
            )
        except Exception as exc:
            logging.error("Failed to start Telegram bot: %s", exc)

    trigger = config["trigger_prefix"]  # "/c "

    while _running:
        try:
            new_messages = get_new_messages(config["allowed_phone"], last_rowid)
        except PermissionError as exc:
            logging.error("DB access permission error: %s", exc)
            time.sleep(config["poll_interval"])
            continue
        except Exception as exc:
            logging.error("Message fetch error: %s", exc, exc_info=True)
            time.sleep(config["poll_interval"])
            continue

        for msg in new_messages:
            rowid: int = msg["rowid"]
            text: str = msg.get("text") or ""
            sender: str = msg["sender"]
            is_from_me: int = msg.get("is_from_me", 0)
            attachments: list = msg.get("attachments", [])

            # Strip U+FFFC (Object Replacement Character) that iMessage
            # inserts as a placeholder for inline attachments, plus whitespace.
            text_clean = text.replace("\uFFFC", "").strip()

            # Skip own outgoing messages that don't have the trigger prefix.
            # Self-messages (sending to yourself) are recorded as is_from_me=1
            # in chat.db; only process those that start with the trigger.
            if is_from_me and not text_clean.startswith(trigger):
                logging.debug("Skipping own message ROWID=%d: %s", rowid, text[:40])
                last_rowid = rowid
                save_last_rowid(last_rowid)
                continue

            logging.info(
                "Received ROWID=%d from=%s from_me=%d text=%r attachments=%d",
                rowid, sender, is_from_me, text_clean[:60], len(attachments),
            )

            session = sessions.setdefault(sender, InteractiveSession())

            # Run daily maintenance once per sender per agent session
            if sender not in _maintenance_done:
                try:
                    run_daily_maintenance(sender, config)
                except Exception as exc:
                    logging.warning("Memory maintenance error for %s: %s", sender, exc)
                _maintenance_done.add(sender)

            try:
                response = None
                question = None
                text_s = text_clean

                # ── Attachment(s) + /c trigger → new file command (always fresh) ──
                if attachments and text_s.startswith(trigger):
                    if session.waiting_for_reply:
                        logging.info("New /c command preempts interactive session for %s", sender)
                        sessions[sender] = InteractiveSession()
                        session = sessions[sender]
                    command_text = text_s[len(trigger):].strip()
                    session.original_prompt = command_text
                    session.conversation_history = []
                    logging.info(
                        "Processing file command: %r (%d attachment(s))",
                        command_text[:80], len(attachments),
                    )
                    response, question = process_incoming_file(
                        attachments, command_text, config, session
                    )

                # ── Regular /c text command (always fresh, preempts interactive) ──
                elif text_s.startswith(trigger):
                    if session.waiting_for_reply:
                        logging.info("New /c command preempts interactive session for %s", sender)
                        sessions[sender] = InteractiveSession()
                        session = sessions[sender]
                    command = text_s[len(trigger):]
                    session.original_prompt = command
                    session.conversation_history = []
                    logging.info("Processing command: %s", command[:120])
                    save_message(sender, "user", command)
                    response, question = _dispatch(command, config, session, stats, sender=sender)

                # ── Waiting for interactive reply (no /c trigger) ─────────────────
                elif session.waiting_for_reply:
                    response, question = _handle_interactive_reply(
                        text, attachments, config, session, sender=sender
                    )
                    if question is None and response and "Response timeout" in response:
                        # Timeout — send message and reset session
                        send_imessage(sender, response)
                        sessions[sender] = InteractiveSession()
                        last_rowid = rowid
                        save_last_rowid(last_rowid)
                        continue

                # ── Attachment(s) without /c trigger and not waiting → ignore ─────
                elif attachments:
                    logging.debug(
                        "Ignoring attachment(s) without trigger from %s", sender
                    )
                    last_rowid = rowid
                    save_last_rowid(last_rowid)
                    continue

                # ── No trigger, no attachments, not waiting → ignore ──────────────
                else:
                    logging.debug("Ignoring (no trigger): %s", text[:60])
                    last_rowid = rowid
                    save_last_rowid(last_rowid)
                    continue

                # ── Post-process response: handle [SEND_FILE/SCREENSHOT] ───────
                if response:
                    response, files_sent = parse_and_execute_response(
                        response, sender, config
                    )
                    if files_sent:
                        logging.info("Files sent: %s", files_sent)

                # ── Save assistant response to memory ────────────────────────
                if response:
                    try:
                        save_message(sender, "assistant", response)
                    except Exception as exc:
                        logging.warning("Memory save error: %s", exc)

                # ── Deliver response ──────────────────────────────────────────
                logging.info("Response ready (len=%d): %r", len(response or ""), (response or "")[:80])
                if question:
                    session.record_assistant_turn(response or "")
                    session.start_waiting()
                    full = f"{response}\n\n❓ {question}" if response else f"❓ {question}"
                    logging.info("Sending interactive reply to %s", sender)
                    send_imessage(sender, full)
                else:
                    if not response:
                        response = "⚠️ No response received."
                    logging.info("Sending response to %s", sender)
                    send_imessage(sender, response)
                    sessions[sender] = InteractiveSession()

            except Exception as exc:
                err_msg = f"❌ Error: {exc}"
                logging.error("Command error ROWID=%d: %s", rowid, exc, exc_info=True)
                try:
                    send_imessage(sender, err_msg)
                except Exception as send_exc:
                    logging.error("Failed to send error message: %s", send_exc)

            last_rowid = rowid
            save_last_rowid(last_rowid)

        if _running:
            time.sleep(config["poll_interval"])

    logging.info("=== iMessage Agent stopped ===")


if __name__ == "__main__":
    main()
