# Architecture

## Runtime planes

1. **Edge and identity**: Nginx terminates TLS and routes browser traffic to LibreChat and approved internal application endpoints.
2. **Shared AI services**: LiteLLM, the PII guard, Presidio, OCR and document translation run as internal services.
3. **Agent execution**: `agents-router` routes an authenticated employee request to that employee's Hermes or OpenClaw runtime. The router alone holds the shared LiteLLM credential.
4. **State and operations**: LibreChat databases/uploads, agent state, metrics, backups and restore tooling.

## Boundary rules

- Only approved edge services may publish host ports.
- Internal services use Docker networks; no internal API may be treated as public.
- Runtime secrets live in protected environment files or an approved secret store, never in Git.
- User uploads, generated files, agent memory, databases and logs are operational data, not source artifacts.
- Any new agent tool needs an explicit data-flow, network-egress and tenant-isolation review.

## Current limitations

This is a single-host pilot architecture. Stateful workloads and task metadata must be externalized before horizontal scaling or a wider production rollout.
