"""Stateful calendar approval workflow tests; no live provider access."""
import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from harvey.integrations.calendar import CalendarClient
from harvey.integrations.calendar_service import CalendarService, CalendarWorkflowError
from harvey.state import StateManager

@pytest_asyncio.fixture
async def state(tmp_path):
    db = StateManager(str(tmp_path / "calendar.db"))
    await db.init_db()
    yield db

def payload():
    return {
        "start": (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0).isoformat().replace("+00:00","Z"),
        "eventTypeId": 42,
        "attendee": {"name":"Ada Lovelace","email":"ada@example.com","timeZone":"Africa/Algiers","language":"en"},
        "allowConflicts": False,
    }

def digest(p):
    encoded = json.dumps(p,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()

@pytest.mark.asyncio
async def test_stage_and_review_are_durable_and_do_not_call_provider(state):
    p=payload()
    op=await state.create_calendar_operation(action="calendar.book",payload=p,payload_hash=digest(p),requested_by="dashboard")
    assert op["status"] == "pending_review"
    assert op["payload"] == p and op["approval_ref"]
    assert not await state.verify_and_consume_calendar_approval(op["approval_ref"],"calendar.book",digest(p))
    assert await state.approve_calendar_operation(op["id"],"operator")
    assert not await state.approve_calendar_operation(op["id"],"operator")
    approved=await state.get_calendar_operation(op["id"])
    assert approved["status"]=="approved" and approved["approved_by"]=="operator"

@pytest.mark.asyncio
async def test_approval_is_payload_bound_and_consumed_only_once(state):
    p=payload()
    op=await state.create_calendar_operation(action="calendar.book",payload=p,payload_hash=digest(p))
    await state.approve_calendar_operation(op["id"],"operator")
    assert not await state.verify_and_consume_calendar_approval(op["approval_ref"],"calendar.book","0"*64)
    assert not await state.verify_and_consume_calendar_approval(op["approval_ref"],"calendar.cancel",digest(p))
    assert await state.verify_and_consume_calendar_approval(op["approval_ref"],"calendar.book",digest(p))
    assert not await state.verify_and_consume_calendar_approval(op["approval_ref"],"calendar.book",digest(p))
    current=await state.get_calendar_operation(op["id"])
    assert current["status"]=="executing"

@pytest.mark.asyncio
async def test_concurrent_consumers_cannot_reuse_approval(state):
    p=payload()
    op=await state.create_calendar_operation(action="calendar.book",payload=p,payload_hash=digest(p))
    await state.approve_calendar_operation(op["id"],"operator")
    outcomes=await asyncio.gather(*[
      state.verify_and_consume_calendar_approval(op["approval_ref"],"calendar.book",digest(p))
      for _ in range(8)
    ])
    assert sum(bool(x) for x in outcomes)==1

@pytest.mark.asyncio
async def test_terminal_outcome_is_written_once_and_preserves_provider_receipt(state):
    p=payload()
    op=await state.create_calendar_operation(action="calendar.book",payload=p,payload_hash=digest(p))
    await state.approve_calendar_operation(op["id"],"operator")
    assert await state.verify_and_consume_calendar_approval(op["approval_ref"],"calendar.book",digest(p))
    result={"id":123,"uid":"booking-uid"}
    assert await state.complete_calendar_operation(op["id"],status="confirmed",result=result)
    assert not await state.complete_calendar_operation(op["id"],status="confirmed",result=result)
    done=await state.get_calendar_operation(op["id"])
    assert done["status"]=="confirmed" and done["provider_booking_id"]=="123"
    assert done["provider_uid"]=="booking-uid" and done["result"]==result

@pytest.mark.asyncio
async def test_rejection_never_becomes_approvable_or_executable(state):
    p=payload()
    op=await state.create_calendar_operation(action="calendar.book",payload=p,payload_hash=digest(p))
    assert await state.reject_calendar_operation(op["id"],"operator","user declined")
    assert not await state.approve_calendar_operation(op["id"],"operator")
    assert not await state.verify_and_consume_calendar_approval(op["approval_ref"],"calendar.book",digest(p))
    assert (await state.get_calendar_operation(op["id"]))["status"]=="rejected"

@pytest.mark.asyncio
async def test_expired_approval_cannot_be_consumed(state):
    p=payload()
    op=await state.create_calendar_operation(action="calendar.book",payload=p,payload_hash=digest(p),approval_ttl_minutes=1)
    old=(datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(minutes=2)).isoformat()
    async with state._connect() as db:
        await db.execute("UPDATE calendar_approvals SET expires_at=? WHERE id=?",(old,op["approval_ref"]))
        await db.commit()
    assert await state.expire_calendar_approvals()==1
    assert not await state.approve_calendar_operation(op["id"],"operator")
    assert (await state.get_calendar_operation(op["id"]))["status"]=="cancelled"

@pytest.mark.asyncio
async def test_crash_in_flight_becomes_unknown_and_requires_reconciliation(state):
    p=payload()
    op=await state.create_calendar_operation(action="calendar.book",payload=p,payload_hash=digest(p))
    await state.approve_calendar_operation(op["id"],"operator")
    await state.verify_and_consume_calendar_approval(op["approval_ref"],"calendar.book",digest(p))
    old=(datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(minutes=20)).isoformat()
    async with state._connect() as db:
        await db.execute("UPDATE calendar_operations SET updated_at=? WHERE id=?",(old,op["id"]))
        await db.commit()
    assert await state.fail_stale_calendar_operations()==1
    assert not await state.complete_calendar_operation(op["id"],status="confirmed",result={"id":1})
    assert not await state.reconcile_calendar_operation(op["id"],outcome="confirmed",note="no evidence")
    assert await state.reconcile_calendar_operation(
        op["id"],outcome="confirmed",note="Checked Cal.com booking list",provider_booking_id="1",provider_uid="u")
    current=await state.get_calendar_operation(op["id"])
    assert current["status"]=="confirmed" and current["provider_booking_id"]=="1"

@pytest.mark.asyncio
async def test_service_stages_using_same_payload_hash_and_never_sends_on_stage(state):
    called=[]
    import httpx
    client=CalendarClient("cal_test",42,transport=httpx.MockTransport(lambda r: called.append(r)))
    svc=CalendarService(state,client)
    operation=await svc.stage_booking(prospect_email="ada@example.com",prospect_name="Ada Lovelace",
       slot={"start":(datetime.now(timezone.utc)+timedelta(days=3)).replace(microsecond=0).isoformat()},
       time_zone="Africa/Algiers")
    assert operation["status"]=="pending_review"
    assert called==[]
    assert operation["payload_hash"]==CalendarClient.payload_hash(operation["payload"])


@pytest.mark.asyncio
async def test_state_refuses_a_payload_hash_mismatch(state):
    p = payload()
    with pytest.raises(ValueError, match="does not match"):
        await state.create_calendar_operation(
            action="calendar.book", payload=p, payload_hash="0" * 64
        )


@pytest.mark.asyncio
async def test_execution_time_preflight_cancels_without_provider_io(state):
    import httpx
    calls = []
    transport = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(
        201, json={"status": "success", "data": {"id": 123, "uid": "would-have-booked"}}
    ))
    c = CalendarClient("cal_test_key", 42, transport=transport)
    svc = CalendarService(
        state, c, booking_enabled=True,
        booking_preflight=lambda operation: "Contact was added to do-not-contact after approval.",
    )
    operation = await svc.stage_booking(
        prospect_email="ada@example.com",
        prospect_name="Ada Lovelace",
        slot={"start": (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0).isoformat()},
        time_zone="Africa/Algiers",
        context={"prospect_id": "prospect-123"},
    )
    assert operation["context"] == {"prospect_id": "prospect-123"}
    assert await state.approve_calendar_operation(operation["id"], "operator")
    with pytest.raises(CalendarWorkflowError, match="do-not-contact"):
        await svc.execute_approved(operation["id"])
    current = await state.get_calendar_operation(operation["id"])
    assert current["status"] == "cancelled"
    assert calls == []
