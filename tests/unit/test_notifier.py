"""Unit tests for TelegramNotifier - no real HTTP calls."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

from deadline_tools.notifier import TelegramNotifier


def _ok_response():
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"ok": True}
    return resp


def test_send_called_with_correct_payload():
    notifier = TelegramNotifier(token="fake-token", chat_id="12345")

    with patch("deadline_tools.notifier.requests.post",
               return_value=_ok_response()) as mock_post:
        result = notifier.warn("warn test stall")

    assert result is True
    # payload is passed as json=...
    _, kwargs = mock_post.call_args
    body = kwargs["json"]
    assert body["chat_id"] == "12345"
    assert "test stall" in body["text"]


def test_no_token_returns_false():
    """Empty token/chat_id with env vars cleared -> must return False."""
    with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}, clear=False):
        notifier = TelegramNotifier(token="", chat_id="")
    assert notifier.warn("test") is False


def test_api_error_returns_false():
    notifier = TelegramNotifier(token="fake-token", chat_id="12345")

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"ok": False, "description": "Unauthorized"}

    with patch("deadline_tools.notifier.requests.post", return_value=resp):
        result = notifier.warn("test")

    assert result is False


def test_http_error_returns_false():
    notifier = TelegramNotifier(token="fake-token", chat_id="12345")

    resp = MagicMock()
    resp.status_code = 401
    resp.text = "Unauthorized"

    with patch("deadline_tools.notifier.requests.post", return_value=resp):
        result = notifier.warn("test")

    assert result is False
