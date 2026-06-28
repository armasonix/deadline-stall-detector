"""Telegram notifier - thin wrapper around the Bot API sendMessage endpoint.

Reads credentials from environment variables:
    TELEGRAM_BOT_TOKEN  - bot token from @BotFather
    TELEGRAM_CHAT_ID    - target chat or channel ID

Both levels route through the same _send() method.
Failures are logged and swallowed so a Telegram outage never kills the monitor.
"""
from __future__ import annotations

import logging
import os
import urllib.request
import urllib.parse
import json
from typing import Optional

log = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    def __init__(
        self,
        token: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> None:
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")

        if not self.token or not self.chat_id:
            log.warning(
                "TelegramNotifier: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set. "
                "Notifications will be skipped."
            )

    # -- public ---------------------------------------------------------------

    def warn(self, message: str) -> None:
        """Send a warning-level notification (yellow icon)."""
        self._send(f"[WARNING] {message}")

    def critical(self, message: str) -> None:
        """Send a critical-level notification (red icon)."""
        self._send(f"[CRITICAL] {message}")

    def info(self, message: str) -> None:
        """Send an informational notification."""
        self._send(f"[INFO] {message}")

    # -- private --------------------------------------------------------------

    def _send(self, text: str) -> None:
        if not self.token or not self.chat_id:
            log.debug("Notification skipped (no credentials): %s", text)
            return

        url = _TELEGRAM_API.format(token=self.token)
        payload = json.dumps({
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
        }).encode()

        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status != 200:
                    log.error("Telegram API returned %d", resp.status)
        except OSError as exc:
            log.error("Failed to send Telegram notification: %s", exc)
