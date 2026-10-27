"""Durable workflow around calendar bookings.

This is the only supported orchestration path for provider writes: stage →
human approve → one-time durable approval claim → provider write → record the
result. Ambiguous writes require reconciliation and are never retried here.
"""
from __future__ import annotations

import inspect
import os

from harvey.integrations.calendar import (
    CalendarApprovalRequired, CalendarClient, CalendarIntegrationError,
)


class CalendarWorkflowError(RuntimeError):
    """A calendar operation is not in a state that permits this transition."""


class CalendarService:
    def __init__(
        self, state, client: CalendarClient | None = None,
        booking_enabled: bool | None = None, booking_preflight=None,
    ):
        self.state = state
        self.client = client or CalendarClient()
        self.booking_enabled = (
            os.getenv("CALENDAR_BOOKING_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
            if booking_enabled is None else bool(booking_enabled)
        )
        self.booking_preflight = booking_preflight
        # The workflow binds approval to the persistent StateManager ledger,
        # never to a caller-supplied boolean alone.
        self.client._approval_verifier = state.verify_and_consume_calendar_approval

    async def stage_booking(
        self, *, prospect_email: str, prospect_name: str, slot: dict,
        time_zone: str = "UTC", requested_by: str = "dashboard",
        context: dict | None = None, approval_ttl_minutes: int = 60,
    ) -> dict:
        """Persist a pending request; never contacts Cal.com."""
        payload = self.client.prepare_booking_payload(
            prospect_email, slot, prospect_name=prospect_name, time_zone=time_zone,
        )
        return await self.state.create_calendar_operation(
            action="calendar.book",
            payload=payload,
            payload_hash=self.client.payload_hash(payload),
            requested_by=requested_by,
            context=context,
            approval_ttl_minutes=approval_ttl_minutes,
        )

    async def approve(self, operation_id: str, approved_by: str = "you") -> dict:
        changed = await self.state.approve_calendar_operation(operation_id, approved_by)
        op = await self.state.get_calendar_operation(operation_id)
        if not changed:
            raise CalendarWorkflowError("The booking request is no longer pending review or its approval expired.")
        return op

    async def reject(self, operation_id: str, rejected_by: str = "you", reason: str = "") -> dict:
        changed = await self.state.reject_calendar_operation(operation_id, rejected_by, reason)
        op = await self.state.get_calendar_operation(operation_id)
        if not changed:
            raise CalendarWorkflowError("The booking request is no longer pending review.")
        return op

    async def execute_approved(self, operation_id: str) -> dict:
        """Execute an approved operation once, recording confirmed/failed/unknown."""
        op = await self.state.get_calendar_operation(operation_id)
        if not op:
            raise CalendarWorkflowError("Booking request not found.")
        if not self.booking_enabled:
            raise CalendarWorkflowError(
                "Live calendar booking is disabled. Set CALENDAR_BOOKING_ENABLED=true only after CI, "
                "approval workflow, and a sandbox-account smoke test pass."
            )
        if not self.client.is_configured():
            raise CalendarWorkflowError("Cal.com is not configured; no booking was attempted.")
        if op.get("status") != "approved":
            raise CalendarWorkflowError(
                "Only an approved, unexpired booking request can be executed; "
                "unknown outcomes must be reconciled, not retried."
            )
        if self.booking_preflight is not None:
            reason = ""
            try:
                check = self.booking_preflight(op)
                if inspect.isawaitable(check):
                    check = await check
                if check is not True and check is not None:
                    reason = str(check) if isinstance(check, str) else "Current booking policy no longer permits this contact."
            except Exception:
                reason = "Harvey could not revalidate booking eligibility; no provider request was attempted."
            if reason:
                await self.state.cancel_approved_calendar_operation(operation_id, reason)
                raise CalendarWorkflowError(reason)
        payload = op.get("payload") or {}
        attendee = payload.get("attendee") or {}
        try:
            result = await self.client.create_booking(
                str(attendee.get("email") or ""),
                {"start": str(payload.get("start") or ""),
                 "duration": payload.get("lengthInMinutes")},
                prospect_name=str(attendee.get("name") or ""),
                time_zone=str(attendee.get("timeZone") or "UTC"),
                approved=True,
                approval_reference=str(op.get("approval_ref") or ""),
            )
        except CalendarApprovalRequired:
            # The verifier rejected the call before claiming the operation.
            # Do not mark it as executed or make a provider request.
            raise
        except CalendarIntegrationError as exc:
            current = await self.state.get_calendar_operation(operation_id)
            if current and current.get("status") == "executing":
                lower = str(exc).lower()
                unknown = any(term in lower for term in (
                    "unknown", "ambiguous", "reconcile", "may have", "timeout",
                    "timed out", "network error",
                ))
                await self.state.complete_calendar_operation(
                    operation_id,
                    status="unknown" if unknown else "failed",
                    error=str(exc),
                )
            current = await self.state.get_calendar_operation(operation_id)
            return current or op
        except Exception as exc:
            # An unexpected exception after the atomic approval claim could
            # be after Cal.com accepted the POST. Treat as unknown, not retryable.
            current = await self.state.get_calendar_operation(operation_id)
            if current and current.get("status") == "executing":
                await self.state.complete_calendar_operation(
                    operation_id, status="unknown",
                    error=f"Unexpected booking failure; reconcile before retry: {type(exc).__name__}",
                )
            return await self.state.get_calendar_operation(operation_id) or op

        await self.state.complete_calendar_operation(
            operation_id, status="confirmed", result=result,
        )
        return await self.state.get_calendar_operation(operation_id) or op

    async def list_operations(self, status: str | None = None, limit: int = 50) -> list[dict]:
        await self.state.expire_calendar_approvals()
        await self.state.fail_stale_calendar_operations()
        return await self.state.list_calendar_operations(status=status, limit=limit)

    async def reconcile(
        self, operation_id: str, *, outcome: str, note: str,
        provider_booking_id: str = "", provider_uid: str = "",
    ) -> dict:
        changed = await self.state.reconcile_calendar_operation(
            operation_id, outcome=outcome, note=note,
            provider_booking_id=provider_booking_id, provider_uid=provider_uid,
        )
        op = await self.state.get_calendar_operation(operation_id)
        if not changed:
            raise CalendarWorkflowError("Only an unknown operation can be reconciled. Verify provider evidence first.")
        return op
