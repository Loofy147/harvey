# Add-ons and integration roadmap

## P0 — current change: safe calendar foundation
- [x] Add a Cal.com API v2 adapter for availability and approval-gated booking.
- [x] Require a caller-supplied approval verifier; default booking path denies.
- [x] Bound availability date range and duration.
- [x] Treat non-idempotent ambiguous booking outcomes as unknown.
- [x] Define the add-on manifest contract and example.
- [x] Connect approval verification to durable StateManager records.
- [x] Add operation ledger and manual reconciliation UI.
- [x] Wire calendar actions into the authenticated dashboard; booking requires a positive open conversation.
- [x] Recheck contact status, current email, do-not-contact list and positive conversation immediately before booking.
- [ ] Implement cancellation/rescheduling and provider lookup to reconcile unknown writes automatically where supported.
- [ ] Verify the full flow in repository CI and a Cal.com sandbox account.

## P1 — integration platform
- [ ] Add typed provider registry; reject duplicate add-on and capability IDs.
- [ ] Add connection setup/test, declared scopes, secret storage, health and quota reporting.
- [ ] Add capability policy engine: DENY / APPROVAL_REQUIRED / ALLOW.
- [ ] Add signed webhook receiver with replay protection and dead-letter handling.
- [ ] Add integration contract-test harness and captured sanitized fixtures.
- [ ] Add per-provider circuit breaker, retry budget and cancellation semantics.

## P2 — high-value business integrations
- [ ] Cal.com: durable booking reconciliation, cancellation and reschedule.
- [ ] Google Calendar and Microsoft Graph Calendar: availability first; write scopes kept separate.
- [ ] HubSpot: idempotent CRM sync, explicit field mapping/conflict policy, DNC/opt-out sync.
- [ ] Generic webhook trigger/action connector with signature verification.
- [ ] Scheduled signal re-observation that only creates reviewed suggestions until separately authorized.

## P3 — extensibility and multi-product
- [ ] Versioned add-on registry with compatibility checks, deprecation policy and signed release artifacts.
- [ ] Isolated execution for third-party code (manifest validation alone is not isolation).
- [ ] MCP/A2A bridge where each tool is explicit capability, provenance-bearing and separately authorized.
- [ ] Multi-product configuration with isolated ICP, offers, suppression lists, campaigns and metrics.
- [ ] Add-on upgrade/rollback, config migration, uninstall cleanup and export/delete data controls.

## Release checklist
- [ ] Threat model and data-flow review approved.
- [ ] Least-privilege provider scopes documented.
- [ ] No secret leakage in logs, errors, prompts or exports.
- [ ] DNC, opt-out, reply and human-approval gates regression-tested.
- [ ] No blind retry after ambiguous writes.
- [ ] Unit, contract, end-to-end, packaging and OS-matrix tests pass in GitHub Actions.
- [ ] Live smoke test produces re-checkable evidence without contacting real prospects.
- [ ] Docs clearly distinguish implemented, experimental and planned features.
