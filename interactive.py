#!/usr/bin/env python3
"""
Interactive session management.

When Claude returns [NEED_INPUT:question], the agent enters a waiting state
and the next iMessage from the user is treated as the answer rather than a
new /c command.
"""
from __future__ import annotations

import re
import time
import logging


class InteractiveSession:
    """Tracks one multi-turn conversation with a single sender."""

    # Seconds before an unanswered [NEED_INPUT] is abandoned
    TIMEOUT = 300

    def __init__(self):
        self.waiting_for_reply: bool = False
        self.original_prompt: str | None = None
        self.conversation_history: list[dict] = []  # {"role": "assistant"|"user", "content": str}
        self.wait_start: float | None = None

    # ── Response processing ───────────────────────────────────────────────────

    def process_response(self, response: str) -> tuple[str, str | None]:
        """
        Scan Claude's response for [NEED_INPUT:question].

        Returns:
            (clean_response, question)  – question is None when no input needed.
        """
        if not response:
            return response, None

        match = re.search(r"\[NEED_INPUT:(.*?)\]", response, re.DOTALL)
        if match:
            question = match.group(1).strip()
            clean = re.sub(r"\[NEED_INPUT:.*?\]", "", response, flags=re.DOTALL).strip()
            logging.info("Entering interactive mode: %s", question[:80])
            return clean, question

        return response, None

    # ── Follow-up construction ────────────────────────────────────────────────

    def build_followup_prompt(self, user_reply: str) -> str:
        """
        Build a follow-up prompt that includes the original request and the
        full conversation history, then appends the user's latest reply.

        Side-effects: appends user reply to history, clears waiting state.
        """
        self.conversation_history.append({"role": "user", "content": user_reply})
        self.waiting_for_reply = False
        self.wait_start = None

        parts = [f"Original request: {self.original_prompt}", "Conversation history:"]
        for turn in self.conversation_history:
            label = "agent" if turn["role"] == "assistant" else "user"
            parts.append(f"  {label}: {turn['content']}")

        parts.append(f"\nThe user replied: '{user_reply}'. Continue the task.")
        return "\n".join(parts)

    def record_assistant_turn(self, content: str) -> None:
        """Save an assistant turn to history (call before waiting for reply)."""
        self.conversation_history.append({"role": "assistant", "content": content})

    # ── State helpers ─────────────────────────────────────────────────────────

    def start_waiting(self) -> None:
        """Mark session as waiting for the user's next reply."""
        self.waiting_for_reply = True
        self.wait_start = time.time()

    def is_timed_out(self) -> bool:
        """Return True (and reset waiting state) if the timeout has elapsed."""
        if self.wait_start is None:
            return False
        if time.time() - self.wait_start > self.TIMEOUT:
            self.waiting_for_reply = False
            self.wait_start = None
            logging.info("Interactive session timed out")
            return True
        return False

    def reset(self) -> None:
        """Fully reset the session (conversation finished)."""
        self.__init__()
