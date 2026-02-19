#!/usr/bin/env python3
"""
File handler: classify, copy to inbox, and extract text from received attachments.
Uses only macOS built-in tools (sips, textutil, mdimport, strings).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

AGENT_DIR = Path.home() / ".imessage-agent"
INBOX_DIR = AGENT_DIR / "inbox"

# ── File-type classification by extension ────────────────────────────────────

TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml",
    ".txt", ".md", ".csv", ".xml", ".html", ".css", ".sh",
    ".bash", ".zsh", ".conf", ".cfg", ".ini", ".log",
    ".sql", ".r", ".swift", ".kt", ".java", ".c", ".cpp",
    ".h", ".go", ".rs", ".rb", ".php", ".pl", ".lua",
}

IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".heic", ".heif",
    ".webp", ".bmp", ".tiff", ".tif", ".svg",
}

DOCUMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".rtf", ".pages", ".numbers", ".key",
}


def classify_file(filepath: str) -> str:
    """Return 'text', 'image', 'document', or 'binary'."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext in TEXT_EXTENSIONS:
        return "text"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in DOCUMENT_EXTENSIONS:
        return "document"
    return "binary"


# ── Inbox management ──────────────────────────────────────────────────────────

def copy_to_inbox(attachment_path: str) -> str | None:
    """
    Expand and copy an attachment to INBOX_DIR.
    Returns the local path on success, None if the source does not exist.
    Deduplicates filenames with a numeric suffix.
    """
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    src = Path(os.path.expanduser(attachment_path))
    if not src.exists():
        return None

    base, ext = os.path.splitext(src.name)
    dst = INBOX_DIR / src.name
    counter = 1
    while dst.exists():
        dst = INBOX_DIR / f"{base}_{counter}{ext}"
        counter += 1

    shutil.copy2(src, dst)
    return str(dst)


# ── PDF text extraction ───────────────────────────────────────────────────────

def extract_pdf_text(filepath: str, max_chars: int = 5000) -> str:
    """
    Extract readable text from a PDF using macOS built-in tools.
    Strategy: textutil → mdimport → strings (last resort).
    """
    # 1. textutil (most reliable for text-based PDFs)
    try:
        tmp_txt = filepath + ".tmp.txt"
        subprocess.run(
            ["textutil", "-convert", "txt", "-output", tmp_txt, filepath],
            capture_output=True, timeout=30,
        )
        if os.path.exists(tmp_txt):
            with open(tmp_txt, "r", errors="ignore") as fh:
                text = fh.read(max_chars)
            os.remove(tmp_txt)
            if text.strip():
                return text
    except Exception:
        pass

    # 2. mdimport (Spotlight indexer metadata)
    try:
        result = subprocess.run(
            ["mdimport", "-d2", filepath],
            capture_output=True, text=True, timeout=30,
        )
        if result.stdout.strip():
            return result.stdout[:max_chars]
    except Exception:
        pass

    # 3. strings (raw byte strings — last resort)
    try:
        result = subprocess.run(
            ["strings", filepath],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout[:max_chars]
    except Exception:
        pass

    return "[PDF text extraction failed]"


# ── HEIC conversion ───────────────────────────────────────────────────────────

def convert_heic_to_jpg(heic_path: str) -> str:
    """
    Convert HEIC/HEIF to JPEG using macOS sips.
    Returns the JPEG path on success, or the original path on failure.
    """
    jpg_path = os.path.splitext(heic_path)[0] + ".jpg"
    try:
        subprocess.run(
            ["sips", "-s", "format", "jpeg", heic_path, "--out", jpg_path],
            capture_output=True, timeout=30,
        )
        if os.path.exists(jpg_path):
            return jpg_path
    except Exception:
        pass
    return heic_path
