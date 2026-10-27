# Harvey integrations and add-ons architecture

## Design goal
Extend Harvey through small, explicit, testable provider adapters while preserving its local-first runtime, governed observations, mail safety gates and human approval model. A plug-in must not automatically become a trusted agent.

## Layers
1. Provider adapter: typed inputs/outputs; fixed HTTPS hosts; short timeouts; normalized errors; redacted logs; no business policy decisions.
2. Capability contract: stable capability IDs (for example calendar.availability.read), read/write mode, data categories, vendor hosts and rate limits.
3. Policy gate: authorization DENY / APPROVAL_REQUIRED / ALLOW; fail closed when required context or approval cannot be verified.
4. Durable operation ledger: idempotency key for each internal operation, provider receipt, status, attempt count, last error, reconciliation outcome and timestamps.
5. Orchestrator: schedules safe reads; stages writes for explicit user review; never treats an agent response as proof that an external action occurred.
6. Dashboard: connection health, scope display, test-only actions, pending approvals, recent operations, costs/quotas, failed jobs and reconciliation queue.

## Add-on manifest v1
Use docs/specs/ADDON_MANIFEST.schema.json as a proposed stable contract. Each add-on must declare ID/version, entrypoint, capabilities, permissions, data access/retention, outbound hosts, request budgets, health check and owner. The sample is illustrative: validation of a manifest does not sandbox code.

## Security boundaries
- Prefer allowlisted, fixed HTTPS hosts and reject redirects by default.
- Store secrets only in local secret configuration; never in harvey.yaml, logs, observations, prompts or exported CSV.
- External content (HTML, emails, CRM notes, webhook payloads and MCP tool output) is untrusted data, not instructions.
- Add-on code runs with the process's operating-system permissions unless a real sandbox is implemented; manifests alone do not provide isolation.
- Enforce opt-out/do-not-contact and approval at action time, not only at discovery or draft time.
- Validate webhook signatures, timestamps, replay windows and event IDs before processing.
- Separate observed facts, inferred values and user-confirmed claims with provenance and freshness.

## Initial integrations
- Cal.com: availability and approval-gated booking foundation.
- Google Calendar and Microsoft Graph Calendar: scoped availability reads first; booking writes require explicit OAuth grants and durable approval.
- HubSpot CRM: idempotent contact/company sync with field mapping, explicit conflict policy and DNC propagation; use narrow read/write scopes.
- Webhooks: signed inbound event envelope, deduplication, replay protection, bounded retries and dead-letter visibility.
- MCP/A2A: adapter boundary only; discovery does not grant authority; each tool/capability must declare a permission and output provenance.

## Required telemetry
Record trace ID, add-on ID/version, capability ID, policy decision, approval reference (never secret), normalized request hash, latency, retries, rate-limit state, provider receipt, outcome and verifier version. Avoid recording attendee message content or API credentials.

## Release gate
No integration is production-ready until offline unit tests, provider contract tests, auth/permission failure tests, timeout/ambiguous-write tests, packaging tests, end-to-end tests and CI on supported Python/OS targets pass. A mock pass is not a live-provider proof.
