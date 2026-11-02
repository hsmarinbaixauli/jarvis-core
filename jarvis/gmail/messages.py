"""Gmail message retrieval, reply, and label management.

Provides thin, typed wrappers over the Gmail API v1 for the operations
Jarvis needs: listing unread messages, fetching a full message for reading
or replying, sending a reply in the same thread, and marking a message as
read.
"""

from __future__ import annotations

import base64
import email.message
from typing import Any

from googleapiclient.discovery import Resource

# Module-level cache keyed by userId, so tests can clear() it.
_sender_email_cache: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_unread_messages(
    service: Resource,
    max_results: int = 10,
) -> list[dict[str, Any]]:
    """Return unread inbox messages as normalised dicts.

    Args:
        service: Authenticated Gmail API service object.
        max_results: Maximum number of messages to return (1–25).

    Returns:
        A list of dicts, each with keys: ``id``, ``thread_id``, ``subject``,
        ``sender``, ``snippet``, ``date``.  Returns an empty list when the
        inbox has no unread messages.
    """
    result: dict[str, Any] = (
        service.users().messages().list(userId="me", q="is:unread in:inbox", maxResults=max_results).execute()
    )
    raw_messages: list[dict[str, Any]] = result.get("messages", [])

    # Paginate until the cap is reached or the API has no more pages.
    while "nextPageToken" in result and len(raw_messages) < max_results:
        result = (
            service.users()
            .messages()
            .list(
                userId="me",
                q="is:unread in:inbox",
                maxResults=max_results - len(raw_messages),
                pageToken=result["nextPageToken"],
            )
            .execute()
        )
        raw_messages.extend(result.get("messages", []))

    output: list[dict[str, Any]] = []
    for msg in raw_messages:
        meta: dict[str, Any] = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=msg["id"],
                format="metadata",
                metadataHeaders=["Subject", "From", "Date"],
            )
            .execute()
        )
        headers: list[dict[str, str]] = meta.get("payload", {}).get("headers", [])
        output.append(
            {
                "id": msg["id"],
                "thread_id": meta.get("threadId", ""),
                "subject": _extract_header(headers, "Subject"),
                "sender": _extract_header(headers, "From"),
                "snippet": meta.get("snippet", ""),
                "date": _extract_header(headers, "Date"),
            }
        )

    return output


def get_message_full(service: Resource, message_id: str) -> dict[str, Any]:
    """Return the full content of a single message.

    Fetches headers and the decoded ``text/plain`` body.  Used internally by
    :func:`send_reply` to build a correctly threaded reply.

    Args:
        service: Authenticated Gmail API service object.
        message_id: The ``id`` value returned by :func:`get_unread_messages`.

    Returns:
        A dict with keys: ``id``, ``thread_id``, ``subject``, ``sender``,
        ``to``, ``date``, ``body_plain``, ``message_id_header``,
        ``references``.
    """
    msg: dict[str, Any] = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    headers: list[dict[str, str]] = msg.get("payload", {}).get("headers", [])
    return {
        "id": message_id,
        "thread_id": msg.get("threadId", ""),
        "subject": _extract_header(headers, "Subject"),
        "sender": _extract_header(headers, "From"),
        "to": _extract_header(headers, "To"),
        "date": _extract_header(headers, "Date"),
        "body_plain": _extract_plain_body(msg.get("payload", {})),
        "message_id_header": _extract_header(headers, "Message-ID"),
        "references": _extract_header(headers, "References"),
    }


def send_reply(
    service: Resource,
    message_id: str,
    body_text: str,
) -> dict[str, Any]:
    """Send a plain-text reply to the given message in the same thread.

    Fetches the original message to derive the recipient, subject, and
    threading headers, then sends the reply via the Gmail API.

    Args:
        service: Authenticated Gmail API service object.
        message_id: The ``id`` of the message being replied to.
        body_text: Plain-text body for the reply (max 5 000 characters,
            enforced by the caller).

    Returns:
        The Gmail API response dict for the sent message.
    """
    original: dict[str, Any] = get_message_full(service, message_id)

    subject: str = original["subject"]
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    sender_email: str = _get_sender_email(service)
    raw: str = _build_raw_reply(
        to=original["sender"],
        subject=subject,
        body_text=body_text,
        in_reply_to=original["message_id_header"],
        references=original["references"],
        sender_email=sender_email,
    )

    sent: dict[str, Any] = (
        service.users()
        .messages()
        .send(
            userId="me",
            body={"raw": raw, "threadId": original["thread_id"]},
        )
        .execute()
    )
    return sent


def mark_as_read(service: Resource, message_id: str) -> None:
    """Remove the UNREAD label from the given message.

    Args:
        service: Authenticated Gmail API service object.
        message_id: The ``id`` of the message to mark as read.
    """
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"removeLabelIds": ["UNREAD"]},
    ).execute()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_sender_email(service: Resource) -> str:
    """Return the authenticated user's email address.

    Calls ``users().getProfile()`` on first use and caches the result for the
    lifetime of the process so subsequent calls are free.

    Args:
        service: Authenticated Gmail API service object.

    Returns:
        The email address string associated with the authenticated account.
    """
    if "me" not in _sender_email_cache:
        profile: dict[str, Any] = service.users().getProfile(userId="me").execute()
        _sender_email_cache["me"] = profile["emailAddress"]
    return _sender_email_cache["me"]


def _extract_header(headers: list[dict[str, str]], name: str) -> str:
    """Return the value of the first header matching *name* (case-insensitive)."""
    name_lower: str = name.lower()
    for header in headers:
        if header.get("name", "").lower() == name_lower:
            return header.get("value", "")
    return ""


def _extract_plain_body(payload: dict[str, Any]) -> str:
    """Recursively walk MIME parts and return the first ``text/plain`` body.

    Decodes the base64url-encoded ``body.data`` field.  Returns an empty
    string when no plain-text part is found.
    """
    mime_type: str = payload.get("mimeType", "")

    if mime_type == "text/plain":
        data: str = payload.get("body", {}).get("data", "")
        if data:
            # Pad to a multiple of 4 before decoding.
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        return ""

    for part in payload.get("parts", []):
        result: str = _extract_plain_body(part)
        if result:
            return result

    return ""


def _build_raw_reply(
    to: str,
    subject: str,
    body_text: str,
    in_reply_to: str,
    references: str,
    sender_email: str = "",
) -> str:
    """Build a base64url-encoded RFC 5322 reply message.

    Args:
        to: Recipient address string (the original sender).
        subject: Subject line with the ``Re:`` prefix already applied.
        body_text: Plain-text message body.
        in_reply_to: Original ``Message-ID`` header value.
        references: Original ``References`` header value (may be empty).
        sender_email: The authenticated sender's email address for the
            ``From`` header.  When empty the header is omitted and the Gmail
            API infers it from the authenticated account.

    Returns:
        Base64url-encoded string ready for the Gmail API ``raw`` field.
    """
    msg: email.message.EmailMessage = email.message.EmailMessage()
    if sender_email:
        msg["From"] = sender_email
    msg["To"] = to
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        combined: str = f"{references} {in_reply_to}".strip() if references else in_reply_to
        msg["References"] = combined
    msg.set_content(body_text)

    return base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
