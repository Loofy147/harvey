"""Deterministic tests for Cal.com adapter; no live account or network."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from harvey.integrations.calendar import (
    API_BASE, BOOKINGS_API_VERSION, SLOTS_API_VERSION, CalendarApprovalRequired,
    CalendarClient, CalendarIntegrationError, CalendarNotConfiguredError,
)

KEY = "cal_test_api_key_123"
EVENT_TYPE = 456
FUTURE = (datetime.now(timezone.utc) + timedelta(days=45)).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def success(data):
    return httpx.Response(200, json={"status": "success", "data": data})

def slots_transport(calls=None, *, status=200):
    def respond(request):
        if calls is not None:
            calls.append(request)
        assert request.url.host == "api.cal.com"
        assert request.url.path == "/v2/slots"
        assert request.method == "GET"
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        assert request.headers["cal-api-version"] == SLOTS_API_VERSION
        if status != 200:
            return httpx.Response(status, json={"status": "error", "message": "provider error"})
        return success({"2033-09-05": [
            {"start": "2033-09-05T09:00:00.000+02:00", "end": "2033-09-05T09:30:00.000+02:00"},
            {"start": "2033-09-05T10:00:00.000+02:00", "end": "2033-09-05T10:30:00.000+02:00"},
        ], "malformed": "skip"})
    return httpx.MockTransport(respond)

def booking_transport(calls=None, *, status=201):
    def respond(request):
        if calls is not None:
            calls.append(request)
        assert request.url == httpx.URL(f"{API_BASE}/bookings")
        assert request.method == "POST"
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        assert request.headers["cal-api-version"] == BOOKINGS_API_VERSION
        assert json.loads(request.content)["allowConflicts"] is False
        if status >= 400:
            return httpx.Response(status, json={"status": "error", "message": "provider error"})
        return success({"id": 987, "uid": "booking_abc", "start": FUTURE, "end": FUTURE})
    return httpx.MockTransport(respond)

def client(transport, **kwargs):
    return CalendarClient(KEY, EVENT_TYPE, transport=transport, **kwargs)

@pytest.mark.asyncio
async def test_availability_normalizes_slots_and_uses_documented_headers():
    calls = []
    slots = await client(slots_transport(calls)).get_available_slots(
        "2033-09-05/2033-09-10", time_zone="Africa/Algiers", duration=30)
    assert len(slots) == 2 and slots[0]["date"] == "2033-09-05"
    request = calls[0]
    assert request.url.params["eventTypeId"] == str(EVENT_TYPE)
    assert request.url.params["start"] == "2033-09-05"
    assert request.url.params["end"] == "2033-09-10"
    assert request.url.params["timeZone"] == "Africa/Algiers"
    assert request.url.params["duration"] == "30"

@pytest.mark.asyncio
async def test_single_date_is_supported():
    assert len(await client(slots_transport()).get_available_slots("2033-09-05")) == 2

@pytest.mark.asyncio
@pytest.mark.parametrize("value", [
    "", "tomorrow", "2033-09-10/2033-09-05", "2033-09-05/2033-11-30",
    "2033-09-05/2033-09-06/2033-09-07",
])
async def test_invalid_and_excessive_ranges_are_rejected(value):
    with pytest.raises(ValueError):
        await client(slots_transport()).get_available_slots(value)

@pytest.mark.asyncio
async def test_missing_config_fails_before_network():
    calls = []
    c = CalendarClient(transport=httpx.MockTransport(lambda r: calls.append(r) or success({})))
    assert not c.is_configured()
    with pytest.raises(CalendarNotConfiguredError):
        await c.get_available_slots("2033-09-05")
    assert calls == []

def test_invalid_credentials_and_ids_are_rejected():
    with pytest.raises(CalendarNotConfiguredError):
        CalendarClient("bad\nkey", EVENT_TYPE)
    with pytest.raises(CalendarNotConfiguredError):
        CalendarClient(KEY, "../bad")

def test_repr_never_contains_credentials():
    assert KEY not in repr(client(slots_transport()))

@pytest.mark.asyncio
async def test_booking_fails_closed_without_approval_or_verifier():
    calls = []
    c = client(booking_transport(calls))
    with pytest.raises(CalendarApprovalRequired):
        await c.create_booking("person@example.com", {"start": FUTURE}, prospect_name="Person")
    with pytest.raises(CalendarApprovalRequired):
        await c.create_booking("person@example.com", {"start": FUTURE}, prospect_name="Person",
                               approved=True, approval_reference="approval-1")
    assert calls == []

@pytest.mark.asyncio
async def test_booking_approval_is_bound_to_canonical_payload():
    calls = []
    async def verifier(reference, action, digest):
        expected = {"start": FUTURE, "eventTypeId": EVENT_TYPE,
                    "attendee": {"name": "Person", "email": "person@example.com",
                                 "timeZone": "Africa/Algiers", "language": "en"},
                    "allowConflicts": False}
        canonical = json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        return reference == "approval-1" and action == "calendar.book" and digest == hashlib.sha256(canonical).hexdigest()
    c = client(booking_transport(calls), approval_verifier=verifier)
    result = await c.create_booking("person@example.com", {"start": FUTURE},
        prospect_name="Person", time_zone="Africa/Algiers", approved=True, approval_reference="approval-1")
    assert result["uid"] == "booking_abc" and result["id"] == 987
    assert len(calls) == 1

@pytest.mark.asyncio
async def test_invalid_attendee_and_timezone_never_reach_provider():
    calls = []
    c = client(booking_transport(calls), approval_verifier=lambda *_: True)
    with pytest.raises(ValueError):
        await c.create_booking("not-email", {"start": FUTURE}, prospect_name="Person",
                               approved=True, approval_reference="a")
    with pytest.raises(ValueError):
        await c.create_booking("person@example.com", {"start": FUTURE}, prospect_name="Person",
                               time_zone="Mars/Olympus", approved=True, approval_reference="a")
    assert calls == []

@pytest.mark.asyncio
async def test_local_slot_offset_is_normalized_to_utc_for_booking():
    calls = []
    c = client(booking_transport(calls), approval_verifier=lambda *_: True)
    await c.create_booking("person@example.com", {"start": "2033-09-05T09:00:00+02:00"},
        prospect_name="Person", approved=True, approval_reference="approval-1")
    payload = json.loads(calls[0].content)
    assert payload["start"] == "2033-09-05T07:00:00Z"

@pytest.mark.asyncio
async def test_booking_timeout_is_unknown_not_retryable():
    def timeout(request):
        raise httpx.ReadTimeout("timed out", request=request)
    c = client(httpx.MockTransport(timeout), approval_verifier=lambda *_: True)
    with pytest.raises(CalendarIntegrationError, match="outcome is unknown"):
        await c.create_booking("person@example.com", {"start": FUTURE}, prospect_name="Person",
                               approved=True, approval_reference="approval-1")

@pytest.mark.asyncio
async def test_success_without_booking_id_requires_reconciliation():
    c = client(httpx.MockTransport(lambda r: httpx.Response(201, json={"status": "success", "data": {}})),
               approval_verifier=lambda *_: True)
    with pytest.raises(CalendarIntegrationError, match="Reconcile"):
        await c.create_booking("person@example.com", {"start": FUTURE}, prospect_name="Person",
                               approved=True, approval_reference="approval-1")

@pytest.mark.asyncio
async def test_legacy_wrapper_stays_fail_closed():
    calls = []
    c = client(booking_transport(calls))
    assert await c.book_meeting("person@example.com", {"start": FUTURE}) is False
    assert calls == []


@pytest.mark.asyncio
async def test_selected_slot_duration_is_bound_to_payload():
    c = client(slots_transport())
    start = datetime.now(timezone.utc) + timedelta(days=45)
    end = start + timedelta(minutes=60)
    slot = {
        "start": start.replace(microsecond=0).isoformat(),
        "end": end.replace(microsecond=0).isoformat(),
    }
    payload = c.prepare_booking_payload(
        "person@example.com", slot, prospect_name="Person", time_zone="Africa/Algiers",
    )
    assert payload["lengthInMinutes"] == 60
    assert c.payload_hash(payload) == c.payload_hash({**payload})
