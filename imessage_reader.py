#!/usr/bin/env python3
"""
iMessage DB reader.
Reads new messages (text + attachments) from ~/Library/Messages/chat.db
in read-only mode.
"""

import sqlite3
import logging
from pathlib import Path

CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"


def get_new_messages(allowed_phone, last_rowid: int) -> list:
    """
    Query chat.db for new inbound messages from allowed_phone with ROWID > last_rowid.

    allowed_phone: str or list[str] — one or more handles (phone numbers or iCloud emails).

    Each returned dict has the shape:
        {
            "rowid":       int,
            "text":        str | None,
            "timestamp":   float,
            "sender":      str,
            "attachments": [{"path": str, "type": str, "name": str, "size": int}, ...]
        }

    A single message may have multiple attachments; these are grouped here so
    callers receive one dict per logical message.

    Raises PermissionError if Full Disk Access is not granted.
    """
    if not CHAT_DB.exists():
        raise FileNotFoundError(
            f"chat.db not found at {CHAT_DB}\n"
            "Full Disk Access permission is required.\n"
            "Go to System Preferences → Security & Privacy → Full Disk Access and add Terminal."
        )

    db_uri = f"file:{CHAT_DB}?mode=ro"
    conn = None
    try:
        # isolation_level=None → autocommit: each query starts a fresh read
        # transaction and always sees the latest committed WAL state.
        # PRAGMA journal_mode is intentionally omitted — chat.db is already
        # in WAL mode and setting it on a read-only connection can interfere
        # with snapshot visibility.
        conn = sqlite3.connect(db_uri, uri=True, check_same_thread=False,
                               isolation_level=None)
        conn.execute("PRAGMA busy_timeout=5000;")   # wait up to 5s if locked
        cursor = conn.cursor()

        # LEFT JOIN attachment tables so messages with no attachments still appear.
        # Include messages where text IS NULL only when there is at least one attachment.
        # Support single string or list of handles
        if isinstance(allowed_phone, list):
            handles = allowed_phone
        else:
            handles = [allowed_phone]
        placeholders = ",".join("?" * len(handles))

        query = f"""
            SELECT
                message.ROWID,
                message.text,
                message.date / 1000000000 + 978307200  AS unix_timestamp,
                handle.id                               AS sender,
                attachment.filename                     AS attachment_path,
                attachment.mime_type                    AS attachment_type,
                attachment.transfer_name                AS attachment_name,
                attachment.total_bytes                  AS attachment_size,
                message.is_from_me
            FROM message
            LEFT JOIN handle
                ON message.handle_id = handle.ROWID
            LEFT JOIN message_attachment_join
                ON message.ROWID = message_attachment_join.message_id
            LEFT JOIN attachment
                ON message_attachment_join.attachment_id = attachment.ROWID
            WHERE message.ROWID > ?
              AND handle.id IN ({placeholders})
              AND (message.text IS NOT NULL OR attachment.ROWID IS NOT NULL)
            ORDER BY message.ROWID ASC
        """
        cursor.execute(query, (last_rowid, *handles))
        rows = cursor.fetchall()

    except sqlite3.OperationalError as exc:
        err = str(exc).lower()
        if "unable to open" in err or "disk" in err or "permission" in err:
            raise PermissionError(
                f"chat.db access error: {exc}\n"
                "Please check Full Disk Access permission."
            )
        raise
    finally:
        if conn:
            conn.close()

    # Group rows by ROWID — one message may produce multiple rows when it has
    # multiple attachments.  Preserve insertion order (Python 3.7+ dicts).
    messages: dict[int, dict] = {}
    for row in rows:
        rowid = row[0]
        if rowid not in messages:
            messages[rowid] = {
                "rowid":       rowid,
                "text":        row[1],
                "timestamp":   row[2],
                "sender":      row[3],
                "is_from_me":  row[8],
                "attachments": [],
            }
        if row[4]:  # attachment_path is not NULL
            messages[rowid]["attachments"].append({
                "path": row[4],
                "type": row[5],
                "name": row[6],
                "size": row[7],
            })

    return list(messages.values())
