#!/usr/bin/env python3
"""
Telegram Bot channel for the Claude Code agent.

Runs alongside the iMessage polling loop in a daemon thread.
Uses python-telegram-bot v20+ (async).
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
    ContextTypes,
)

from executor import execute_command, call_claude_code, process_incoming_file
from interactive import InteractiveSession
from response_parser import parse_and_execute_response
from memory import save_message, build_context_prefix, run_daily_maintenance
from telegram_sender import send_file_telegram_async

INBOX_DIR = Path.home() / ".imessage-agent" / "inbox"


class TelegramChannel:
    """Manages the Telegram bot lifecycle and message handling."""

    def __init__(
        self,
        config: dict,
        stats,
        sessions: dict[str, InteractiveSession],
        maintenance_done: set[str],
    ):
        self.config = config
        self.stats = stats
        self.sessions = sessions
        self._maintenance_done = maintenance_done
        self.trigger = config["trigger_prefix"]
        self.bot_token = config.get("telegram_bot_token", "")
        self.allowed_ids: list[int] = config.get("allowed_telegram_ids", [])
        self.sender_map: dict[str, str] = config.get("sender_identity_map", {})
        self._app: Application | None = None

    # ── Identity helpers ─────────────────────────────────────────────────────

    def _canonical_sender(self, user_id: int) -> str:
        """Map Telegram user ID to a canonical sender string for memory."""
        uid_str = str(user_id)
        return self.sender_map.get(uid_str, f"telegram:{uid_str}")

    def _is_allowed(self, user_id: int) -> bool:
        if not self.allowed_ids:
            return False
        return user_id in self.allowed_ids

    # ── Text sending ─────────────────────────────────────────────────────────

    async def _send_text(self, chat_id: int, text: str) -> None:
        """Send a text message, chunking if needed (Telegram max ~4096)."""
        MAX_CHUNK = 4000
        if not text:
            return
        bot = self._app.bot
        if len(text) <= MAX_CHUNK:
            await bot.send_message(chat_id=chat_id, text=text)
            return
        remaining = text
        while remaining:
            if len(remaining) <= MAX_CHUNK:
                await bot.send_message(chat_id=chat_id, text=remaining)
                break
            split = remaining.rfind("\n", 0, MAX_CHUNK)
            if split <= 0:
                split = remaining.rfind(" ", 0, MAX_CHUNK)
            if split <= 0:
                split = MAX_CHUNK
            await bot.send_message(chat_id=chat_id, text=remaining[:split])
            remaining = remaining[split:].lstrip("\n ")

    # ── Typing indicator ─────────────────────────────────────────────────────

    def _start_typing(self, chat_id: int, loop: asyncio.AbstractEventLoop) -> asyncio.Task:
        """Start a background task that sends 'typing...' every 4 seconds."""
        async def _typing_loop():
            try:
                while True:
                    await self._app.bot.send_chat_action(
                        chat_id=chat_id, action=ChatAction.TYPING,
                    )
                    await asyncio.sleep(4)
            except asyncio.CancelledError:
                pass
            except Exception:
                pass  # silently stop if chat action fails
        return loop.create_task(_typing_loop())

    # ── File download ────────────────────────────────────────────────────────

    async def _download_file(self, file_id: str, filename: str) -> str | None:
        """Download a Telegram file to inbox/. Returns local path."""
        INBOX_DIR.mkdir(parents=True, exist_ok=True)
        base, ext = os.path.splitext(filename)
        local_path = INBOX_DIR / filename
        counter = 1
        while local_path.exists():
            local_path = INBOX_DIR / f"{base}_{counter}{ext}"
            counter += 1
        try:
            tg_file = await self._app.bot.get_file(file_id)
            await tg_file.download_to_drive(str(local_path))
            logging.info("Telegram file downloaded: %s", local_path)
            return str(local_path)
        except Exception as exc:
            logging.error("Telegram file download error: %s", exc)
            return None

    # ── send_file_fn factory (for response_parser) ───────────────────────────

    def _make_send_fn(self, chat_id: int, loop: asyncio.AbstractEventLoop):
        """Create a synchronous send_file callable bound to this chat.
        Uses run_coroutine_threadsafe to schedule on the Telegram event loop."""
        bot = self._app.bot

        def send_fn(_recipient: str, file_path: str) -> tuple[bool, str]:
            future = asyncio.run_coroutine_threadsafe(
                send_file_telegram_async(bot, chat_id, file_path), loop
            )
            return future.result(timeout=30)

        return send_fn

    # ── Dispatch (mirrors agent.py _dispatch) ────────────────────────────────

    def _dispatch(self, command: str, sender: str) -> tuple[str | None, str | None]:
        """Route a single command (after /c prefix). Returns (response, question)."""
        self.stats.record(command)

        if command.strip().lower() == "status":
            return self.stats.status_message(), None

        context_prefix = build_context_prefix(sender)
        session = self.sessions.get(sender)
        return execute_command(command, self.config, session, context_prefix=context_prefix)

    # ── Interactive reply (mirrors agent.py _handle_interactive_reply) ────────

    def _handle_interactive_reply(
        self, text: str, attachments: list, sender: str
    ) -> tuple[str | None, str | None]:
        session = self.sessions.get(sender)
        if session is None or session.is_timed_out():
            return "⏰ Response timeout. Interactive session ended.", None

        logging.info("Telegram interactive reply from %s: %s", sender, text[:80])

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
        context_prefix = build_context_prefix(sender)
        raw = call_claude_code(followup, self.config, context_prefix=context_prefix)
        return session.process_response(raw)

    # ── Main message handler ─────────────────────────────────────────────────

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Process one incoming Telegram message."""
        message = update.effective_message
        if message is None:
            return

        user = update.effective_user
        if user is None:
            return
        if not self._is_allowed(user.id):
            logging.info(
                "Telegram: unauthorized user_id=%d username=%s — "
                "add to allowed_telegram_ids in config.json to allow",
                user.id, user.username or "N/A",
            )
            return

        chat_id = message.chat_id
        sender = self._canonical_sender(user.id)
        text = (message.text or message.caption or "").strip()
        trigger = self.trigger

        # Download attachments
        attachments: list[dict] = []
        if message.photo:
            photo = message.photo[-1]  # largest size
            local = await self._download_file(
                photo.file_id, f"photo_{photo.file_unique_id}.jpg"
            )
            if local:
                attachments.append({
                    "path": local,
                    "type": "image/jpeg",
                    "name": f"photo_{photo.file_unique_id}.jpg",
                    "size": photo.file_size or 0,
                })
        if message.document:
            doc = message.document
            local = await self._download_file(
                doc.file_id, doc.file_name or "file"
            )
            if local:
                attachments.append({
                    "path": local,
                    "type": doc.mime_type or "",
                    "name": doc.file_name or "file",
                    "size": doc.file_size or 0,
                })

        logging.info(
            "Telegram msg from %s (user_id=%d) text=%r attachments=%d",
            sender, user.id, text[:60], len(attachments),
        )

        session = self.sessions.setdefault(sender, InteractiveSession())

        # Daily maintenance (once per sender per agent session)
        if sender not in self._maintenance_done:
            try:
                run_daily_maintenance(sender, self.config)
            except Exception as exc:
                logging.warning("Memory maintenance error for %s: %s", sender, exc)
            self._maintenance_done.add(sender)

        loop = asyncio.get_event_loop()

        # Show "typing..." while processing
        typing_task = self._start_typing(chat_id, loop)

        # Strip /c prefix if present (optional on Telegram — all messages are commands)
        command_text = text[len(trigger):] if text.startswith(trigger) else text
        is_new_command = bool(command_text) or bool(attachments)

        try:
            response = None
            question = None

            # ── Waiting for interactive reply (no new command text) ─────────
            if session.waiting_for_reply and not text.startswith(trigger):
                response, question = await loop.run_in_executor(
                    None,
                    lambda: self._handle_interactive_reply(text, attachments, sender),
                )
                if question is None and response and "Response timeout" in response:
                    typing_task.cancel()
                    await self._send_text(chat_id, response)
                    self.sessions[sender] = InteractiveSession()
                    return

            # ── Attachment(s) → file command ───────────────────────────────
            elif attachments:
                if session.waiting_for_reply:
                    self.sessions[sender] = InteractiveSession()
                    session = self.sessions[sender]
                session.original_prompt = command_text
                session.conversation_history = []
                logging.info("Telegram file command: %r (%d attachment(s))", command_text[:80], len(attachments))
                response, question = await loop.run_in_executor(
                    None,
                    lambda: process_incoming_file(attachments, command_text, self.config, session),
                )

            # ── Text command (with or without /c prefix) ──────────────────
            elif command_text:
                if session.waiting_for_reply:
                    self.sessions[sender] = InteractiveSession()
                    session = self.sessions[sender]
                session.original_prompt = command_text
                session.conversation_history = []
                logging.info("Telegram command: %s", command_text[:120])
                save_message(sender, "user", command_text)
                response, question = await loop.run_in_executor(
                    None,
                    lambda cmd=command_text: self._dispatch(cmd, sender),
                )

            # ── Empty message → ignore ────────────────────────────────────
            else:
                typing_task.cancel()
                return

            # ── Stop typing before sending response ─────────────────────
            typing_task.cancel()

            # ── Post-process [SEND_FILE/SCREENSHOT] directives ────────────
            if response:
                send_fn = self._make_send_fn(chat_id, loop)
                response, files_sent = await loop.run_in_executor(
                    None,
                    lambda: parse_and_execute_response(
                        response, sender, self.config, send_file_fn=send_fn,
                    ),
                )
                if files_sent:
                    logging.info("Telegram files sent: %s", files_sent)

            # ── Save assistant response to memory ─────────────────────────
            if response:
                try:
                    save_message(sender, "assistant", response)
                except Exception as exc:
                    logging.warning("Memory save error: %s", exc)

            # ── Deliver response ──────────────────────────────────────────
            if question:
                session.record_assistant_turn(response or "")
                session.start_waiting()
                full = f"{response}\n\n❓ {question}" if response else f"❓ {question}"
                await self._send_text(chat_id, full)
            else:
                if not response:
                    response = "⚠️ No response received."
                await self._send_text(chat_id, response)
                self.sessions[sender] = InteractiveSession()

        except Exception as exc:
            typing_task.cancel()
            err_msg = f"❌ Error: {exc}"
            logging.error("Telegram command error: %s", exc, exc_info=True)
            try:
                await self._send_text(chat_id, err_msg)
            except Exception as send_exc:
                logging.error("Failed to send Telegram error message: %s", send_exc)

    # ── Thread lifecycle ─────────────────────────────────────────────────────

    def start_in_thread(self) -> threading.Thread:
        """Start the Telegram bot in a daemon thread."""
        if not self.bot_token:
            raise ValueError("telegram_bot_token is not configured")

        async def _run_async():
            self._app = (
                Application.builder()
                .token(self.bot_token)
                .build()
            )
            self._app.add_handler(
                MessageHandler(
                    filters.TEXT | filters.PHOTO | filters.Document.ALL | filters.CAPTION,
                    self._handle_message,
                )
            )
            await self._app.initialize()
            await self._app.start()
            await self._app.updater.start_polling(drop_pending_updates=True)
            logging.info("Telegram bot polling started (allowed_ids=%s)", self.allowed_ids)
            # Keep running until the thread is killed
            stop_event = asyncio.Event()
            await stop_event.wait()

        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_run_async())

        t = threading.Thread(target=_run, name="telegram-bot", daemon=True)
        t.start()
        logging.info("Telegram bot thread started")
        return t
