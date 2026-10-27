"""Calendar dashboard API contract tests; provider calls stay disabled."""
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey.state import StateManager


@pytest.fixture
def calendar_client(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "CALCOM_API_KEY=cal_test_key\n"
        "CALCOM_EVENT_TYPE_ID=42\n"
        "CALENDAR_BOOKING_ENABLED=false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dash, "ENV_FILE", env_file)
    monkeypatch.setattr(dash, "DB_PATH", data / "harvey.db")
    monkeypatch.setattr(dash, "WEB_DIR", Path(__file__).resolve().parent.parent / "harvey" / "web")
    dash.token_store.configure(data / "dashboard-token")
    token = dash.token_store.get()
    with TestClient(
        dash.app, base_url="http://127.0.0.1:5555",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        client.calendar_db = data / "harvey.db"
        yield client


def _payload():
    return {
        "start": "2033-09-05T09:00:00Z",
        "eventTypeId": 42,
        "attendee": {
            "name": "Ada Lovelace", "email": "ada@example.com",
            "timeZone": "Africa/Algiers", "language": "en",
        },
        "allowConflicts": False,
    }


def _hash(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


@pytest.mark.asyncio
def test_calendar_status_reports_credentials_but_keeps_booking_disabled(calendar_client):
    # The endpoint's configured check reads configuration only; it must not call the vendor.
    response = calendar_client.get("/api/calendar/status")
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert body["booking_enabled"] is False


def test_calendar_operation_requires_approval_and_disabled_switch(calendar_client):
    import asyncio
    state = StateManager(str(calendar_client.calendar_db))
    asyncio.run(state.init_db())
    payload = _payload()
    op = asyncio.run(state.create_calendar_operation(
        action="calendar.book", payload=payload, payload_hash=_hash(payload)
    ))
    approved = calendar_client.post(
        f"/api/calendar/operations/{op['id']}/approve", json={"approved_by": "tester"}
    )
    assert approved.status_code == 200
    assert approved.json()["operation"]["status"] == "approved"

    execute = calendar_client.post(f"/api/calendar/operations/{op['id']}/execute")
    assert execute.status_code == 409
    assert "disabled" in execute.json()["error"].lower()
    current = asyncio.run(state.get_calendar_operation(op["id"]))
    assert current["status"] == "approved"


def test_calendar_reject_is_terminal_and_audited(calendar_client):
    import asyncio
    state = StateManager(str(calendar_client.calendar_db))
    asyncio.run(state.init_db())
    payload = _payload()
    op = asyncio.run(state.create_calendar_operation(
        action="calendar.book", payload=payload, payload_hash=_hash(payload)
    ))
    rejected = calendar_client.post(
        f"/api/calendar/operations/{op['id']}/reject",
        json={"rejected_by": "tester", "reason": "Not requested"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["operation"]["status"] == "rejected"
    approve = calendar_client.post(
        f"/api/calendar/operations/{op['id']}/approve", json={"approved_by": "tester"}
    )
    assert approve.status_code == 409


def test_calendar_operations_endpoint_lists_pending_request(calendar_client):
    import asyncio
    state = StateManager(str(calendar_client.calendar_db))
    asyncio.run(state.init_db())
    payload = _payload()
    op = asyncio.run(state.create_calendar_operation(
        action="calendar.book", payload=payload, payload_hash=_hash(payload)
    ))
    response = calendar_client.get("/api/calendar/operations")
    assert response.status_code == 200
    operations = response.json()["operations"]
    assert any(item["id"] == op["id"] and item["status"] == "pending_review" for item in operations)
