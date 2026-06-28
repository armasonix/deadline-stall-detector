"""Telegram notifications for stall events.

Token and chat_id are read from environment variables — never hardcoded.

Environment variables required:
    TELEGRAM_BOT_TOKEN  — bot token from @BotFather
    TELEGRAM_CHAT_ID    — numeric chat/channel id

If variables are not set, notifications are silently skipped (warn logged).
"""
from __future__ import annotations

import logging
import os
import urllib.request
import urllib.parse
import json
from typing import Literal

log = logging.getLogger(__name__)

ParseMode = Literal["Markdown", "HTML", "MarkdownV2"]


class TelegramNotifier:
    API_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        parse_mode: ParseMode = "Markdown",
        timeout: int = 10,
    ) -> None:
        self._token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        self._parse_mode = parse_mode
        self._timeout = timeout

        if not self._token or not self._chat_id:
            log.warning(
                "TelegramNotifier: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set. "
                "Notifications disabled."
            )

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def warn(self, text: str) -> bool:
        """Send a warning-level message."""
        return self._send(text)

    def critical(self, text: str) -> bool:
        """Send a critical-level message."""
        return self._send(text)

    def info(self, text: str) -> bool:
        return self._send(text)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _send(self, text: str) -> bool:
        """POST to Telegram sendMessage. Returns True on success."""
        if not self._token or not self._chat_id:
            return False

        url = self.API_URL.format(token=self._token)
        payload = json.dumps(
            {
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": self._parse_mode,
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                result = json.loads(resp.read())
                if not result.get("ok"):
                    log.error("Telegram API error: %s", result)
                    return False
                return True
        except Exception as exc:
            log.error("Telegram send failed: %s", exc)
            return False
