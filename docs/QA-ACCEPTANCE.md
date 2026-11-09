# QA acceptance harness

This is the release-acceptance test matrix for the private chat-agent and document-quality platform. It deliberately separates deterministic, credential-free tests from the small set of checks that require a deployed LibreChat session.

## Runbook

Run from the repository root (Git Bash on Windows is supported):

```bash
python scripts/qa_acceptance.py --unit
# or
scripts/run-qa-acceptance.sh --unit
```

The default invocation also runs `--unit`. It uses only synthetic browser/session and document-job fixtures plus a static `docker-compose.yml` contract; it **does not** start Docker, call LibreChat, require a login, or read a secret.

Run the focused component contracts as part of a fuller pre-deploy verification:

```bash
python scripts/qa_acceptance.py --unit --components
```

`--components` runs these existing isolated suites in their own service directories so their module imports cannot collide:

- `agents/tests/test_translate_document_bridge.py`
- `agents/router/tests/test_document_translate_bridge.py`
- `keypool/tests/test_pool.py`
- `quality-tools/tests/test_quality_tools_api.py`
- `doc-translate/tests/test_doc_translate.py`

Some document-rendering tests may be skipped if LibreOffice (`soffice`) is absent. A skipped rendering test is not release evidence; run the same command in the built image/target host where LibreOffice is present.

### Opt-in, safe live probes

Only after the services are already deployed in an approved environment, health checks may be run without credentials. Each service must be named and the URL must exactly use the allow-listed health path:

```bash
python scripts/qa_acceptance.py --live-health \
  doc-translate=http://doc-translate:8000/translate/health \
  quality-tools=http://quality-tools:8000/health \
  upstream-keypool=http://upstream-keypool:8000/health \
  ocr-ru=http://ocr-ru:8000/health \
  litellm=http://litellm:4000/health/liveliness
```

The harness sends an unauthenticated `GET` only—no request body, cookies, or authorization header. Do not expose internal services merely to run this command. Run it from a permitted internal network/container instead.

To verify the agent host has not left a prohibited local model translation worker running (the command never terminates processes):

```bash
python scripts/qa_acceptance.py --audit-processes
```

For deterministic CI or review of a captured process list:

```bash
python scripts/qa_acceptance.py --processes-file /path/to/captured-processes.txt
```

The audit permits the remote `agents/translate_document_bridge.py` client and flags local worker command names such as `local_translate`, `translate_document_local`, or `docx_translate`.

## Automated acceptance matrix

| ID | Acceptance behavior | Deterministic evidence | Pass criteria |
|---|---|---|---|
| QA-CHAT-01 | Create a new chat | `test_synthetic_browser_chat_keeps_history_across_first_and_follow_up_messages` | A new conversation has a stable ID and contains the first turn. |
| QA-CHAT-02 | Send the first message | same test | The first user message is paired with exactly one assistant response. |
| QA-CHAT-03 | Continue a conversation | same test | A later user turn and response append to—not replace—the first turn. |
| QA-CHAT-04 | Continue after interrupted/aborted output | `test_synthetic_browser_chat_recovers_after_an_aborted_response` | The aborted turn is terminal and the next turn is accepted in the same chat. |
| QA-DOC-01 | Start document translation | `test_synthetic_document_translation_reports_progress_and_delivers_warning_artifacts`; router bridge suite | A job begins `queued` and has an opaque job ID. |
| QA-DOC-02 | Show progress | same test; router bridge suite | Progress transitions to `running` and exposes bounded `done`/`total`. |
| QA-DOC-03 | Deliver a `done_with_warnings` result | same test; `doc-translate` suite | Public status is `completed_with_warnings`, never success, and notes are retained. |
| QA-DOC-04 | Deliver safe artifacts | same test; router bridge suite | Warning result contains only produced DOCX and QA-report links. |
| QA-REC-01 | Agent recovery does not start a local translator | `test_agent_recovery_audit_rejects_local_translation_workers_but_allows_remote_bridge`; agent bridge suite | Normal `--to` calls use the bounded remote bridge; local worker names are flagged. |
| QA-REC-02 | No stuck local translation process | `--audit-processes` (opt-in) | Exit 0 and `PROCESS AUDIT PASSED`; no process is killed by the test. |
| QA-AUTH-01 | Document jobs/artifacts are owner-isolated | `test_translation_job_and_artifacts_are_isolated_by_owner`; router/doc-translate suites | A second user receives neither status nor download link. |
| QA-AUTH-02 | Internal service exposure is constrained | `test_platform_contract_declares_internal_services_and_safe_health_endpoints` | Only nginx publishes host ports; quality-tools and keypool remain internal. |
| QA-QUALITY-01 | Quality tools are deterministic and path-confined | `quality-tools/tests/test_quality_tools_api.py` | Health and documented operations succeed in temporary roots; traversal/absolute paths return 400. |
| QA-KEYPOOL-01 | Key pool distributes and quarantines retryable keys | `keypool/tests/test_pool.py` | Idle keys rotate; a 429 key is skipped while a healthy key exists. |
| QA-HEALTH-01 | Service-health endpoint contract | compose contract test | All expected services and explicit safe endpoint paths are present. |
| QA-HEALTH-02 | Running services respond | `--live-health …` (opt-in) | Every named endpoint returns HTTP 2xx/3xx without credentials. |

## Manual live acceptance (requires authorized LibreChat test accounts)

The repository intentionally does not automate authentication UI or use production credentials. Before release, an authorized tester must record the environment, account IDs (not tokens), timestamps, and screenshots/response IDs for these checks:

1. Sign in to LibreChat with **two separate approved test accounts**. Create a chat, send a first message, then send a follow-up; verify the expected conversation is retained.
2. Interrupt a streaming answer using the UI stop control. In the same chat, send a follow-up and verify the agent accepts it and returns a new answer rather than remaining stuck.
3. Attach a non-sensitive DOCX fixture, request translation, observe queued/running progress, then download every returned artifact. Force/use the approved warning fixture to verify the visible warning and QA JSON artifact before relying on any output.
4. From the second account, attempt to open the first account's saved job and each artifact URL. The result must be `404`/denied; repeat for another user's chat attachment.
5. Confirm the agent's document-translation path returns a progress URL for a long job and that the remote bridge remains responsive after an interrupted request. Then run `--audit-processes` on the agent host.
6. Run the allowed live-health command from the internal network, and inspect `upstream-keypool` health metrics without recording credentials.

Do not place source documents containing personal data, browser cookies, API keys, or screenshots with secrets in test evidence or this repository.

## Exit criteria

Release acceptance is complete only when `--unit --components` passes, all required target-host rendering tests run without skips, the applicable opt-in health/process checks pass, and every manual item above has recorded evidence. This harness is QA evidence only; it neither deploys nor changes a running service.
