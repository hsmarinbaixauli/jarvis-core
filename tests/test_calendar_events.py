"""Unit tests for jarvis/gcalendar/events.py.

All tests use mocks — no real Google API calls are made.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from jarvis.gcalendar.events import create_event, get_upcoming_events

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(summary: str, start_dt: str) -> dict[str, Any]:
    """Build a minimal event dict that matches the Calendar API shape."""
    return {
        "id": "abc123",
        "summary": summary,
        "start": {"dateTime": start_dt, "timeZone": "UTC"},
        "end": {"dateTime": start_dt, "timeZone": "UTC"},
    }


# ---------------------------------------------------------------------------
# get_upcoming_events
# ---------------------------------------------------------------------------


class TestGetUpcomingEvents:
    def test_returns_formatted_event_list(self, mock_calendar_service: MagicMock) -> None:
        """get_upcoming_events returns a list of event dicts from the API."""
        fake_events: list[dict[str, Any]] = [
            _make_event("Team standup", "2026-05-07T09:00:00+00:00"),
            _make_event("Lunch break", "2026-05-07T12:00:00+00:00"),
        ]
        mock_calendar_service.events.return_value.list.return_value.execute.return_value = {"items": fake_events}

        result: list[dict[str, Any]] = get_upcoming_events(mock_calendar_service, days=7)

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["summary"] == "Team standup"
        assert result[1]["summary"] == "Lunch break"

    def test_returns_empty_list_when_no_events(self, mock_calendar_service: MagicMock) -> None:
        """get_upcoming_events returns an empty list when the API returns no items."""
        mock_calendar_service.events.return_value.list.return_value.execute.return_value = {"items": []}

        result: list[dict[str, Any]] = get_upcoming_events(mock_calendar_service, days=7)

        assert result == []

    def test_paginates_until_cap(self, mock_calendar_service: MagicMock) -> None:
        """get_upcoming_events follows nextPageToken until the hard cap is reached."""
        page1_events: list[dict[str, Any]] = [_make_event(f"Event {i}", "2026-05-07T09:00:00+00:00") for i in range(3)]
        page2_events: list[dict[str, Any]] = [
            _make_event(f"Event {i}", "2026-05-08T09:00:00+00:00") for i in range(3, 5)
        ]

        execute_mock: MagicMock = mock_calendar_service.events.return_value.list.return_value.execute
        execute_mock.side_effect = [
            {"items": page1_events, "nextPageToken": "tok1"},
            {"items": page2_events},
        ]

        result: list[dict[str, Any]] = get_upcoming_events(mock_calendar_service, days=7)

        assert len(result) == 5


# ---------------------------------------------------------------------------
# create_event
# ---------------------------------------------------------------------------


class TestCreateEvent:
    def test_calls_insert_with_correct_body(self, mock_calendar_service: MagicMock) -> None:
        """create_event calls the Calendar API insert with the right body fields."""
        start: datetime = datetime(2026, 5, 10, 10, 0, tzinfo=UTC)
        end: datetime = datetime(2026, 5, 10, 11, 0, tzinfo=UTC)

        create_event(mock_calendar_service, title="Doctor visit", start_datetime=start, end_datetime=end)

        insert_call = mock_calendar_service.events.return_value.insert
        insert_call.assert_called_once()
        _, kwargs = insert_call.call_args
        body: dict[str, Any] = kwargs["body"]
        assert body["summary"] == "Doctor visit"
        assert "dateTime" in body["start"]
        assert "dateTime" in body["end"]

    def test_raises_value_error_when_end_before_start(self, mock_calendar_service: MagicMock) -> None:
        """create_event raises ValueError when end_datetime <= start_datetime."""
        start: datetime = datetime(2026, 5, 10, 11, 0, tzinfo=UTC)
        end: datetime = datetime(2026, 5, 10, 10, 0, tzinfo=UTC)  # before start

        with pytest.raises(ValueError, match="end_datetime must be after start_datetime"):
            create_event(mock_calendar_service, title="Bad event", start_datetime=start, end_datetime=end)

    def test_raises_value_error_when_end_equals_start(self, mock_calendar_service: MagicMock) -> None:
        """create_event raises ValueError when end_datetime == start_datetime."""
        start: datetime = datetime(2026, 5, 10, 10, 0, tzinfo=UTC)

        with pytest.raises(ValueError):
            create_event(mock_calendar_service, title="Zero-duration", start_datetime=start, end_datetime=start)
