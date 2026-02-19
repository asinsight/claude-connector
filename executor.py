#!/usr/bin/env python3
"""
Command executor.

Routing rules:
  /c !<cmd>       → BLOCKED_PATTERNS check → direct shell execution
  /c status       → handled by agent.py (returns None, None)
  /c <natural>    → BLOCKED_PATTERNS check → Claude Code (claude -p)

Security: two-layer deletion block
  1. Regex patterns on raw command text (catches shell direct execution)
  2. System prompt injected into every Claude Code call (catches AI-level attempts)
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from interactive import InteractiveSession

# ── Blocked patterns (file deletion) ─────────────────────────────────────────

BLOCKED_PATTERNS = [
    r"\brm\s",
    r"\brm$",
    r"\brmdir\b",
    r"\bunlink\b",
    r"\btrash\b",
    r"move\s+to\s+trash",
    r"shutil\.rmtree",
    r"os\.remove",
    r"os\.unlink",
    r"os\.rmdir",
    r"pathlib.*\.unlink",
    r"\bdelete\b.*file",
    r"find\s+.*-delete",
    r">\s*/dev/null",
    r"\btruncate\b",
]

_BLOCKED_RE = re.compile(
    "|".join(BLOCKED_PATTERNS), re.IGNORECASE
)

BLOCK_RESPONSE = "🚫 File deletion commands are blocked by security policy. Moving files is allowed."


def is_blocked(text: str) -> bool:
    """Return True if text matches any deletion-blocking pattern."""
    return bool(_BLOCKED_RE.search(text))


# ── System prompt for Claude Code ─────────────────────────────────────────────

def _build_system_prompt(agent_dir: Path) -> str:
    return f"""You are a Mac control agent. Execute commands received via iMessage.

Rules:
1. Never delete files or directories by any means — rm, rmdir, unlink, trash, shutil.rmtree, \
os.remove, os.rmdir, or any other method is strictly forbidden.
2. If the user requests deletion, respond exactly: \
"File deletion is blocked by security policy. Moving files is allowed."
3. Report results concisely. Start with ✅ on success, ❌ on failure.
4. To read the current browser page:
   python3 -c "import sys; sys.path.insert(0,'{agent_dir}'); \
from browser_helper import get_safari_page_text; print(get_safari_page_text())"
   Use get_chrome_page_text() for Chrome.
5. To fill a form field:
   python3 -c "import sys; sys.path.insert(0,'{agent_dir}'); \
from browser_helper import fill_form_field; print(fill_form_field('#selector','value'))"
6. When you need more information, respond with [NEED_INPUT:your question].
   Example: [NEED_INPUT:Which server? (1) dev-server (2) prod-server]
7. To send a file to the user: append [SEND_FILE:/absolute/path/to/file] at the end of your reply.
   Example: Here is the log file. [SEND_FILE:/tmp/output.log]
8. To send a screenshot: append [SEND_SCREENSHOT] at the end of your reply.
   To send a specific app's window: [SEND_SCREENSHOT:AppName]
   Example: Here's the current screen. [SEND_SCREENSHOT]
9. Respond in English.
10. Keep responses under 4000 characters.
11. Never echo or log sensitive information such as passwords.
"""


# ── Shell execution ───────────────────────────────────────────────────────────

def run_shell_command(command: str, timeout: int = 60) -> str:
    """Run a shell command and return combined stdout+stderr as a string."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        parts = []
        if result.stdout.strip():
            parts.append(result.stdout.strip())
        if result.stderr.strip():
            parts.append(f"[stderr]\n{result.stderr.strip()}")
        if result.returncode != 0:
            parts.insert(0, f"[exit code: {result.returncode}]")
        return "\n".join(parts) if parts else "(no output)"
    except subprocess.TimeoutExpired:
        return f"❌ Command timeout (>{timeout}s)"
    except Exception as exc:
        return f"❌ Execution error: {exc}"


# ── Claude Code invocation ────────────────────────────────────────────────────

def call_claude_code(prompt: str, config: dict, context_prefix: str = "") -> str:
    """
    Invoke `claude -p <prompt>` in non-interactive mode.
    If context_prefix is provided it is prepended to the prompt so Claude
    has access to past conversation history.
    Returns the plain-text response string.
    """
    agent_dir = Path.home() / ".imessage-agent"
    system_prompt = _build_system_prompt(agent_dir)
    timeout = config.get("claude_timeout", 300)

    full_prompt = f"{context_prefix}\n\n[Current request:]\n{prompt}" if context_prefix else prompt

    cmd = [
        "claude",
        "-p", full_prompt,
        "--allowedTools", "Bash,Read,Write,Edit,MultiEdit",
        "--output-format", "json",
        "--system-prompt", system_prompt,
    ]

    logging.info("Claude Code CMD: claude -p %r (timeout=%ds)", prompt[:80], timeout)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return (
            "❌ 'claude' command not found. "
            "Check that Claude Code is installed and on PATH."
        )
    except subprocess.TimeoutExpired:
        return f"❌ Claude Code timeout (>{timeout}s)"
    except Exception as exc:
        logging.error("Claude Code call exception: %s", exc, exc_info=True)
        return f"❌ Error: {exc}"

    logging.info("Claude Code finished rc=%d stdout=%d stderr=%d",
                 result.returncode, len(result.stdout), len(result.stderr))
    if result.stderr.strip():
        logging.info("Claude Code stderr: %s", result.stderr.strip()[:300])

    if result.returncode != 0:
        err = result.stderr.strip()
        logging.error("Claude Code error rc=%d: %s", result.returncode, err[:300])
        return f"❌ Claude Code error: {err[:500]}"

    raw = result.stdout.strip()
    if not raw:
        return "❌ Claude Code returned an empty response."

    # Parse JSON output
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            # Primary field
            if "result" in data:
                return str(data["result"])
            # Fallback: concatenate text content blocks
            if "content" in data:
                content = data["content"]
                if isinstance(content, list):
                    texts = [
                        block.get("text", "")
                        for block in content
                        if block.get("type") == "text"
                    ]
                    return "\n".join(texts)
                return str(content)
        return str(data)
    except json.JSONDecodeError:
        # If output isn't JSON (e.g. plain-text fallback), return as-is
        return raw


# ── Incoming file/attachment processing ──────────────────────────────────────

def process_incoming_file(
    attachments: list,
    user_text: str,
    config: dict,
    session: InteractiveSession | None = None,
) -> tuple[str | None, str | None]:
    """
    Process one or more received file attachments alongside an optional user text command.

    Each element of attachments is a dict with keys: path, type, name, size.
    Returns (response, question) like execute_command.
    """
    from file_handler import classify_file, copy_to_inbox, extract_pdf_text

    if not attachments:
        return "⚠️ No attachments found.", None

    max_file_mb = config.get("max_file_size_mb", 100)
    max_img_mb = config.get("max_image_size_mb", 20)
    results: list[str] = []

    for att in attachments:
        att_path = att.get("path", "")
        att_type = att.get("type", "")
        att_name = att.get("name", "") or os.path.basename(att_path)
        att_size = att.get("size") or 0

        # Size guard
        if att_size and att_size > max_file_mb * 1024 * 1024:
            results.append(
                f"⚠️ File too large: {att_name} "
                f"({att_size / 1024 / 1024:.1f} MB > {max_file_mb} MB limit)"
            )
            continue

        # Copy to inbox
        local_path = copy_to_inbox(att_path)
        if not local_path:
            results.append(f"⚠️ Could not access attachment: {att_name}")
            continue

        file_type = classify_file(local_path)
        logging.info("Incoming file: %s type=%s local=%s", att_name, file_type, local_path)

        if file_type == "image":
            size_mb = os.path.getsize(local_path) / 1024 / 1024
            if size_mb > max_img_mb:
                results.append(
                    f"⚠️ Image too large: {os.path.basename(local_path)} "
                    f"({size_mb:.1f} MB > {max_img_mb} MB)"
                )
                continue

            if config.get("vision_enabled", True) and config.get("anthropic_api_key", ""):
                from vision_analyzer import analyze_image_with_vision
                prompt = user_text if user_text else "Describe this image in detail."
                analysis = analyze_image_with_vision(local_path, prompt, config)
                results.append(f"🖼️ Image analysis:\n{analysis}")
            else:
                results.append(
                    f"📎 Image received: {os.path.basename(local_path)}\n"
                    "⚠️ Image analysis disabled. "
                    "Set anthropic_api_key in config.json to enable."
                )

        elif file_type == "text":
            prompt = (
                f"The user sent a file.\n"
                f"File path: {local_path}\n"
                f"User message: {user_text or '(analyze the file)'}\n\n"
                "Read the file and process the user's request."
            )
            results.append(call_claude_code(prompt, config))

        elif file_type == "document":
            ext = os.path.splitext(local_path)[1].lower()
            if ext == ".pdf":
                pdf_text = extract_pdf_text(local_path)
                prompt = (
                    f"The user sent a PDF.\n"
                    f"File path: {local_path}\n"
                    f"Extracted text:\n---\n{pdf_text}\n---\n"
                    f"User message: {user_text or 'Summarize this document'}\n\n"
                    "Analyze the text and process the user's request."
                )
                results.append(call_claude_code(prompt, config))
            else:
                # Try textutil for Word/Pages/etc.
                try:
                    tmp_txt = local_path + ".tmp.txt"
                    subprocess.run(
                        ["textutil", "-convert", "txt", "-output", tmp_txt, local_path],
                        capture_output=True, timeout=30,
                    )
                    if os.path.exists(tmp_txt):
                        with open(tmp_txt, "r", errors="ignore") as fh:
                            doc_text = fh.read(5000)
                        os.remove(tmp_txt)
                        prompt = (
                            f"The user sent a document: {os.path.basename(local_path)}\n"
                            f"Extracted text:\n---\n{doc_text}\n---\n"
                            f"User message: {user_text or 'Summarize this document'}"
                        )
                        results.append(call_claude_code(prompt, config))
                    else:
                        results.append(
                            f"📎 Document received: {os.path.basename(local_path)}\n"
                            "⚠️ Text extraction not supported for this format."
                        )
                except Exception as exc:
                    results.append(
                        f"📎 Document received: {os.path.basename(local_path)}\n"
                        f"⚠️ Text extraction failed: {exc}"
                    )

        else:  # binary / unknown
            results.append(
                f"📎 File received: {os.path.basename(local_path)} "
                f"({att_type or 'unknown type'})"
            )

    combined = "\n\n".join(results) if results else "⚠️ No files were processed."

    if session is not None:
        return session.process_response(combined)
    return combined, None


# ── Main routing entry point ──────────────────────────────────────────────────

def execute_command(
    command: str,
    config: dict,
    session: InteractiveSession | None = None,
    context_prefix: str = "",
) -> tuple[str | None, str | None]:
    """
    Route a command (everything after the /c prefix) and execute it.

    context_prefix: conversation history to inject into the Claude prompt.

    Returns:
        (response, question)
        question is None unless Claude signalled [NEED_INPUT:...]
        Both may be None for the 'status' keyword (handled by agent.py).
    """
    command = command.strip()

    # ── status: delegate to agent.py ─────────────────────────────────────────
    if command.lower() == "status":
        return None, None  # agent.py injects the stats response

    # ── !cmd: direct shell execution ─────────────────────────────────────────
    if command.startswith("!"):
        shell_cmd = command[1:].strip()
        if not shell_cmd:
            return "❌ Empty shell command.", None
        if is_blocked(shell_cmd):
            logging.warning("Blocked shell command: %s", shell_cmd[:120])
            return BLOCK_RESPONSE, None
        logging.info("Shell exec: %s", shell_cmd[:120])
        output = run_shell_command(shell_cmd, timeout=config.get("shell_timeout", 60))
        return f"```\n{output}\n```", None

    # ── natural language: check obvious delete patterns then call Claude ───────
    if is_blocked(command):
        logging.warning("Blocked natural-language command: %s", command[:120])
        return BLOCK_RESPONSE, None

    logging.info("Invoking Claude Code: %s", command[:120])
    raw_result = call_claude_code(command, config, context_prefix=context_prefix)

    if session is not None:
        return session.process_response(raw_result)

    return raw_result, None
