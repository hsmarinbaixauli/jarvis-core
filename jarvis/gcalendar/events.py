"""Google Calendar event retrieval and creation.

Provides thin, typed wrappers around the Google Calendar API v3 events
resource for the three most common operations Jarvis needs:

- Listing today's events.
- Listing upcoming events within a rolling window.
- Creating a new event on the primary calendar.

All datetime values passed to the API are RFC3339 strings that include a
UTC-offset so the API can interpret them correctly regardless of the server
timezone.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, tzinfo
from typing import Any

from googleapiclient.discovery import Resource
from tzlocal import get_localzone

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_today_events(service: Resource) -> list[dict[str, Any]]:
    """Return all events scheduled for today on the primary calendar.

    "Today" spans from 00:00:00 to 23:59:59 in the local system timezone.
    All-day events (which have a ``date`` field rather than ``dateTime``) are
    included because ``singleEvents=True`` expands recurring instances and the
    API matches the date range against all-day events as well.

    Args:
        service: An authenticated Google Calendar API service object as
            returned by :func:`jarvis.gcalendar.auth.get_calendar_service`.

    Returns:
        A list of event dicts as returned by the Google Calendar API.  Each
        dict contains at minimum the keys ``id``, ``summary``, and ``start``.
        Returns an empty list when no events exist for today.
    """
    local_tz: tzinfo = _local_timezone()
    now: datetime = datetime.now(tz=local_tz)

    time_min: datetime = datetime.combine(now.date(), time.min).replace(tzinfo=local_tz)
    time_max: datetime = datetime.combine(now.date(), time.max).replace(tzinfo=local_tz)

    return _list_events(service, time_min, time_max)


def get_upcoming_events(service: Resource, days: int = 7) -> list[dict[str, Any]]:
    """Return events from now until *days* days ahead on the primary calendar.

    Args:
        service: An authenticated Google Calendar API service object as
            returned by :func:`jarvis.gcalendar.auth.get_calendar_service`.
        days: Number of days ahead to include, counting from the current
            moment.  Defaults to ``7``.

    Returns:
        A list of event dicts ordered by start time.  Returns an empty list
        when no events exist in the window.
    """
    local_tz: tzinfo = _local_timezone()
    time_min: datetime = datetime.now(tz=local_tz)
    time_max: datetime = time_min + timedelta(days=days)

    return _list_events(service, time_min, time_max)


def create_event(
    service: Resource,
    title: str,
    start_datetime: datetime,
    end_datetime: datetime,
    description: str = "",
) -> dict[str, Any]:
    """Create a new event on the primary calendar.

    Args:
        service: An authenticated Google Calendar API service object as
            returned by :func:`jarvis.gcalendar.auth.get_calendar_service`.
        title: The event summary / title shown in the calendar UI.
        start_datetime: A timezone-aware :class:`~datetime.datetime` for the
            event start.  If a naive datetime is supplied it is assumed to be
            in the local system timezone.
        end_datetime: A timezone-aware :class:`~datetime.datetime` for the
            event end.  Same naive-datetime handling as *start_datetime*.
        description: Optional free-text description for the event body.
            Defaults to an empty string (no description).

    Returns:
        The full event dict returned by the Google Calendar API after the
        event has been created, including the server-assigned ``id``,
        ``htmlLink``, and all other fields.
    """
    local_tz: tzinfo = _local_timezone()

    # Ensure both datetimes are timezone-aware before converting to RFC3339.
    if start_datetime.tzinfo is None:
        start_datetime = start_datetime.replace(tzinfo=local_tz)
    if end_datetime.tzinfo is None:
        end_datetime = end_datetime.replace(tzinfo=local_tz)

    if end_datetime <= start_datetime:
        raise ValueError("end_datetime must be after start_datetime.")

    event_body: dict[str, Any] = {
        "summary": title,
        "description": description,
        "start": {
            "dateTime": _to_rfc3339(start_datetime),
            "timeZone": _tz_name(start_datetime),
        },
        "end": {
            "dateTime": _to_rfc3339(end_datetime),
            "timeZone": _tz_name(end_datetime),
        },
    }

    created_event: dict[str, Any] = service.events().insert(calendarId="primary", body=event_body).execute()
    return created_event


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _local_timezone() -> tzinfo:
    """Return the local timezone using tzlocal for proper IANA timezone support."""
    return get_localzone()


def _to_rfc3339(dt: datetime) -> str:
    """Serialise *dt* to an RFC3339 string with a numeric UTC offset.

    Example output: ``"2026-05-05T09:30:00+02:00"``

    Args:
        dt: A timezone-aware datetime.

    Returns:
        RFC3339-formatted string accepted by the Google Calendar API.
    """
    return dt.isoformat()


def _tz_name(dt: datetime) -> str:
    """Return a timezone name string suitable for the Google Calendar API body.

    Uses the IANA timezone name from the datetime's tzinfo when available
    (e.g. ``"Europe/Madrid"`` from a tzlocal-produced ZoneInfo).  Falls back
    to ``"UTC"`` when tzinfo is absent or has no string representation.

    Args:
        dt: A timezone-aware datetime whose ``tzinfo`` is used.

    Returns:
        An IANA timezone name string, or ``"UTC"`` as a fallback.
    """
    if dt.tzinfo is None:
        return "UTC"
    name: str = str(dt.tzinfo)
    return name if name else "UTC"


def _list_events(
    service: Resource,
    time_min: datetime,
    time_max: datetime,
) -> list[dict[str, Any]]:
    """Execute a Calendar API events.list call and return all items.

    Args:
        service: Authenticated Calendar API service.
        time_min: Lower bound (inclusive) for event start times.
        time_max: Upper bound (exclusive) for event start times.

    Returns:
        List of event dicts ordered by start time.
    """
    _MAX_RESULTS: int = 250  # Hard cap across all pages.

    events_result: dict[str, Any] = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=_to_rfc3339(time_min),
            timeMax=_to_rfc3339(time_max),
            singleEvents=True,
            orderBy="startTime",
            maxResults=_MAX_RESULTS,
        )
        .execute()
    )
    items: list[dict[str, Any]] = events_result.get("items", [])

    # Paginate until the hard cap is reached or the API has no more pages.
    while "nextPageToken" in events_result and len(items) < _MAX_RESULTS:
        events_result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=_to_rfc3339(time_min),
                timeMax=_to_rfc3339(time_max),
                singleEvents=True,
                orderBy="startTime",
                maxResults=_MAX_RESULTS - len(items),
                pageToken=events_result["nextPageToken"],
            )
            .execute()
        )
        items.extend(events_result.get("items", []))

    return items
