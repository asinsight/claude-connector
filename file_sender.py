#!/usr/bin/env python3
"""
Send files and screenshots to an iMessage contact via AppleScript.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

AGENT_DIR = Path.home() / ".imessage-agent"
OUTBOX_DIR = AGENT_DIR / "outbox"


# ── AppleScript helpers ───────────────────────────────────────────────────────

def _escape_applescript(text: str) -> str:
    """Escape a string for safe embedding in an AppleScript double-quoted literal."""
    text = text.replace("\\", "\\\\")
    text = text.replace('"', '\\"')
    text = text.replace("\r\n", "\\r")
    text = text.replace("\n", "\\n")
    text = text.replace("\r", "\\r")
    return text


def _run_osascript(script: str, timeout: int = 30) -> tuple[str, str]:
    """Run an AppleScript snippet. Returns (stdout, stderr)."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return "", "AppleScript timed out"
    except Exception as exc:
        return "", str(exc)


# ── File sending ──────────────────────────────────────────────────────────────

def send_file_via_imessage(phone: str, file_path: str) -> tuple[bool, str]:
    """
    Send a file to phone via iMessage using AppleScript.
    Returns (success, detail_message).
    """
    abs_path = os.path.abspath(os.path.expanduser(file_path))
    if not os.path.exists(abs_path):
        return False, f"File not found: {abs_path}"

    escaped_phone = _escape_applescript(phone)
    # POSIX file path embedded in AppleScript string — only quotes need escaping
    escaped_path = abs_path.replace("\\", "\\\\").replace('"', '\\"')

    script = (
        'tell application "Messages"\n'
        '    set targetService to 1st account whose service type = iMessage\n'
        f'    set targetBuddy to participant "{escaped_phone}" of targetService\n'
        f'    send POSIX file "{escaped_path}" to targetBuddy\n'
        'end tell'
    )

    _, stderr = _run_osascript(script)
    if stderr:
        logging.warning("File send warning (%s): %s", os.path.basename(abs_path), stderr[:200])
        return False, stderr[:200]

    logging.info("File sent: %s → %s", os.path.basename(abs_path), phone)
    return True, "sent"


# ── Screenshots ───────────────────────────────────────────────────────────────

def take_screenshot(region: str | None = None) -> str | None:
    """
    Capture a screenshot to outbox/.
    region: optional 'x,y,width,height' string for a partial capture.
    Returns the file path on success, None on failure.
    """
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    filepath = str(OUTBOX_DIR / f"screenshot_{time.strftime('%Y%m%d_%H%M%S')}.png")

    cmd = ["screencapture", "-x"]
    if region:
        cmd += ["-R", region]
    cmd.append(filepath)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            logging.warning("screencapture failed rc=%d: %s", result.returncode, result.stderr[:200])
        if not os.path.exists(filepath):
            logging.warning("screencapture produced no file — Screen Recording permission may be needed")
            return None
        return filepath
    except Exception as exc:
        logging.error("screencapture exception: %s", exc)
        return None


def take_window_screenshot(app_name: str | None = None) -> str | None:
    """
    Optionally bring app_name to front, then capture the full screen.
    Returns the file path on success, None on failure.
    """
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    filepath = str(OUTBOX_DIR / f"screenshot_{time.strftime('%Y%m%d_%H%M%S')}.png")

    if app_name:
        _, err = _run_osascript(
            f'tell application "{_escape_applescript(app_name)}" to activate\ndelay 0.5'
        )
        if err:
            logging.warning("Failed to activate %s: %s", app_name, err[:200])

    try:
        result = subprocess.run(
            ["screencapture", "-x", filepath],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            logging.warning("screencapture failed rc=%d: %s", result.returncode, result.stderr[:200])
        if not os.path.exists(filepath):
            logging.warning("screencapture produced no file — Screen Recording permission may be needed")
            return None
        return filepath
    except Exception as exc:
        logging.error("screencapture exception: %s", exc)
        return None


# ── Outbox maintenance ────────────────────────────────────────────────────────

def cleanup_outbox(max_age_hours: int = 24) -> None:
    """
    Move outbox files older than max_age_hours to outbox/archive/.
    Files are never deleted — only archived.
    """
    if not OUTBOX_DIR.exists():
        return

    archive_dir = OUTBOX_DIR / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - max_age_hours * 3600

    for entry in OUTBOX_DIR.iterdir():
        if entry.is_file() and entry.stat().st_mtime < cutoff:
            shutil.move(str(entry), str(archive_dir / entry.name))
            logging.debug("Archived outbox file: %s", entry.name)
