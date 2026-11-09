"""Deterministic acceptance contracts; no LibreChat login or production secret."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("qa_acceptance", ROOT / "scripts" / "qa_acceptance.py")
qa = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = qa
assert SPEC.loader is not None
SPEC.loader.exec_module(qa)


def test_synthetic_browser_chat_keeps_history_across_first_and_follow_up_messages() -> None:
    chat = qa.SyntheticBrowserSession("alice")

    conversation = chat.create_chat()
    first = chat.send(conversation, "First message", reply="First response")
    second = chat.send(conversation, "Continue with detail", reply="Follow-up response")

    assert conversation == "chat-1"
    assert first == "First response"
    assert second == "Follow-up response"
    assert chat.messages(conversation) == [
        ("user", "First message"),
        ("assistant", "First response"),
        ("user", "Continue with detail"),
        ("assistant", "Follow-up response"),
    ]


def test_synthetic_browser_chat_recovers_after_an_aborted_response() -> None:
    chat = qa.SyntheticBrowserSession("alice")
    conversation = chat.create_chat()

    chat.begin_response(conversation, "Long response")
    chat.abort_response(conversation)
    resumed = chat.send(conversation, "Continue after interruption", reply="Recovered response")

    assert resumed == "Recovered response"
    assert chat.messages(conversation)[-3:] == [
        ("assistant", "[aborted]"),
        ("user", "Continue after interruption"),
        ("assistant", "Recovered response"),
    ]


def test_synthetic_document_translation_reports_progress_and_delivers_warning_artifacts() -> None:
    jobs = qa.SyntheticDocumentTranslation()

    job_id = jobs.start("alice", "contract.docx", "ru")
    assert jobs.status("alice", job_id)["status"] == "queued"

    jobs.progress(job_id, done=2, total=5)
    running = jobs.status("alice", job_id)
    assert running["status"] == "running"
    assert running["progress"] == {"done": 2, "total": 5}

    jobs.complete_with_warnings(job_id, notes=["QA verification failed"])
    completed = jobs.status("alice", job_id)
    assert completed["status"] == "completed_with_warnings"
    assert completed["artifacts"] == [
        {"kind": "docx", "url": f"/translate/api/jobs/{job_id}/docx"},
        {"kind": "qa_report", "url": f"/translate/api/jobs/{job_id}/qa_report"},
    ]


def test_translation_job_and_artifacts_are_isolated_by_owner() -> None:
    jobs = qa.SyntheticDocumentTranslation()
    job_id = jobs.start("alice", "contract.docx", "en")
    jobs.complete_with_warnings(job_id)

    assert jobs.status("alice", job_id)["name"] == "contract.docx"
    assert jobs.status("bob", job_id) is None
    assert jobs.download("bob", job_id, "docx") is None
    assert jobs.download("alice", job_id, "docx") == f"/translate/api/jobs/{job_id}/docx"


def test_agent_recovery_audit_rejects_local_translation_workers_but_allows_remote_bridge() -> None:
    clean = [
        "python agents/translate_document_bridge.py uploads/contract.docx --to ru",
        "uvicorn app:app --host 0.0.0.0 --port 8000",
    ]
    stuck = clean + ["python local_translate.py --model /models/qwen --input contract.docx"]

    assert qa.find_stuck_local_translate_processes(clean) == []
    assert qa.find_stuck_local_translate_processes(stuck) == [stuck[-1]]


def test_platform_contract_declares_internal_services_and_safe_health_endpoints() -> None:
    contract = qa.platform_contract(ROOT)

    assert contract["safe_health_endpoints"] == {
        "doc-translate": "/translate/health",
        "quality-tools": "/health",
        "upstream-keypool": "/health",
        "ocr-ru": "/health",
        "litellm": "/health/liveliness",
    }
    assert contract["internal_only"] == {"quality-tools", "upstream-keypool"}
    assert contract["published_services"] == {"nginx"}


def test_live_health_arguments_require_http_urls_and_known_services() -> None:
    parsed = qa.parse_live_health_targets([
        "doc-translate=http://127.0.0.1:8000/translate/health",
        "quality-tools=http://127.0.0.1:8010/health",
    ])

    assert parsed["doc-translate"].endswith("/translate/health")
    assert parsed["quality-tools"].endswith("/health")

    for invalid in ("unknown=http://127.0.0.1/health", "doc-translate=ftp://example.test", "not-an-assignment"):
        try:
            qa.parse_live_health_targets([invalid])
        except ValueError:
            pass
        else:
            raise AssertionError(f"must reject {invalid!r}")


def test_component_runner_uses_document_translation_venv_when_present() -> None:
    interpreter = qa.component_python(ROOT / "doc-translate")

    assert interpreter.name.lower() in {"python.exe", "python"}
    assert ".venv" in {part.lower() for part in interpreter.parts}
