#!/usr/bin/env python3
"""
Parse Claude response directives and execute the corresponding side-effects.

Recognised directives:
  [SEND_SCREENSHOT]          – capture full screen and send via iMessage
  [SEND_SCREENSHOT:AppName]  – activate AppName, capture, and send
  [SEND_FILE:/abs/path]      – send the file at the given path

Directives are replaced with short status strings in the returned text.
[NEED_INPUT:...] is intentionally left for interactive.py to handle.
"""

import logging
import os
import re

from file_sender import take_screenshot, take_window_screenshot


def parse_and_execute_response(
    response_text: str,
    phone: str,
    config: dict,
    send_file_fn=None,
) -> tuple[str, list[str]]:
    """
    Scan response_text for [SEND_*] directives, execute them, and replace
    each directive with a short status string.

    Returns:
        (cleaned_response, list_of_sent_filenames)
    """
    if send_file_fn is None:
        from file_sender import send_file_via_imessage
        send_file_fn = send_file_via_imessage

    result = response_text
    files_sent: list[str] = []

    # ── [SEND_SCREENSHOT] / [SEND_SCREENSHOT:AppName] ────────────────────────
    # Collect all matches first (indices shift after replacements)
    for m in list(re.finditer(r"\[SEND_SCREENSHOT(?::([^\]]*))?\]", result)):
        tag = m.group(0)
        app_name = m.group(1).strip() if m.group(1) else None

        filepath = take_window_screenshot(app_name) if app_name else take_screenshot()

        if filepath:
            success, detail = send_file_fn(phone, filepath)
            if success:
                files_sent.append(os.path.basename(filepath))
                replacement = "📸 Screenshot sent"
            else:
                replacement = f"⚠️ Screenshot send failed: {detail}"
        else:
            replacement = "⚠️ Screenshot capture failed"

        result = result.replace(tag, replacement, 1)
        logging.info("SEND_SCREENSHOT%s → %s", f":{app_name}" if app_name else "", replacement)

    # ── [SEND_FILE:/path/to/file] ─────────────────────────────────────────────
    for m in list(re.finditer(r"\[SEND_FILE:([^\]]+)\]", result)):
        tag = m.group(0)
        raw_path = m.group(1).strip()
        expanded = os.path.expanduser(raw_path)

        if os.path.exists(expanded):
            success, detail = send_file_fn(phone, expanded)
            if success:
                files_sent.append(os.path.basename(expanded))
                replacement = f"📎 {os.path.basename(expanded)} sent"
            else:
                replacement = f"⚠️ File send failed: {detail}"
        else:
            replacement = f"⚠️ File not found: {raw_path}"

        result = result.replace(tag, replacement, 1)
        logging.info("SEND_FILE:%s → %s", raw_path, replacement)

    return result, files_sent
