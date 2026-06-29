"""Telegram notifications for stall events.

Token and chat_id are read from environment variables - never hardcoded.

Environment variables:
    TELEGRAM_BOT_TOKEN    - bot token from @BotFather
    TELEGRAM_CHAT_ID      - numeric chat/channel id
    TELEGRAM_PROXY        - optional proxy URL:
                              http://host:port
                              http://user:pass@host:port
                              socks4://host:port
                              socks5://host:port
                              socks5h://host:port    (DNS via proxy)
    TELEGRAM_TIMEOUT_SEC  - request timeout (default 20)

If variables are not set, notifications are silently skipped (warn logged).
"""
from __future__ import annotations

import logging
import os
from typing import Literal

import requests

log = logging.getLogger(__name__)

ParseMode = Literal["Markdown", "HTML", "MarkdownV2"]


class TelegramNotifier:
    API_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        parse_mode: ParseMode = "Markdown",
        timeout: int | None = None,
    ) -> None:
        self._token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        self._parse_mode = parse_mode
        self._timeout = timeout or int(os.environ.get("TELEGRAM_TIMEOUT_SEC", "20"))
        self._proxy = os.environ.get("TELEGRAM_PROXY", "").strip()

        if not self._token or not self._chat_id:
            log.warning(
                "TelegramNotifier: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set. "
                "Notifications disabled."
            )
            self._enabled = False
            return

        self._enabled = True
        token_preview = self._token[:8] + "..." if len(self._token) > 8 else "?"
        log.info(
            "TelegramNotifier: enabled (token=%s chat_id=%s proxy=%s timeout=%ss)",
            token_preview, self._chat_id, self._proxy or "none", self._timeout,
        )

        self._proxies = (
            {"http": self._proxy, "https": self._proxy} if self._proxy else None
        )

    def warn(self, text: str) -> bool:
        return self._send(text)

    def critical(self, text: str) -> bool:
        return self._send(text)

    def info(self, text: str) -> bool:
        return self._send(text)

    def _send(self, text: str) -> bool:
        if not self._enabled:
            return False

        url = self.API_URL.format(token=self._token)
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": self._parse_mode,
            "disable_web_page_preview": True,
        }

        try:
            resp = requests.post(
                url,
                json=payload,
                proxies=self._proxies,
                timeout=self._timeout,
            )
        except requests.exceptions.ProxyError as exc:
            log.error("Telegram proxy error: %s (proxy=%s)", exc, self._proxy)
            return False
        except requests.exceptions.Timeout:
            log.error("Telegram timeout after %ss (proxy=%s)", self._timeout, self._proxy or "none")
            return False
        except Exception as exc:
            log.error("Telegram send failed: %s", exc)
            return False

        if resp.status_code != 200:
            log.error("Telegram HTTP %s: %s", resp.status_code, resp.text[:300])
            return False

        result = resp.json()
        if not result.get("ok"):
            log.error("Telegram API error: %s", result)
            return False

        log.info("Telegram message delivered (len=%d)", len(text))
        return True
