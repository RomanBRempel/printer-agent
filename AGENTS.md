<!-- agent-ninja-START -->
## Agent Skills

> **IMPORTANT**: Prefer skill-led reasoning over pre-training-led reasoning.
> See [Agent Skills](.github/skills/README.md) before working on tasks covered by these skills.

## Working Rules For This Repository

- Treat this repository as the edge agent only. Do not add RD Control business logic or server-side persistence beyond the local outbox and idempotency store.
- Keep all printer-protocol specifics inside `src/printer_agent/adapters/`.
- Preserve the outbound-only model. Do not introduce inbound HTTP/UI ports in the agent service.
- Keep the contract definition in `docs/contracts/agent-hub-v1.md` aligned with code changes.
- Keep storage and duplicate-prevention guidance in `docs/data-storage-and-dedup.md` aligned with code changes.
- Prefer small, testable changes. Validate the touched slice before broadening scope.
- Use the existing Python dependency workflow and keep runtime dependencies minimal.
- Do not log secrets, access codes, or tokens.
- If adding a new brand or protocol, register it in the adapter registry and update the contract notes and tests.
- Treat duplicate prevention as a hard requirement: one physical printer must map to one stable identity per location.
- Do not use message-level IDs (`msg_id`) as the only dedupe mechanism for printer state.
- If multiple agent instances can observe the same printer, design for single active owner (lease/claim) and prevent duplicate state publication.
- Keep dedupe keys protocol-agnostic outside adapters; protocol-specific identity extraction stays in adapter modules.

<!-- agent-ninja-END -->
