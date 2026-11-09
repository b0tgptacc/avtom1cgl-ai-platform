"""Regression coverage for the async document-translation agent bridge."""
import asyncio
import json

import httpx
import pytest

from test_router import router  # reuse the isolated users.json/TestClient fixture


def _transport(app, handler):
    app.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_start_requires_agent_key_and_forwards_only_derived_user_and_filename(router):
    client, _calls = router
    import app

    forwarded = []

    def doc_translate(request):
        forwarded.append({"url": str(request.url), "headers": dict(request.headers), "json": json.loads(request.content)})
        return httpx.Response(200, json={"id": "a" * 32, "name": "Contract.docx"})

    _transport(app, doc_translate)

    assert client.post("/document-translate/v1/jobs", json={"filename": "Contract.docx", "target": "en"}).status_code == 401
    bad = client.post("/document-translate/v1/jobs", headers={"Authorization": "Bearer ak-ivanov"},
                      json={"filename": "../other-user.docx", "target": "en"})
    assert bad.status_code == 400
    assert forwarded == []

    response = client.post("/document-translate/v1/jobs", headers={"Authorization": "Bearer ak-ivanov"},
                           json={"filename": "Contract.docx", "target": "en", "user": "attacker"})

    assert response.status_code == 202
    assert response.json()["job_id"] == "a" * 32
    assert response.json()["progress_url"] == "/translate/?job=" + "a" * 32
    assert len(forwarded) == 1
    assert forwarded[0]["url"] == "http://doc-translate:8000/internal/jobs"
    assert forwarded[0]["headers"]["x-internal-key"] == ""
    assert forwarded[0]["json"] == {"user": "0123456789abcdef01234567", "filename": "Contract.docx", "target": "en"}


@pytest.mark.parametrize("filename", ["", ".", "..", "/etc/passwd", "uploads/contract.docx", "C:\\temp\\contract.docx", "a\x00.docx"])
def test_start_rejects_any_non_filename(router, filename):
    client, _calls = router

    response = client.post("/document-translate/v1/jobs", headers={"Authorization": "Bearer ak-ivanov"},
                           json={"filename": filename, "target": "en"})

    assert response.status_code == 400


def test_status_maps_internal_state_and_returns_session_protected_artifact_links(router):
    client, _calls = router
    import app

    def doc_translate(request):
        assert request.url.params["user"] == "89abcdef0123456701234567"
        assert request.headers["x-internal-key"] == ""
        return httpx.Response(200, json={
            "status": "done_with_warnings", "stage": "готово с предупреждениями", "done": 5, "total": 5,
            "notes": ["QA failed"], "files": {"docx": "contract.uncertain.docx", "qa_report": "qa.json"},
            "name": "Contract.docx",
        })

    _transport(app, doc_translate)
    job_id = "b" * 32
    response = client.get(f"/document-translate/v1/jobs/{job_id}", headers={"Authorization": "Bearer ak-petrova"})

    assert response.status_code == 200
    assert response.json() == {
        "job_id": job_id, "name": "Contract.docx", "status": "completed_with_warnings",
        "progress": {"stage": "готово с предупреждениями", "done": 5, "total": 5}, "notes": ["QA failed"],
        "artifacts": [
            {"kind": "docx", "url": f"/translate/api/jobs/{job_id}/docx"},
            {"kind": "qa_report", "url": f"/translate/api/jobs/{job_id}/qa_report"},
        ],
        "progress_url": f"/translate/?job={job_id}",
    }


@pytest.mark.parametrize(("internal", "external"), [
    ("queued", "pending"), ("running", "running"), ("done", "completed"),
    ("done_with_warnings", "completed_with_warnings"), ("error", "failed"),
])
def test_status_mapping_is_stable(internal, external):
    import app

    assert app._public_document_status({"status": internal}) == external


def test_document_bridge_returns_gateway_timeout_without_retrying(router):
    client, _calls = router
    import app

    def timeout(_request):
        raise httpx.ReadTimeout("slow")

    _transport(app, timeout)
    response = client.post("/document-translate/v1/jobs", headers={"Authorization": "Bearer ak-ivanov"},
                           json={"filename": "Contract.docx", "target": "en"})

    assert response.status_code == 504


def test_document_bridge_preserves_client_disconnect_cancellation(monkeypatch):
    import app

    class CancellingClient:
        async def post(self, *_args, **_kwargs):
            raise asyncio.CancelledError()

    monkeypatch.setattr(app, "client", CancellingClient())

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app._document_request("POST", "/internal/jobs", json={}))
