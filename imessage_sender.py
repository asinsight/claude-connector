#!/usr/bin/env python3
"""
iMessage sender via AppleScript.
Handles chunking and special-character escaping.
"""

import subprocess
import logging
import time

MAX_CHUNK_SIZE = 1500
MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds between retries


def _escape_applescript(text: str) -> str:
    """
    Escape a string for safe embedding inside AppleScript double-quoted string literals.
    AppleScript recognises: backslash sequences and embedded return characters.
    """
    # Backslash must come first
    text = text.replace("\\", "\\\\")
    # Double-quote ends the string literal
    text = text.replace('"', '\\"')
    # Newlines → AppleScript 'return' character shorthand
    text = text.replace("\r\n", "\\r")
    text = text.replace("\n", "\\n")
    text = text.replace("\r", "\\r")
    return text


def _send_single_chunk(phone_number: str, chunk: str, attempt: int = 1) -> bool:
    """Send one chunk via AppleScript. Returns True on success."""
    escaped_msg = _escape_applescript(chunk)
    escaped_phone = _escape_applescript(phone_number)

    script = (
        'tell application "Messages"\n'
        f'    set targetBuddy to "{escaped_phone}"\n'
        '    set targetService to 1st account whose service type = iMessage\n'
        f'    send "{escaped_msg}" to participant targetBuddy of targetService\n'
        'end tell'
    )

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return True
        logging.warning(
            "Send failed (attempt %d/%d): %s",
            attempt, MAX_RETRIES, result.stderr.strip()[:200],
        )
    except subprocess.TimeoutExpired:
        logging.warning("Send timeout (attempt %d/%d)", attempt, MAX_RETRIES)
    except Exception as exc:
        logging.error("Send exception (attempt %d/%d): %s", attempt, MAX_RETRIES, exc)

    return False


def send_imessage(phone_number: str, message: str) -> None:
    """
    Send an iMessage to phone_number, splitting into chunks ≤ MAX_CHUNK_SIZE chars.
    Each chunk is retried up to MAX_RETRIES times before giving up.
    """
    if not message:
        return

    message = str(message)

    # Split on natural boundaries first, then hard-cut if needed
    chunks = []
    while len(message) > MAX_CHUNK_SIZE:
        split_at = message.rfind("\n", 0, MAX_CHUNK_SIZE)
        if split_at <= 0:
            split_at = message.rfind(" ", 0, MAX_CHUNK_SIZE)
        if split_at <= 0:
            split_at = MAX_CHUNK_SIZE
        chunks.append(message[:split_at])
        message = message[split_at:].lstrip("\n ")

    if message:
        chunks.append(message)

    total = len(chunks)
    for idx, chunk in enumerate(chunks, start=1):
        if total > 1:
            logging.info("Sending chunk %d/%d (%d chars)", idx, total, len(chunk))

        success = False
        for attempt in range(1, MAX_RETRIES + 1):
            if _send_single_chunk(phone_number, chunk, attempt):
                success = True
                break
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)

        if not success:
            logging.error("Chunk %d/%d failed after all retries", idx, total)

        if idx < total:
            time.sleep(0.5)  # brief inter-chunk pause
