"""Проверка распределителя: кто куда попадает, какие ключи уходят дальше, кто может скачать файл."""
import json
import os
import sys
import time

import jwt
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SECRET = "test-refresh-secret-0123456789abcdef0123"


@pytest.fixture()
def router(tmp_path, monkeypatch):
    users = {"users": {
        "ivanov@corp.local": {"id": "0123456789abcdef01234567", "slug": "ivanov", "dept": "legal", "agent_key": "ak-ivanov",
                              "engines": {"hermes": {"key": "hk-ivanov"}, "openclaw": {"key": "ok-ivanov"}}},
        "petrova@corp.local": {"id": "89abcdef0123456701234567", "slug": "petrova", "dept": "it", "agent_key": "ak-petrova",
                               "engines": {"hermes": {"key": "hk-petrova"}}},
    }}
    (tmp_path / "users.json").write_text(json.dumps(users), encoding="utf-8")
    for slug in ("ivanov", "petrova"):
        (tmp_path / "users" / slug / "files-hermes").mkdir(parents=True)
    (tmp_path / "users" / "ivanov" / "files-hermes" / "перевод.docx").write_bytes(b"DOCX")
    (tmp_path / "users" / "petrova" / "files-hermes" / "secret.txt").write_bytes(b"PETROVA")
    monkeypatch.setenv("USERS_FILE", str(tmp_path / "users.json"))
    monkeypatch.setenv("USERS_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("ROUTER_KEY", "rk-librechat")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-master")
    monkeypatch.setenv("JWT_REFRESH_SECRET", SECRET)
    sys.modules.pop("app", None)
    import app
    calls = []

    async def fake_proxy(url, headers, body):
        calls.append({"url": url, "headers": headers, "body": body})
        return app.JSONResponse({"ok": True})

    async def fake_agent(url, headers, body, engine, user, conv):
        return await fake_proxy(url, headers, body)
    monkeypatch.setattr(app, "_proxy", fake_proxy)
    monkeypatch.setattr(app, "_agent_proxy", fake_agent)
    from starlette.testclient import TestClient
    return TestClient(app.app), calls


def post(c, path, key, email=None, conv=None):
    h = {"Authorization": f"Bearer {key}"}
    if email:
        h["X-User-Email"] = email
    if conv:
        h["X-Conversation-Id"] = conv
    return c.post(path, headers=h, json={"model": "x", "messages": [{"role": "user", "content": "привет"}]})


def test_routing(router):
    c, calls = router
    assert post(c, "/hermes/v1/chat/completions", "wrong", "ivanov@corp.local").status_code == 401
    assert post(c, "/hermes/v1/chat/completions", "rk-librechat", "stranger@corp.local").status_code == 403
    r = post(c, "/openclaw/v1/chat/completions", "rk-librechat", "petrova@corp.local")      # нет OpenClaw
    assert r.status_code == 403 and "OpenClaw вам не подключён" in r.text and "«Агент Hermes»" in r.text
    assert post(c, "/hermes/v1/chat/completions", "rk-librechat", "Ivanov@Corp.Local", "conv-1").status_code == 200
    assert calls[-1]["url"] == "http://agent-hermes-ivanov:8642/v1/chat/completions"
    assert calls[-1]["headers"]["Authorization"] == "Bearer hk-ivanov"
    assert calls[-1]["headers"]["X-Hermes-Session-Key"] == "librechat:conv-1"
    assert calls[-1]["headers"]["X-Hermes-Session-Id"] == "librechat-conv-1"
    assert calls[-1]["body"]["model"] == "hermes-agent"
    hist = [{"role": "user", "content": "1"},
            {"role": "assistant", "content": "Ответ\n\n---\n⏱ Hermes: 12 с · оценить: [👍 хорошо](/agent-files/rate/"
                                             "0123456789abcdef/up) · [👎 плохо](/agent-files/rate/0123456789abcdef/down)"},
            {"role": "user", "content": "2"}]
    c.post("/hermes/v1/chat/completions", json={"model": "x", "messages": hist},
           headers={"Authorization": "Bearer rk-librechat", "X-User-Email": "ivanov@corp.local"})
    assert calls[-1]["body"]["messages"][1]["content"] == "Ответ"           # строка времени агенту не уходит
    hist = [{"role": "system", "content": "s"}, {"role": "user", "content": "1"},
            {"role": "assistant", "content": "a"}, {"role": "user", "content": "2"}]
    c.post("/openclaw/v1/chat/completions", json={"model": "x", "messages": hist},
           headers={"Authorization": "Bearer rk-librechat", "X-User-Email": "ivanov@corp.local", "X-Conversation-Id": "conv-2"})
    assert calls[-1]["url"] == "http://agent-openclaw-ivanov:18789/v1/chat/completions"
    assert calls[-1]["body"]["user"] == "librechat-conv-2" and calls[-1]["body"]["model"] == "openclaw"
    assert calls[-1]["body"]["messages"][0] == hist[0]                 # system message stays first
    assert calls[-1]["body"]["messages"][1]["role"] == "developer"
    assert "GENERAL" in calls[-1]["body"]["messages"][1]["content"]
    assert calls[-1]["body"]["messages"][2] == hist[3]                 # only the newest user message remains


def test_llm_keys(router):
    c, calls = router
    assert post(c, "/llm/v1/chat/completions", "hk-ivanov").status_code == 401        # ключ агента — не ключ API
    assert post(c, "/llm/v1/chat/completions", "rk-librechat").status_code == 401
    assert post(c, "/llm/v1/chat/completions", "ak-petrova").status_code == 200
    assert calls[-1]["url"].endswith("/chat/completions")
    assert calls[-1]["headers"]["Authorization"] == "Bearer sk-master"                # ключ шлюза знает только распределитель
    assert calls[-1]["body"]["user"] == "petrova@corp.local"


def test_agent_can_access_only_typed_quality_tool_routes(router, monkeypatch):
    """Agents authenticate with their existing per-user key; router is the only bridge."""
    import app
    import httpx

    forwarded = []

    def quality_tools(request):
        forwarded.append({"url": str(request.url), "headers": dict(request.headers), "json": json.loads(request.content)})
        return httpx.Response(200, json={"total_amount": "64.76"})

    app.client = httpx.AsyncClient(transport=httpx.MockTransport(quality_tools))
    c, _ = router
    payload = {"quantity": "3", "unit_price": "19.99", "discount_percent": "10", "vat_percent": "20", "currency": "EUR"}

    assert c.post("/quality/v1/calculator/invoice", json=payload).status_code == 401
    assert c.post("/quality/v1/calculator/invoice", headers={"Authorization": "Bearer rk-librechat"}, json=payload).status_code == 401
    response = c.post("/quality/v1/calculator/invoice", headers={"Authorization": "Bearer ak-petrova"}, json=payload)
    assert response.status_code == 200 and response.json() == {"total_amount": "64.76"}
    assert len(forwarded) == 1
    assert forwarded[0]["url"] == "http://quality-tools:8000/v1/calculator/invoice"
    assert forwarded[0]["json"] == payload
    assert "authorization" not in forwarded[0]["headers"]
    assert c.post("/quality/v1/not-an-endpoint", headers={"Authorization": "Bearer ak-petrova"}, json={}).status_code == 404


def test_quality_file_operations_are_scoped_to_calling_employee(router, monkeypatch):
    import app
    import httpx

    forwarded = []

    def quality_tools(request):
        forwarded.append(json.loads(request.content))
        return httpx.Response(200, json=[])

    app.client = httpx.AsyncClient(transport=httpx.MockTransport(quality_tools))
    c, _ = router
    response = c.post("/quality/v1/evidence/inventory", headers={"Authorization": "Bearer ak-petrova"},
                      json={"root": "uploads", "path": "."})

    assert response.status_code == 200
    assert forwarded == [{"root": "uploads", "path": "89abcdef0123456701234567/."}]


def test_files(router):
    c, _ = router
    assert c.get("/agent-files/hermes/перевод.docx").status_code == 401
    c.cookies.set("refreshToken", jwt.encode({"id": "0123456789abcdef01234567", "exp": int(time.time()) + 600},
                                             SECRET, algorithm="HS256"))
    r = c.get("/agent-files/hermes/перевод.docx")
    assert r.status_code == 200 and r.content == b"DOCX"
    assert "%D0%BF%D0%B5%D1%80%D0%B5%D0%B2%D0%BE%D0%B4%20%28Hermes%29.docx" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"
    assert c.get("/agent-files/hermes/../../petrova/files-hermes/secret.txt").status_code == 404
    assert c.get("/agent-files/hermes/%2e%2e/%2e%2e/petrova/files-hermes/secret.txt").status_code == 404
    assert c.get("/agent-files/hermes/secret.txt").status_code == 404                  # чужой файл не виден


def _sse_app(monkeypatch, tmp_path, sse: str):
    monkeypatch.setenv("STATS_DIR", str(tmp_path / "stats"))
    sys.modules.pop("app", None)
    import app
    import httpx

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())
    app.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return app


def test_agent_stream_footer_and_usage(monkeypatch, tmp_path):
    import asyncio
    sse = ('data: {"id":"c","created":1,"model":"m","choices":[{"index":0,"delta":{"content":"Привет"},"finish_reason":null}]}\n\n'
           'event: hermes.tool.progress\ndata: {"tool":"x"}\n\n'
           ': keepalive\n\n'
           'data: {"id":"c","created":1,"model":"m","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
           '"usage":{"prompt_tokens":600000}}\n\n'
           'data: {"id":"c","created":1,"model":"m","choices":[],"usage":{"prompt_tokens":600000}}\n\n'
           'data: [DONE]\n\n')
    app = _sse_app(monkeypatch, tmp_path, sse)
    user = {"slug": "ivanov", "id": "0123456789abcdef01234567", "dept": "legal"}

    async def run():
        resp = await app._agent_proxy("http://x/v1/chat/completions", {}, {}, "hermes", user, "conv-1")
        return "".join([c async for c in resp.body_iterator])
    out = asyncio.run(run())
    assert "hermes.tool.progress" not in out and "usage" not in out and "600000" not in out
    assert out.index("⏱ Hermes") < out.index('"finish_reason": "stop"') < out.index("[DONE]")   # строка до конца ответа
    assert out.count("⏱") == 1 and "/agent-files/rate/" in out
    rec = json.loads((tmp_path / "stats" / "runs.jsonl").read_text(encoding="utf-8"))
    assert rec["engine"] == "hermes" and rec["slug"] == "ivanov" and rec["status"] == "ok"


def test_rating(monkeypatch, tmp_path, router):
    c, _ = router
    import app
    monkeypatch.setattr(app, "STATS", tmp_path / "stats")
    app.runs["0123456789abcdef"] = {"run": "0123456789abcdef", "engine": "openclaw", "slug": "ivanov",
                                    "uid": "0123456789abcdef01234567", "seconds": 75}
    assert c.get("/agent-files/rate/0123456789abcdef/up").status_code == 401
    c.cookies.set("refreshToken", jwt.encode({"id": "89abcdef0123456701234567", "exp": int(time.time()) + 600},
                                             SECRET, algorithm="HS256"))
    assert c.get("/agent-files/rate/0123456789abcdef/up").status_code == 404           # чужой ответ
    c.cookies.set("refreshToken", jwt.encode({"id": "0123456789abcdef01234567", "exp": int(time.time()) + 600},
                                             SECRET, algorithm="HS256"))
    r = c.get("/agent-files/rate/0123456789abcdef/down")
    assert r.status_code == 200 and "1 мин 15 с" in r.text
    c.post("/agent-files/rate/0123456789abcdef/down", data={"comment": "сломалась вёрстка"})
    lines = [json.loads(l) for l in (tmp_path / "stats" / "ratings.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [l["score"] for l in lines] == ["down", "down"] and lines[-1]["comment"] == "сломалась вёрстка"
    assert lines[-1]["engine"] == "openclaw"
