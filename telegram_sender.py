#!/usr/bin/env python3
"""
Send files via Telegram Bot API.
Provides both async and sync wrappers for use from telegram_bot.py
and response_parser.py respectively.
"""
from __future__ import annotations

import asyncio
import logging
import os


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


async def send_file_telegram_async(bot, chat_id: int, file_path: str) -> tuple[bool, str]:
    """Send a file via Telegram Bot API. Returns (success, detail)."""
    abs_path = os.path.abspath(os.path.expanduser(file_path))
    if not os.path.exists(abs_path):
        return False, f"File not found: {abs_path}"
    try:
        ext = os.path.splitext(abs_path)[1].lower()
        with open(abs_path, "rb") as f:
            if ext in IMAGE_EXTS:
                await bot.send_photo(chat_id=chat_id, photo=f)
            else:
                await bot.send_document(chat_id=chat_id, document=f)
        logging.info("Telegram file sent: %s → chat %d", os.path.basename(abs_path), chat_id)
        return True, "sent"
    except Exception as exc:
        logging.error("Telegram file send error: %s", exc)
        return False, str(exc)


def send_file_telegram_sync(bot, chat_id: int, file_path: str) -> tuple[bool, str]:
    """
    Synchronous wrapper for sending files via Telegram.
    Used by response_parser.py which runs in a sync context.
    """
    try:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                send_file_telegram_async(bot, chat_id, file_path)
            )
        finally:
            loop.close()
    except Exception as exc:
        logging.error("Telegram sync file send error: %s", exc)
        return False, str(exc)
