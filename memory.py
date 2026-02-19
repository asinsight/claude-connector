#!/usr/bin/env python3
"""
Conversation memory backed by SQLite.

Tables
------
conversations
    Current messages younger than 1 day.
    Columns: id, sender, role, content, created_at

daily_summaries
    One summary row per sender per calendar day (older than today).
    Columns: id, sender, summary_date, summary, created_at

conversation_archive
    Full message archive (never deleted). Written when conversations
    are summarised and removed from the live table.
    Columns: id, sender, role, content, original_date, original_time, archived_at
"""
from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

MEMORY_DB = Path.home() / ".imessage-agent" / "memory.db"


# ── Connection helper ─────────────────────────────────────────────────────────

def _conn(db_path: Path = MEMORY_DB) -> sqlite3.Connection:
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    return c


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db(db_path: Path = MEMORY_DB) -> None:
    """Create tables if they do not exist."""
    with _conn(db_path) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                sender     TEXT    NOT NULL,
                role       TEXT    NOT NULL,   -- 'user' | 'assistant'
                content    TEXT    NOT NULL,
                created_at TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS daily_summaries (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                sender       TEXT    NOT NULL,
                summary_date TEXT    NOT NULL,  -- YYYY-MM-DD
                summary      TEXT    NOT NULL,
                created_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
                UNIQUE(sender, summary_date)
            );

            CREATE TABLE IF NOT EXISTS conversation_archive (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                sender        TEXT    NOT NULL,
                role          TEXT    NOT NULL,
                content       TEXT    NOT NULL,
                original_date TEXT    NOT NULL,  -- YYYY-MM-DD
                original_time TEXT    NOT NULL,  -- HH:MM:SS
                archived_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            );
        """)
    logging.info("Memory DB initialised at %s", db_path)


# ── Write ─────────────────────────────────────────────────────────────────────

def save_message(sender: str, role: str, content: str,
                 db_path: Path = MEMORY_DB) -> None:
    """Append one turn to the live conversation table."""
    with _conn(db_path) as c:
        c.execute(
            "INSERT INTO conversations (sender, role, content) VALUES (?, ?, ?)",
            (sender, role, content),
        )


# ── Read ──────────────────────────────────────────────────────────────────────

def get_summaries(sender: str, db_path: Path = MEMORY_DB) -> list[dict]:
    """Return all daily summaries for sender, oldest first."""
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT summary_date, summary FROM daily_summaries "
            "WHERE sender = ? ORDER BY summary_date ASC",
            (sender,),
        ).fetchall()
    return [{"date": r["summary_date"], "summary": r["summary"]} for r in rows]


def get_today_messages(sender: str, db_path: Path = MEMORY_DB) -> list[dict]:
    """Return today's live messages for sender, oldest first."""
    today = datetime.now().strftime("%Y-%m-%d")
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT role, content FROM conversations "
            "WHERE sender = ? AND created_at >= ? ORDER BY id ASC",
            (sender, today + " 00:00:00"),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def build_context_prefix(sender: str, db_path: Path = MEMORY_DB) -> str:
    """
    Build a context block to prepend to the Claude prompt.
    Returns an empty string when there is no prior history.
    """
    summaries = get_summaries(sender, db_path)
    today_msgs = get_today_messages(sender, db_path)

    if not summaries and not today_msgs:
        return ""

    parts: list[str] = ["[Conversation history with this user:]"]

    if summaries:
        parts.append("--- Past summaries ---")
        for s in summaries:
            parts.append(f"{s['date']}: {s['summary']}")

    if today_msgs:
        parts.append("--- Today's conversation (so far) ---")
        for m in today_msgs:
            label = "User" if m["role"] == "user" else "Agent"
            # Truncate very long turns so we don't blow the context window
            snippet = m["content"][:600]
            if len(m["content"]) > 600:
                snippet += "…"
            parts.append(f"{label}: {snippet}")

    parts.append("--- End of history ---\n")
    return "\n".join(parts)


# ── Daily maintenance ─────────────────────────────────────────────────────────

def run_daily_maintenance(sender: str, config: dict,
                          db_path: Path = MEMORY_DB) -> None:
    """
    Archive conversations older than today and store a summary.
    Safe to call on every agent startup — exits immediately if nothing to do.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    cutoff = today + " 00:00:00"

    with _conn(db_path) as c:
        old_rows = c.execute(
            "SELECT id, role, content, created_at FROM conversations "
            "WHERE sender = ? AND created_at < ? ORDER BY id ASC",
            (sender, cutoff),
        ).fetchall()

    if not old_rows:
        return

    logging.info("Memory: %d old message(s) found for %s — archiving", len(old_rows), sender)

    # Group by calendar day
    by_date: dict[str, list] = {}
    for row in old_rows:
        day = row["created_at"][:10]
        by_date.setdefault(day, []).append(row)

    for day, rows in by_date.items():
        _archive_day(sender, day, rows, config, db_path)

    # Remove archived messages from the live table
    ids = [r["id"] for r in old_rows]
    ph = ",".join("?" * len(ids))
    with _conn(db_path) as c:
        c.execute(f"DELETE FROM conversations WHERE id IN ({ph})", ids)

    logging.info("Memory: archived %d message(s) across %d day(s)",
                 len(old_rows), len(by_date))


def _archive_day(sender: str, day: str, rows: list, config: dict,
                 db_path: Path) -> None:
    """Summarise one day, write to daily_summaries and conversation_archive."""
    # Build raw conversation text for summarisation
    lines = []
    for row in rows:
        label = "User" if row["role"] == "user" else "Agent"
        lines.append(f"{label}: {row['content']}")
    conversation_text = "\n".join(lines)

    summary = _summarise(conversation_text)
    logging.info("Memory: summary for %s on %s: %s", sender, day, summary[:80])

    with _conn(db_path) as c:
        # Upsert summary
        c.execute(
            "INSERT OR REPLACE INTO daily_summaries "
            "(sender, summary_date, summary) VALUES (?, ?, ?)",
            (sender, day, summary),
        )
        # Archive full rows
        for row in rows:
            dt = row["created_at"]          # "YYYY-MM-DD HH:MM:SS"
            orig_date = dt[:10]
            orig_time = dt[11:19] if len(dt) >= 19 else "00:00:00"
            c.execute(
                "INSERT INTO conversation_archive "
                "(sender, role, content, original_date, original_time) "
                "VALUES (?, ?, ?, ?, ?)",
                (sender, row["role"], row["content"], orig_date, orig_time),
            )


def _summarise(conversation_text: str) -> str:
    """Call `claude -p` to produce a short summary. Falls back to truncation."""
    prompt = (
        "Summarise the following conversation in 2-3 concise sentences. "
        "Focus on what was requested and what was accomplished. "
        "Do not include greetings or filler.\n\n"
        f"{conversation_text[:4000]}"
    )
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout.strip())
            if isinstance(data, dict):
                text = data.get("result") or data.get("content", "")
                if isinstance(text, list):
                    text = " ".join(
                        b.get("text", "") for b in text if b.get("type") == "text"
                    )
                if text:
                    return str(text).strip()
    except Exception as exc:
        logging.warning("Memory: summarisation failed (%s) — using truncation", exc)

    # Fallback: first 300 chars of raw conversation
    return conversation_text[:300] + ("…" if len(conversation_text) > 300 else "")
