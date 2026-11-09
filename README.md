# Avtom1cGL Corporate AI Platform

Private deployment repository for the early-stage corporate chat-agent platform.

## Scope

The platform combines LibreChat with a private AI gateway and controlled agent runtimes:

```text
Employee → Nginx/TLS → LibreChat → LiteLLM + PII guard → corporate model gateway
                              ├─ local OCR
                              ├─ document translation
                              ├─ internal document-quality tools
                              └─ agents-router → per-user Hermes/OpenClaw runtimes
```

The repository contains deployable application code, Docker definitions, non-secret configuration, tests, operational scripts, and CI/CD metadata.

It intentionally does **not** contain runtime secrets, TLS private material, user uploads, databases, agent memories, reports, logs, or backups.

## Components

- `presidio-ru/` — Russian-language PII analysis.
- `litellm/` — model gateway configuration and PII masking guard.
- `ocr-ru/` — local OCR API for documents and images.
- `doc-translate/` — document translation and MCP integration.
- `quality-tools/` — internal-only local REST adapter for document evidence, Decimal invoice calculation, translation QA, anonymization audit, and approved document composition. It has no external HTTP client or secrets; user file paths are constrained to `/uploads` (read-only) and `/work`.
- `agents/` — per-user Hermes/OpenClaw lifecycle controller and router.
- `nginx/` — edge proxy configuration.
- `scripts/` — backup, restore, update and diagnostic utilities.
- `librechat/` — LibreChat integration configuration and skills.

## Quick start

1. Use a dedicated Ubuntu host with Docker Engine and Docker Compose v2.
2. Copy `.env.example` to `.env`; populate values through the approved secret-management process. Never commit `.env`.
3. Provision TLS material outside Git under `nginx/certs/` with restricted permissions.
4. Create the shared Docker network required by the deployment contract.
5. Review `docs/OPERATIONS.md`, then run the documented deployment procedure.

## Security posture

- The remote model receives text through the PII guard only.
- OCR, document translation, and `quality-tools` are internal services.
- `quality-tools` publishes no host port and mounts only LibreChat uploads read-only at `/uploads` plus its named work volume at `/work`.
- Agent containers must remain isolated from the public network and receive only least-privilege mounts.
- Production deployment is intentionally manual at this stage. The release workflow builds an auditable source artifact; it does not deploy to production.

See `docs/ARCHITECTURE.md`, `docs/SECURITY.md`, `docs/OPERATIONS.md`, and `docs/CI-CD.md`.
