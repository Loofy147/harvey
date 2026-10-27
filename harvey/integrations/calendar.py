"""Cal.com API v2 adapter for Harvey.

Availability reads are safe to call without approval. Booking is fail-closed:
the caller must provide explicit human approval and a verifier that checks a
durable Harvey approval record for the exact canonical booking payload.

Configure CALCOM_API_KEY and CALCOM_EVENT_TYPE_ID in the local .env file.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
import pytz

logger = logging.getLogger("harvey.calendar")
API_BASE = "https://api.cal.com/v2"
SLOTS_API_VERSION = "2024-09-04"
BOOKINGS_API_VERSION = "2026-02-25"
TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)
MAX_RANGE_DAYS = 31
MAX_DURATION_MINUTES = 720

class CalendarIntegrationError(RuntimeError):
    """A safe-to-display integration failure."""

class CalendarNotConfiguredError(CalendarIntegrationError):
    """Required local configuration is missing or invalid."""

class CalendarApprovalRequired(CalendarIntegrationError):
    """A durable approval could not be verified."""

ApprovalVerifier = Callable[[str, str, str], bool | Awaitable[bool]]

def _obj(value: Any) -> dict:
    return value if isinstance(value, dict) else {}

def _valid_email(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= 254
        and not any(ch.isspace() for ch in value)
        and bool(re.fullmatch(r"[^<>@\s]+@[^<>@\s]+\.[^<>@\s]+", value.strip()))
    )

def _date_range(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("date_range must be an ISO date or start/end date interval")
    pieces = value.strip().replace("..", "/").split("/")
    if len(pieces) == 1:
        pieces *= 2
    if len(pieces) != 2:
        raise ValueError("date_range must be YYYY-MM-DD or YYYY-MM-DD/YYYY-MM-DD")
    try:
        start, end = date.fromisoformat(pieces[0]), date.fromisoformat(pieces[1])
    except (TypeError, ValueError):
        raise ValueError("date_range must contain ISO calendar dates") from None
    if end < start:
        raise ValueError("date_range end must be on or after start")
    if (end - start).days > MAX_RANGE_DAYS:
        raise ValueError(f"date_range cannot exceed {MAX_RANGE_DAYS} days")
    return start.isoformat(), end.isoformat()

def _utc_start(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("slot.start must be a timezone-aware ISO 8601 datetime")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("slot.start must be a timezone-aware ISO 8601 datetime") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("slot.start must include a timezone offset")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

class CalendarClient:
    """Read availability and create approved Cal.com bookings.

    The approval verifier must perform a durable, one-time claim/check against
    Harvey's state store. This module intentionally does not invent that store.
    """

    def __init__(
        self,
        api_key: str | None = None,
        event_type_id: str | int | None = None,
        *,
        approval_verifier: ApprovalVerifier | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        raw_key = os.getenv("CALCOM_API_KEY", "") if api_key is None else api_key
        raw_id = os.getenv("CALCOM_EVENT_TYPE_ID", "") if event_type_id is None else str(event_type_id)
        self._key = raw_key.strip() if isinstance(raw_key, str) else ""
        raw_id = raw_id.strip() if isinstance(raw_id, str) else ""
        if self._key and (not self._key.isascii() or not self._key.isprintable()):
            raise CalendarNotConfiguredError("CALCOM_API_KEY contains invalid header characters.")
        if raw_id and (not raw_id.isascii() or not raw_id.isdigit() or int(raw_id) <= 0):
            raise CalendarNotConfiguredError("CALCOM_EVENT_TYPE_ID must be a positive integer.")
        self.event_type_id = int(raw_id) if raw_id else None
        self._approval_verifier = approval_verifier
        self._transport = transport

    def __repr__(self) -> str:
        return f"CalendarClient(event_type_id={self.event_type_id!r}, configured={self.is_configured()})"

    def is_configured(self) -> bool:
        return bool(self._key and self.event_type_id)

    async def test_connection(self) -> tuple[bool, str]:
        """Make one read-only availability request; never creates a booking."""
        if not self.is_configured():
            return False, "Set CALCOM_API_KEY and CALCOM_EVENT_TYPE_ID in .env."
        try:
            await self.get_available_slots(date.today().isoformat(), time_zone="UTC")
        except (CalendarIntegrationError, ValueError) as exc:
            return False, str(exc)[:240]
        return True, "Cal.com availability API responded; no booking was created."

    async def _request(
        self, method: str, path: str, *, api_version: str,
        params: dict | None = None, json_body: dict | None = None,
    ) -> dict:
        if not self.is_configured():
            raise CalendarNotConfiguredError(
                "Set CALCOM_API_KEY and CALCOM_EVENT_TYPE_ID in .env."
            )
        headers = {
            "Authorization": f"Bearer {self._key}",
            "cal-api-version": api_version,
            "Accept": "application/json",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            async with httpx.AsyncClient(
                base_url=API_BASE, timeout=TIMEOUT, transport=self._transport,
                follow_redirects=False, trust_env=False,
            ) as client:
                response = await client.request(method, path, headers=headers, params=params, json=json_body)
        except httpx.TimeoutException:
            if method == "POST":
                raise CalendarIntegrationError(
                    "Booking outcome is unknown after a timeout. Check Cal.com before retrying."
                ) from None
            raise CalendarIntegrationError("Cal.com availability request timed out.") from None
        except httpx.HTTPError:
            if method == "POST":
                raise CalendarIntegrationError(
                    "Booking outcome is unknown after a network error. Check Cal.com before retrying."
                ) from None
            raise CalendarIntegrationError("Could not connect to Cal.com.") from None

        try:
            envelope = _obj(response.json())
        except (ValueError, TypeError):
            envelope = {}
        data = envelope.get("data")
        if response.is_success and envelope.get("status") != "error":
            if method == "GET" and data is None:
                return {}
            if isinstance(data, dict):
                return data
            if method == "POST":
                raise CalendarIntegrationError(
                    "Cal.com returned an ambiguous booking response. Reconcile before retrying."
                )

        status = response.status_code
        if method == "POST" and status >= 500:
            raise CalendarIntegrationError(
                "Cal.com returned a server error during booking; outcome may be unknown. Reconcile before retrying."
            )
        messages = {
            401: "Cal.com rejected the API key. Check CALCOM_API_KEY.",
            403: "Cal.com denied the operation; check API key permissions.",
            404: "Cal.com could not find the configured event type or resource.",
            409: "Cal.com reports a booking conflict. Refresh availability and ask for a new choice.",
            429: "Cal.com rate limit reached. Retry after the provider window.",
        }
        detail = messages.get(status, "Cal.com is temporarily unavailable." if status >= 500 else "")
        if not detail:
            message = envelope.get("errorMessage") or envelope.get("message") or ""
            message = str(message).replace(self._key, "[hidden]").replace("\n", " ").strip()[:180]
            detail = f"Cal.com returned HTTP {status}" + (f": {message}" if message else ".")
        raise CalendarIntegrationError(detail)

    async def get_available_slots(
        self, date_range: str, *, time_zone: str = "UTC", duration: int | None = None,
    ) -> list[dict]:
        """Return available slots; this is not a reservation or a slot hold."""
        start, end = _date_range(date_range)
        if not isinstance(time_zone, str):
            raise ValueError("time_zone must be a valid IANA timezone")
        try:
            pytz.timezone(time_zone)
        except pytz.UnknownTimeZoneError:
            raise ValueError("time_zone must be a valid IANA timezone, such as Africa/Algiers") from None
        params: dict[str, str | int] = {
            "eventTypeId": self.event_type_id or "",
            "start": start,
            "end": end,
            "timeZone": time_zone,
            "format": "range",
        }
        if duration is not None:
            if isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= MAX_DURATION_MINUTES:
                raise ValueError(f"duration must be an integer from 1 to {MAX_DURATION_MINUTES}")
            params["duration"] = duration
        data = await self._request("GET", "/slots", api_version=SLOTS_API_VERSION, params=params)
        slots: list[dict] = []
        for day, day_slots in data.items():
            if not isinstance(day_slots, list):
                continue
            for item in day_slots:
                if not isinstance(item, dict) or not isinstance(item.get("start"), str):
                    continue
                slot = {key: item[key] for key in ("start", "end", "attendeesCount", "bookingUid") if key in item}
                slot["date"] = day
                slots.append(slot)
        return slots

    def prepare_booking_payload(
        self, prospect_email: str, slot: dict, *, prospect_name: str,
        time_zone: str = "UTC",
    ) -> dict:
        """Validate and canonicalize a booking payload without making a request."""
        if not _valid_email(prospect_email):
            raise ValueError("prospect_email must be a valid email address")
        if not isinstance(prospect_name, str) or not prospect_name.strip() or len(prospect_name.strip()) > 200:
            raise ValueError("prospect_name is required and must be at most 200 characters")
        try:
            pytz.timezone(time_zone)
        except (pytz.UnknownTimeZoneError, TypeError):
            raise ValueError("time_zone must be a valid IANA timezone") from None
        if not self.event_type_id:
            raise CalendarNotConfiguredError("Set CALCOM_EVENT_TYPE_ID in .env.")
        slot_data = _obj(slot)
        start = _utc_start(slot_data.get("start", ""))
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        if start_dt <= datetime.now(timezone.utc):
            raise ValueError("slot.start must be in the future")
        duration = slot_data.get("duration")
        end_value = slot_data.get("end")
        if isinstance(duration, bool) or (duration is not None and not isinstance(duration, int)):
            raise ValueError("slot duration must be an integer number of minutes")
        if end_value:
            end_dt = datetime.fromisoformat(_utc_start(end_value).replace("Z", "+00:00"))
            seconds = (end_dt - start_dt).total_seconds()
            if seconds <= 0 or seconds % 60:
                raise ValueError("slot.end must be after slot.start by a whole number of minutes")
            inferred = int(seconds // 60)
            if duration is not None and duration != inferred:
                raise ValueError("slot duration conflicts with its end time")
            duration = inferred
        if duration is not None and not 1 <= duration <= MAX_DURATION_MINUTES:
            raise ValueError(f"slot duration must be from 1 to {MAX_DURATION_MINUTES} minutes")
        payload = {
            "start": start,
            "eventTypeId": self.event_type_id,
            "attendee": {
                "name": prospect_name.strip(),
                "email": prospect_email.strip(),
                "timeZone": time_zone,
                "language": "en",
            },
            "allowConflicts": False,
        }
        if duration is not None:
            payload["lengthInMinutes"] = duration
        return payload

    @staticmethod
    def payload_hash(payload: dict) -> str:
        """Stable digest used to bind a human approval to exactly one payload."""
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    async def _verify_approval(self, reference: str, approved: bool, payload_hash: str) -> None:
        if approved is not True or not isinstance(reference, str) or not reference.strip():
            raise CalendarApprovalRequired("Booking requires explicit human approval and a durable approval reference.")
        if self._approval_verifier is None:
            raise CalendarApprovalRequired("Booking is disabled until Harvey's durable approval verifier is connected.")
        try:
            result = self._approval_verifier(reference.strip(), "calendar.book", payload_hash)
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            raise CalendarApprovalRequired("Harvey could not verify booking approval; no booking was attempted.") from None
        if result is not True:
            raise CalendarApprovalRequired("The approval is missing, expired, or does not authorize this booking.")

    async def create_booking(
        self, prospect_email: str, slot: dict, *, prospect_name: str,
        time_zone: str = "UTC", approved: bool = False, approval_reference: str = "",
    ) -> dict:
        """Create a booking only after approval is verified for this exact payload."""
        payload = self.prepare_booking_payload(
            prospect_email, slot, prospect_name=prospect_name, time_zone=time_zone,
        )
        payload_hash = self.payload_hash(payload)
        await self._verify_approval(approval_reference, approved, payload_hash)
        body = {
            **payload,
            "metadata": {"source": "harvey", "approvalReference": approval_reference.strip()[:200]},
        }
        data = await self._request("POST", "/bookings", api_version=BOOKINGS_API_VERSION, json_body=body)
        result = {key: data[key] for key in ("id", "uid", "start", "end", "meetingUrl", "status") if key in data}
        if not result.get("uid") and not result.get("id"):
            raise CalendarIntegrationError("Cal.com returned success without a booking identifier. Reconcile before retrying.")
        return result

    async def book_meeting(
        self, prospect_email: str, slot: dict, *, prospect_name: str = "",
        time_zone: str = "UTC", approved: bool = False, approval_reference: str = "",
    ) -> bool:
        """Legacy bool wrapper; fail closed if booking approval is not wired."""
        try:
            await self.create_booking(
                prospect_email, slot, prospect_name=prospect_name, time_zone=time_zone,
                approved=approved, approval_reference=approval_reference,
            )
            return True
        except (CalendarIntegrationError, ValueError) as exc:
            logger.warning("Calendar booking not completed: %s", str(exc)[:200])
            return False
