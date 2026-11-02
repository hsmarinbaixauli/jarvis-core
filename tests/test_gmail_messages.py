"""Unit tests for jarvis/gmail/messages.py.

All tests use mocks — no real Gmail API calls are made.
"""

from __future__ import annotations

import base64
from typing import Any
from unittest.mock import MagicMock

from jarvis.gmail.messages import (
    _sender_email_cache,
    get_unread_messages,
    mark_as_read,
    send_reply,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_list_result(ids: list[str], next_page_token: str | None = None) -> dict[str, Any]:
    """Build a minimal messages.list() API response."""
    result: dict[str, Any] = {"messages": [{"id": mid} for mid in ids]}
    if next_page_token:
        result["nextPageToken"] = next_page_token
    return result


def _make_message_meta(msg_id: str, sender: str = "sender@example.com") -> dict[str, Any]:
    """Build a minimal messages.get(format='metadata') response."""
    return {
        "id": msg_id,
        "threadId": f"thread-{msg_id}",
        "snippet": "Hello there",
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Test subject"},
                {"name": "From", "value": sender},
                {"name": "Date", "value": "Wed, 6 May 2026 10:00:00 +0000"},
            ]
        },
    }


def _make_message_full(msg_id: str) -> dict[str, Any]:
    """Build a minimal messages.get(format='full') response."""
    return {
        "id": msg_id,
        "threadId": f"thread-{msg_id}",
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": base64.urlsafe_b64encode(b"Original body").decode("utf-8")},
            "headers": [
                {"name": "Subject", "value": "Hello"},
                {"name": "From", "value": "sender@example.com"},
                {"name": "To", "value": "me@example.com"},
                {"name": "Date", "value": "Wed, 6 May 2026 10:00:00 +0000"},
                {"name": "Message-ID", "value": "<original@example.com>"},
                {"name": "References", "value": ""},
            ],
        },
    }


# ---------------------------------------------------------------------------
# get_unread_messages
# ---------------------------------------------------------------------------


class TestGetUnreadMessages:
    def test_returns_message_dicts(self, mock_gmail_service: MagicMock) -> None:
        """get_unread_messages returns normalised message dicts from the mock API."""
        mock_gmail_service.users.return_value.messages.return_value.list.return_value.execute.return_value = (
            _make_list_result(["id1", "id2"])
        )
        mock_gmail_service.users.return_value.messages.return_value.get.return_value.execute.side_effect = [
            _make_message_meta("id1"),
            _make_message_meta("id2", sender="other@example.com"),
        ]

        result: list[dict[str, Any]] = get_unread_messages(mock_gmail_service, max_results=10)

        assert len(result) == 2
        assert result[0]["id"] == "id1"
        assert result[0]["subject"] == "Test subject"
        assert result[0]["sender"] == "sender@example.com"
        assert result[1]["sender"] == "other@example.com"

    def test_returns_empty_list_when_no_messages(self, mock_gmail_service: MagicMock) -> None:
        """get_unread_messages returns an empty list when the inbox has no unread messages."""
        mock_gmail_service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
            "messages": []
        }

        result: list[dict[str, Any]] = get_unread_messages(mock_gmail_service, max_results=10)

        assert result == []

    def test_paginates_across_pages(self, mock_gmail_service: MagicMock) -> None:
        """get_unread_messages follows nextPageToken until the cap is met."""
        list_mock = mock_gmail_service.users.return_value.messages.return_value.list
        list_mock.return_value.execute.side_effect = [
            _make_list_result(["id1"], next_page_token="tok1"),
            _make_list_result(["id2"]),
        ]
        mock_gmail_service.users.return_value.messages.return_value.get.return_value.execute.side_effect = [
            _make_message_meta("id1"),
            _make_message_meta("id2"),
        ]

        result: list[dict[str, Any]] = get_unread_messages(mock_gmail_service, max_results=10)

        assert len(result) == 2


# ---------------------------------------------------------------------------
# send_reply
# ---------------------------------------------------------------------------


class TestSendReply:
    def test_calls_send_with_base64_raw(self, mock_gmail_service: MagicMock) -> None:
        """send_reply calls the Gmail API send with a base64-encoded raw message."""
        mock_gmail_service.users.return_value.messages.return_value.get.return_value.execute.return_value = (
            _make_message_full("id1")
        )
        mock_gmail_service.users.return_value.messages.return_value.send.return_value.execute.return_value = {
            "id": "sent001"
        }

        # Clear cache so the profile call is made fresh.
        _sender_email_cache.clear()

        send_reply(mock_gmail_service, "id1", "Thanks!")

        send_call = mock_gmail_service.users.return_value.messages.return_value.send
        send_call.assert_called_once()
        _, kwargs = send_call.call_args
        body: dict[str, Any] = kwargs["body"]
        assert "raw" in body
        # Verify the raw value is valid base64url by decoding it.
        decoded: bytes = base64.urlsafe_b64decode(body["raw"] + "==")
        assert b"Thanks!" in decoded

    def test_from_header_is_set(self, mock_gmail_service: MagicMock) -> None:
        """send_reply includes the From header in the outgoing message."""
        mock_gmail_service.users.return_value.messages.return_value.get.return_value.execute.return_value = (
            _make_message_full("id2")
        )
        mock_gmail_service.users.return_value.getProfile.return_value.execute.return_value = {
            "emailAddress": "myaccount@example.com"
        }

        _sender_email_cache.clear()

        send_reply(mock_gmail_service, "id2", "Reply body")

        send_call = mock_gmail_service.users.return_value.messages.return_value.send
        _, kwargs = send_call.call_args
        raw_bytes: bytes = base64.urlsafe_b64decode(kwargs["body"]["raw"] + "==")
        assert b"myaccount@example.com" in raw_bytes


# ---------------------------------------------------------------------------
# mark_as_read
# ---------------------------------------------------------------------------


class TestMarkAsRead:
    def test_calls_modify_with_remove_unread(self, mock_gmail_service: MagicMock) -> None:
        """mark_as_read calls modify with removeLabelIds containing 'UNREAD'."""
        mark_as_read(mock_gmail_service, "id99")

        modify_call = mock_gmail_service.users.return_value.messages.return_value.modify
        modify_call.assert_called_once_with(
            userId="me",
            id="id99",
            body={"removeLabelIds": ["UNREAD"]},
        )
