"""Unit tests for TelegramNotifier — no real HTTP calls."""
from __future__ import annotations

from unittest.mock import patch, MagicMock
import json

from deadline_tools.notifier import TelegramNotifier


def test_send_called_with_correct_payload():
    notifier = TelegramNotifier(token="fake-token", chat_id="12345")

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"ok": True}).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        result = notifier.warn("⚠️ test stall")

    assert result is True
    call_args = mock_open.call_args[0][0]
    body = json.loads(call_args.data)
    assert body["chat_id"] == "12345"
    assert "test stall" in body["text"]


def test_no_token_returns_false():
    """Empty token/chat_id with env vars cleared -> must return False."""
    with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}, clear=False):
        notifier = TelegramNotifier(token="", chat_id="")
    assert notifier.warn("test") is False


def test_api_error_returns_false():
    notifier = TelegramNotifier(token="fake-token", chat_id="12345")

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(
        {"ok": False, "description": "Unauthorized"}
    ).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = notifier.warn("test")

    assert result is False
