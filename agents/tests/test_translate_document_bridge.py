import importlib.util
import json
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location("translate_document_bridge", Path(__file__).parents[1] / "translate_document_bridge.py")
bridge = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(bridge)


def test_remote_translation_accepts_an_agent_upload_mount_or_attachment_filename_and_language():
    assert bridge.remote_request(["uploads/Supply Agreement.docx", "--to", "EN"]) == ("Supply Agreement.docx", "en")
    assert bridge.remote_request(["/opt/data/uploads/uuid__Supply Agreement.docx", "--to", "ru"]) == ("uuid__Supply Agreement.docx", "ru")

    for argv in (["/etc/passwd", "--to", "en"], ["uploads/nested/contract.docx", "--to", "en"],
                 ["uploads/contract.docx", "--to", "en", "--out", "outbox"], ["uploads/contract.docx"]):
        with pytest.raises(bridge.UsageError):
            bridge.remote_request(argv)


def test_remote_translation_uses_a_short_bounded_poll_and_returns_progress_link(capsys):
    calls = []

    class Router:
        def start(self, filename, target):
            calls.append(("start", filename, target))
            return {"job_id": "a" * 32, "progress_url": "/translate/?job=" + "a" * 32}

        def status(self, job_id):
            calls.append(("status", job_id))
            return {"status": "running", "progress": {"done": 2, "total": 10},
                    "progress_url": "/translate/?job=" + job_id}

    code = bridge.translate(["uploads/contract.docx", "--to", "ru"], Router(), poll_attempts=2, poll_seconds=0)

    assert code == 0
    assert calls == [("start", "contract.docx", "ru"), ("status", "a" * 32), ("status", "a" * 32)]
    output = capsys.readouterr().out
    assert "ещё выполняется" in output
    assert "/translate/?job=" + "a" * 32 in output


def test_remote_translation_returns_download_links_when_the_job_is_complete(capsys):
    class Router:
        def start(self, filename, target):
            return {"job_id": "b" * 32, "progress_url": "/translate/?job=" + "b" * 32}

        def status(self, job_id):
            return {"status": "completed_with_warnings", "notes": ["QA failed"], "progress_url": "/translate/?job=" + job_id,
                    "artifacts": [{"kind": "docx", "url": f"/translate/api/jobs/{job_id}/docx"},
                                  {"kind": "qa_report", "url": f"/translate/api/jobs/{job_id}/qa_report"}]}

    assert bridge.translate(["uploads/contract.docx", "--to", "ru"], Router(), poll_attempts=1, poll_seconds=0) == 0
    output = capsys.readouterr().out
    assert "ПРЕДУПРЕЖДЕНИЕМ" in output
    assert f"/translate/api/jobs/{'b' * 32}/docx" in output
    assert f"/translate/api/jobs/{'b' * 32}/qa_report" in output


def test_only_explicitly_configured_safe_local_extract_or_apply_can_run(monkeypatch):
    monkeypatch.delenv("TRANSLATE_DOCUMENT_LOCAL", raising=False)
    assert bridge.local_command(["uploads/contract.docx", "--to", "ru", "--extract", "--out", "work"]) is None

    monkeypatch.setenv("TRANSLATE_DOCUMENT_LOCAL", "/opt/tools/translate-document-local")
    assert bridge.local_command(["uploads/contract.docx", "--to", "ru", "--extract", "--out", "work"]) == [
        "/opt/tools/translate-document-local", "uploads/contract.docx", "--to", "ru", "--extract", "--out", "work"]
    with pytest.raises(bridge.UsageError):
        bridge.local_command(["uploads/contract.docx", "--to", "ru", "--extract", "--apply"])
    with pytest.raises(bridge.UsageError):
        bridge.local_command(["uploads/contract.docx", "--to", "ru", "--extract", "--unsafe-model-option"])
