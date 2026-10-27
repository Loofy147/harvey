# Cal.com integration specification

Status: adapter, durable StateManager approval/operation ledger, and dashboard review/reconciliation workflow are implemented on this branch. Repository CI and live sandbox validation remain pending; live booking is disabled by default.

## Credentials and endpoints
- Use CALCOM_API_KEY and CALCOM_EVENT_TYPE_ID only in a local ignored .env file.
- Availability: GET https://api.cal.com/v2/slots, header cal-api-version: 2024-09-04.
- Booking: POST https://api.cal.com/v2/bookings, header cal-api-version: 2026-02-25.
- The base host is fixed in code, redirects are disabled, environment proxy variables are ignored, and credentials must never be written to logs.

## Safety contract
- Availability is a read operation, never a hold or reservation.
- Booking requires an explicit approval flag and a verifier for a durable approval record bound to action calendar.book and the SHA-256 of a canonical payload.
- Persist the approved payload, approval actor, timestamp, expiry, provider operation ID and terminal outcome in Harvey state before enabling booking in an end-user workflow.
- A 5xx, timeout, malformed success body, or success without a stable booking ID is an unknown outcome. Reconcile provider state before retry; never blindly retry POST.
- Use Cal.com as the last availability authority and treat slot data as stale after returning it to the user.
- Do not allow the model to choose the approval decision, bypass DNC/opt-out rules, or fabricate successful booking results.

## Integration sequence
1. Wire the verifier to a durable, one-time approval claim in StateManager.
2. Add a booking-operation ledger with pending, confirmed, rejected, unknown and reconciled states.
3. Add a preview/confirm panel to the dashboard showing attendee, host/event type, timezone, start/end, and a human-readable approval fingerprint.
4. Add a reconcile command that looks up the provider booking by UID or searches recent events where supported.
5. Add bounded retry policy only for idempotent reads; require manual reconciliation for unknown writes.
6. Add provider contract tests, end-to-end approval tests and a sandbox-account smoke test.
7. Keep automatic bookings disabled until the dashboard + state-store path passes CI.

## Current limitations
- Calendar approvals use a dedicated durable approval/operation ledger rather than the email outbox; dashboard routes and review UI are now wired.
- The approval verifier is injected by CalendarService and fails closed if the durable record is missing, expired, mismatched, or already consumed. The request's prospect reference is stored separately from the hashed Cal.com payload so live policy can be rechecked at execution.
- Cancellation, rescheduling, automatic meeting-intent triggers, and OAuth setup remain unimplemented.
- Full repository CI and a live Cal.com sandbox smoke test have not yet passed in this workstream; live booking remains disabled by default.
