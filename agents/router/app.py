"""Распределитель стенда агентов: LibreChat → контейнер агента этого сотрудника (Hermes или OpenClaw).

  POST /<движок>/v1/chat/completions  — от LibreChat (ключ ROUTER_KEY, сотрудник — заголовок X-User-Email)
  POST /llm/v1/chat/completions       — от агентов к модели: ключ агента → сотрудник, дальше в шлюз LiteLLM
                                        (маскирование ПДн) общим ключом; агенты ключа шлюза не видят
  GET  /agent-files/<движок>/<файл>   — файлы, которые агент положил в outbox сотрудника; вход — сессия LibreChat
  GET  /agent-files/rate/<запуск>/up|down — оценка ответа (ссылки под ответом), POST — замечание к оценке

Под каждым ответом агента — строка «⏱ время · оценить 👍 / 👎»: в режиме двух бесед LibreChat хранит оба ответа
одним сообщением и своей оценки у второй колонки нет. Время и оценки — в stats/runs.jsonl и stats/ratings.jsonl.
Расход токенов (usage) агентов в LibreChat не передаётся: это сумма всех внутренних шагов агента, LibreChat принял бы
её за размер переписки и показал бы переполненное окно контекста.

Список сотрудников и ключей — users.json, его пишет stand.py. Перечитывается при изменении файла."""
import json
import logging
import os
import re
import time
import uuid
from html import escape
from pathlib import Path

import httpx
import jwt
from profiles import apply_profile_policy
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

USERS_FILE = Path(os.getenv("USERS_FILE", "/data/users.json"))
USERS_DIR = Path(os.getenv("USERS_DIR", "/data/users"))
STATS = Path(os.getenv("STATS_DIR", "/data/stats"))
ROUTER_KEY = os.getenv("ROUTER_KEY", "")
LLM_BASE = os.getenv("LLM_BASE", "http://litellm:4000/v1").rstrip("/")
LLM_KEY = os.getenv("LITELLM_MASTER_KEY", "")
SECRET = os.getenv("JWT_REFRESH_SECRET", "")
QUALITY_BASE = os.getenv("QUALITY_TOOLS_BASE", "http://quality-tools:8000").rstrip("/")
DOC_TRANSLATE_BASE = os.getenv("DOC_TRANSLATE_BASE", "http://doc-translate:8000").rstrip("/")
DOC_INTERNAL_KEY = os.getenv("DOC_INTERNAL_KEY", "")
DOC_TIMEOUT = float(os.getenv("DOC_TRANSLATE_TIMEOUT", "15"))
QUALITY_OPERATIONS = ("evidence/inventory", "evidence/records", "evidence/coverage", "calculator/invoice", "translation-qa", "anonymization-audit", "document-compose/offer", "document-compose/invoice", "document-compose/reports")
TIMEOUT = httpx.Timeout(float(os.getenv("AGENT_TIMEOUT", "1800")), connect=10)
ENGINES = {
    "hermes": {"model": "hermes-agent", "port": 8642, "title": "Hermes"},
    "openclaw": {"model": "openclaw", "port": 18789, "title": "OpenClaw"},
}
NAME_RE = re.compile(r"^[^/\\\x00]{1,200}$")
FOOTER_RE = re.compile(r"\n*---\n⏱ [^\n]*/agent-files/rate/[0-9a-f]{16}/down\)\s*$")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("agents-router")
client = httpx.AsyncClient(timeout=TIMEOUT)
_users = {"mtime": 0.0, "data": {}}


def users() -> dict:
    """{email: {...}} из users.json; перечитывается, когда stand.py его обновил."""
    try:
        mtime = USERS_FILE.stat().st_mtime
    except FileNotFoundError:
        return {}
    if mtime != _users["mtime"]:
        _users["data"] = {k.lower(): v for k, v in json.loads(USERS_FILE.read_text(encoding="utf-8"))["users"].items()}
        _users["mtime"] = mtime
    return _users["data"]


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth[7:] if auth.lower().startswith("bearer ") else ""


def _deny(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": "agents_router"}}, status_code=status)


async def _proxy(url: str, headers: dict, body: dict) -> Response:
    """Запрос агента к модели — дальше в шлюз, поток ответа как есть."""
    req = client.build_request("POST", url, headers=headers, json=body)
    resp = await client.send(req, stream=True)
    if "text/event-stream" not in resp.headers.get("content-type", ""):
        data = await resp.aread()
        await resp.aclose()
        return Response(data, status_code=resp.status_code, media_type=resp.headers.get("content-type") or "application/json")

    async def stream():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()
    return StreamingResponse(stream(), status_code=resp.status_code, media_type="text/event-stream")


runs: dict = {}                                   # запуск → запись (для проверки, чей запуск оценивают)


def _record(kind: str, rec: dict) -> None:
    try:
        STATS.mkdir(parents=True, exist_ok=True)
        with (STATS / f"{kind}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("stats %s: %s", kind, e)


def _duration(sec: float) -> str:
    m, s = divmod(int(round(sec)), 60)
    return f"{m} мин {s} с" if m else f"{s} с"


def _footer(engine: str, run_id: str, sec: float) -> str:
    return (f"\n\n---\n⏱ {ENGINES[engine]['title']}: {_duration(sec)} · оценить: "
            f"[👍 хорошо](/agent-files/rate/{run_id}/up) · [👎 плохо](/agent-files/rate/{run_id}/down)")


async def _agent_proxy(url: str, headers: dict, body: dict, engine: str, user: dict, conv: str) -> Response:
    """Ответ агента для LibreChat: без служебных событий SSE и без usage, со строкой времени и оценки в конце."""
    run_id, started = uuid.uuid4().hex[:16], time.time()
    state = {"footer": False}

    def finish(status: str) -> None:
        rec = {"run": run_id, "ts": int(started), "engine": engine, "slug": user["slug"], "uid": user.get("id", ""),
               "dept": user.get("dept", ""), "conv": conv[:40], "seconds": round(time.time() - started, 1),
               "status": status}
        runs[run_id] = rec
        if len(runs) > 5000:
            runs.pop(next(iter(runs)))
        _record("runs", rec)
        log.info("done %s %s %.0f s %s", engine, user["slug"], rec["seconds"], status)

    req = client.build_request("POST", url, headers=headers, json=body)
    resp = await client.send(req, stream=True)
    if "text/event-stream" not in resp.headers.get("content-type", ""):
        data = await resp.aread()
        await resp.aclose()
        if resp.status_code == 200:
            try:
                d = json.loads(data)
                d.pop("usage", None)
                msg = d["choices"][0]["message"]
                msg["content"] = (msg.get("content") or "") + _footer(engine, run_id, time.time() - started)
                data = json.dumps(d, ensure_ascii=False).encode()
            except (ValueError, KeyError, IndexError, TypeError):
                pass
        finish("ok" if resp.status_code == 200 else f"http {resp.status_code}")
        return Response(data, status_code=resp.status_code, media_type="application/json")

    def footer_chunk(like: dict) -> str:
        state["footer"] = True
        chunk = {"id": like.get("id", run_id), "object": "chat.completion.chunk", "created": like.get("created", 0),
                 "model": like.get("model", ENGINES[engine]["model"]),
                 "choices": [{"index": 0, "delta": {"content": _footer(engine, run_id, time.time() - started)},
                              "finish_reason": None}]}
        return "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"

    last: dict = {}

    def transform(block: str) -> list:
        lines = block.splitlines()
        if any(line.startswith("event:") for line in lines):         # hermes.tool.progress и т. п.
            return []
        data = "\n".join(line[5:].lstrip() for line in lines if line.startswith("data:"))
        if not data:
            return [block + "\n\n"]                                 # «: keepalive» — как есть
        if data == "[DONE]":
            return ([] if state["footer"] else [footer_chunk(last)]) + ["data: [DONE]\n\n"]
        try:
            chunk = json.loads(data)
        except ValueError:
            return [block + "\n\n"]
        chunk.pop("usage", None)
        choices = chunk.get("choices") or []
        if not choices:
            return []                                                # только usage
        last.update({k: chunk[k] for k in ("id", "created", "model") if k in chunk})
        out = []
        if not state["footer"] and any(c.get("finish_reason") for c in choices):
            out.append(footer_chunk(chunk))
        out.append("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n")
        return out

    async def stream():
        buf, status = "", "ok"
        try:
            async for text in resp.aiter_text():
                buf += text.replace("\r\n", "\n")
                while "\n\n" in buf:
                    block, buf = buf.split("\n\n", 1)
                    for piece in transform(block):
                        yield piece
            if buf.strip():
                for piece in transform(buf):
                    yield piece
        except Exception as e:                                       # обрыв — тоже в статистику
            status = type(e).__name__
            raise
        finally:
            await resp.aclose()
            finish(status if resp.status_code == 200 else f"http {resp.status_code}")
    return StreamingResponse(stream(), status_code=resp.status_code, media_type="text/event-stream")


async def agent_chat(request: Request) -> Response:
    engine = request.path_params["engine"]
    if engine not in ENGINES:
        return _deny(404, "нет такого агента")
    if not ROUTER_KEY or _bearer(request) != ROUTER_KEY:
        return _deny(401, "неверный ключ")
    email = request.headers.get("x-user-email", "").strip().lower()
    user = users().get(email)
    if not user:
        return _deny(403, f"Стенд агентов: {email or 'почта не передана'} нет в списке тестировщиков. "
                          "Обратитесь к администратору.")
    if engine not in user.get("engines", {}):
        mine = ", ".join(ENGINES[e]["title"] for e in user.get("engines", {}) if e in ENGINES)
        return _deny(403, f"Стенд агентов: {ENGINES[engine]['title']} вам не подключён — выберите «Агент {mine}».")
    try:
        body = await request.json()
    except ValueError:
        return _deny(400, "тело запроса должно быть JSON")
    if not isinstance(body, dict):
        return _deny(400, "тело запроса должно быть JSON-объектом")
    body["model"] = ENGINES[engine]["model"]
    for m in body.get("messages") or []:                # строку «⏱ время · оценить» агенту в истории не отдаём
        if m.get("role") == "assistant" and isinstance(m.get("content"), str):
            m["content"] = FOOTER_RE.sub("", m["content"])
    body, profile = apply_profile_policy(body)
    conv = request.headers.get("x-conversation-id", "")
    headers = {"Authorization": f"Bearer {user['engines'][engine]['key']}"}
    if conv and re.fullmatch(r"[\w-]{1,100}", conv):
        # один чат LibreChat = одна сессия агента. Историю (с шагами и результатами инструментов) агент хранит
        # сам, поэтому ему — только новое сообщение: вся переписка от LibreChat удвоила бы её в подсказке.
        # Hermes без явной сессии выводит её из первого сообщения: два чата, начатые одной фразой, смешались бы.
        if engine == "openclaw":
            body["user"] = f"librechat-{conv}"
        else:
            headers["X-Hermes-Session-Id"] = f"librechat-{conv}"
            headers["X-Hermes-Session-Key"] = f"librechat:{conv}"
        msgs = body.get("messages") or []
        body["messages"] = [m for m in msgs if m.get("role") in ("system", "developer")] + \
            [m for m in msgs if m.get("role") == "user"][-1:]
    log.info("chat %s %s profile=%s conv=%s stream=%s", engine, user["slug"], profile, conv[:8], bool(body.get("stream")))
    url = f"http://agent-{engine}-{user['slug']}:{ENGINES[engine]['port']}/v1/chat/completions"
    try:
        return await _agent_proxy(url, headers, body, engine, user, conv)
    except httpx.ConnectError:
        return _deny(503, f"Агент {engine} ещё запускается или остановлен — повторите через минуту")
    except httpx.HTTPError as e:                         # агент оборвал ответ: перезапуск, нехватка памяти
        log.warning("agent %s %s: %s", engine, user["slug"], type(e).__name__)
        _record("runs", {"run": "", "ts": int(time.time()), "engine": engine, "slug": user["slug"],
                         "uid": user.get("id", ""), "dept": user.get("dept", ""), "conv": conv[:40],
                         "seconds": 0, "status": type(e).__name__})
        return _deny(502, f"Агент {ENGINES[engine]['title']} прервал ответ (перезапуск или нехватка ресурсов). "
                          "Повторите сообщение; если повторится — сообщите администратору.")


async def agent_models(request: Request) -> Response:
    engine = request.path_params["engine"]
    if engine not in ENGINES or _bearer(request) != ROUTER_KEY:
        return _deny(401, "неверный ключ")
    return JSONResponse({"object": "list", "data": [{"id": ENGINES[engine]["model"], "object": "model"}]})


def _by_agent_key(key: str):
    for email, u in users().items():
        if key and key == u.get("agent_key"):
            return email, u
    return None, None


def _valid_filename(filename: object) -> bool:
    """Agent bridge accepts a display filename, never an agent-local filesystem path."""
    return isinstance(filename, str) and bool(NAME_RE.fullmatch(filename)) and filename not in (".", "..")


async def _document_request(method: str, path: str, **kwargs) -> httpx.Response:
    """One short, cancellable hop to doc-translate; deliberately no retries or long polling."""
    headers = {"X-Internal-Key": DOC_INTERNAL_KEY}
    url = f"{DOC_TRANSLATE_BASE}{path}"
    if method == "POST":
        return await client.post(url, headers=headers, timeout=DOC_TIMEOUT, **kwargs)
    return await client.get(url, headers=headers, timeout=DOC_TIMEOUT, **kwargs)


def _public_document_status(job: dict) -> str:
    return {"queued": "pending", "running": "running", "done": "completed",
            "done_with_warnings": "completed_with_warnings", "error": "failed"}.get(job.get("status"), "failed")


def _document_result(job_id: str, job: dict) -> dict:
    files = job.get("files") if isinstance(job.get("files"), dict) else {}
    return {
        "job_id": job_id,
        "name": job.get("name", ""),
        "status": _public_document_status(job),
        "progress": {"stage": job.get("stage", ""), "done": job.get("done", 0), "total": job.get("total", 0)},
        "notes": job.get("notes") if isinstance(job.get("notes"), list) else [],
        "artifacts": [{"kind": kind, "url": f"/translate/api/jobs/{job_id}/{kind}"}
                      for kind in ("docx", "pdf", "qa_report") if files.get(kind)],
        "progress_url": f"/translate/?job={job_id}",
        **({"error": job["error"]} if isinstance(job.get("error"), str) else {}),
    }


async def document_translate_start(request: Request) -> Response:
    _email, user = _by_agent_key(_bearer(request))
    if not user:
        return _deny(401, "неверный ключ агента")
    try:
        body = await request.json()
    except ValueError:
        return _deny(400, "тело запроса должно быть JSON")
    if not isinstance(body, dict) or not _valid_filename(body.get("filename")):
        return _deny(400, "нужно только имя прикреплённого файла без пути")
    target = body.get("target")
    if not isinstance(target, str) or not target:
        return _deny(400, "нужен язык перевода")
    user_id = str(user.get("id") or "")
    if not re.fullmatch(r"[0-9a-f]{24}", user_id):
        return _deny(403, "у сотрудника нет каталога вложений")
    try:
        response = await _document_request("POST", "/internal/jobs", json={
            "user": user_id, "filename": body["filename"], "target": target.lower(),
        })
    except httpx.ConnectError:
        return _deny(503, "doc-translate недоступен")
    except httpx.TimeoutException:
        return _deny(504, "doc-translate не ответил вовремя")
    if response.status_code != 200:
        return Response(response.content, status_code=response.status_code,
                        media_type=response.headers.get("content-type", "application/json"))
    try:
        created = response.json()
        job_id = created["id"]
    except (ValueError, KeyError, TypeError):
        return _deny(502, "doc-translate вернул неверный ответ")
    if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return _deny(502, "doc-translate вернул неверный идентификатор задания")
    return JSONResponse({"job_id": job_id, "name": created.get("name", body["filename"]),
                         "status": "pending", "progress_url": f"/translate/?job={job_id}"}, status_code=202)


async def document_translate_status(request: Request) -> Response:
    _email, user = _by_agent_key(_bearer(request))
    if not user:
        return _deny(401, "неверный ключ агента")
    job_id = request.path_params["job_id"]
    user_id = str(user.get("id") or "")
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return _deny(404, "задание не найдено")
    if not re.fullmatch(r"[0-9a-f]{24}", user_id):
        return _deny(403, "у сотрудника нет каталога вложений")
    try:
        response = await _document_request("GET", f"/internal/jobs/{job_id}", params={"user": user_id})
    except httpx.ConnectError:
        return _deny(503, "doc-translate недоступен")
    except httpx.TimeoutException:
        return _deny(504, "doc-translate не ответил вовремя")
    if response.status_code != 200:
        return Response(response.content, status_code=response.status_code,
                        media_type=response.headers.get("content-type", "application/json"))
    try:
        job = response.json()
    except ValueError:
        return _deny(502, "doc-translate вернул неверный ответ")
    if not isinstance(job, dict):
        return _deny(502, "doc-translate вернул неверный ответ")
    return JSONResponse(_document_result(job_id, job))


def _scoped_quality_body(operation: str, body: dict, user: dict) -> dict:
    """Translate agent-local paths to the caller's isolated quality-tools roots."""
    result = json.loads(json.dumps(body))
    uploads_prefix = str(user.get("id") or "").strip()
    work_prefix = user["slug"]

    def scope_location(location: dict) -> None:
        root = location.get("root")
        if root == "uploads":
            if not uploads_prefix:
                raise ValueError("у сотрудника нет каталога вложений")
            location["path"] = f"{uploads_prefix}/{location.get('path', '.') }"
        elif root == "work":
            location["path"] = f"{work_prefix}/{location.get('path', '.') }"

    if operation == "evidence/inventory":
        scope_location(result)
    elif operation == "evidence/records" and isinstance(result.get("paths"), list):
        root = result.get("root")
        if root == "uploads" and not uploads_prefix:
            raise ValueError("у сотрудника нет каталога вложений")
        prefix = uploads_prefix if root == "uploads" else work_prefix
        result["paths"] = [f"{prefix}/{path}" for path in result["paths"]]
    elif operation == "translation-qa" and isinstance(result.get("required_artifacts"), list):
        for location in result["required_artifacts"]:
            if isinstance(location, dict):
                scope_location(location)
    elif operation == "document-compose/reports" and isinstance(result.get("output_dir"), str):
        result["output_dir"] = f"{work_prefix}/{result['output_dir']}"
    return result


async def quality_call(request: Request, operation: str | None = None) -> Response:
    _email, user = _by_agent_key(_bearer(request))
    if not user:
        return _deny(401, "неверный ключ агента")
    operation = operation or request.path_params["operation"]
    try:
        body = await request.json()
    except ValueError:
        return _deny(400, "тело запроса должно быть JSON")
    if not isinstance(body, dict):
        return _deny(400, "тело запроса должно быть JSON-объектом")
    try:
        scoped_body = _scoped_quality_body(operation, body, user)
    except ValueError as error:
        return _deny(403, str(error))
    try:
        response = await client.post(f"{QUALITY_BASE}/v1/{operation}", json=scoped_body)
    except httpx.ConnectError:
        return _deny(503, "quality-tools недоступен")
    except httpx.TimeoutException:
        return _deny(504, "quality-tools не ответил вовремя")
    return Response(response.content, status_code=response.status_code, media_type=response.headers.get("content-type", "application/json"))


def quality_endpoint(operation: str):
    async def endpoint(request: Request) -> Response:
        return await quality_call(request, operation)
    return endpoint


MCP_TOOL_DEFINITIONS = (
    ("evidence_inventory", "evidence/inventory", "Inventory files beneath an isolated uploads or work root."),
    ("evidence_records", "evidence/records", "Extract canonical, source-located records from selected files."),
    ("evidence_coverage", "evidence/coverage", "Assess deterministic requirement coverage against extracted records."),
    ("calculate_invoice", "calculator/invoice", "Calculate invoice totals using Decimal arithmetic."),
    ("translation_qa", "translation-qa", "Validate a translation against source text and declared artifacts."),
    ("anonymization_audit", "anonymization-audit", "Audit text for required anonymization patterns."),
    ("compose_offer", "document-compose/offer", "Compose an offer only from supplied structured fields."),
    ("compose_invoice", "document-compose/invoice", "Compose an invoice only from supplied structured fields."),
    ("write_quality_reports", "document-compose/reports", "Write CSV and JSON quality reports inside the caller's work root."),
)


def _mcp_result(request_id: object, result: dict, *, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result}, headers=headers)


async def quality_mcp(request: Request) -> Response:
    """Small authenticated Streamable HTTP MCP facade over the typed quality routes.

    The router authenticates every request with the existing agent key; discovery never
    exposes service DNS, filesystem roots, or credentials to either agent runtime.
    """
    _email, user = _by_agent_key(_bearer(request))
    if not user:
        return _deny(401, "неверный ключ агента")
    try:
        message = await request.json()
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "invalid JSON"}}, status_code=400)
    if not isinstance(message, dict):
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}, status_code=400)
    request_id = message.get("id")
    method = message.get("method")
    if method == "initialize":
        return _mcp_result(request_id, {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "quality-tools", "version": "1.0.0"},
        }, headers={"Mcp-Session-Id": uuid.uuid4().hex})
    if method == "notifications/initialized":
        return Response(status_code=202)
    if method == "tools/list":
        return _mcp_result(request_id, {"tools": [
            {"name": name, "description": description, "inputSchema": {"type": "object", "additionalProperties": True}}
            for name, _operation, description in MCP_TOOL_DEFINITIONS
        ]})
    if method == "tools/call":
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return JSONResponse({"jsonrpc": "2.0", "id": request_id,
                                 "error": {"code": -32602, "message": "tool name is required"}}, status_code=400)
        operations = {name: operation for name, operation, _description in MCP_TOOL_DEFINITIONS}
        operation = operations.get(params["name"])
        arguments = params.get("arguments", {})
        if operation is None or not isinstance(arguments, dict):
            return _mcp_result(request_id, {"content": [{"type": "text", "text": "unknown tool or invalid arguments"}],
                                             "isError": True})
        try:
            scoped = _scoped_quality_body(operation, arguments, user)
            response = await client.post(f"{QUALITY_BASE}/v1/{operation}", json=scoped)
        except httpx.ConnectError:
            return _mcp_result(request_id, {"content": [{"type": "text", "text": "quality-tools unavailable"}], "isError": True})
        except httpx.TimeoutException:
            return _mcp_result(request_id, {"content": [{"type": "text", "text": "quality-tools timeout"}], "isError": True})
        text = response.text
        return _mcp_result(request_id, {"content": [{"type": "text", "text": text}],
                                         "isError": response.status_code >= 400})
    return JSONResponse({"jsonrpc": "2.0", "id": request_id,
                         "error": {"code": -32601, "message": "method not found"}}, status_code=400)


async def llm_chat(request: Request) -> Response:
    email, user = _by_agent_key(_bearer(request))
    if not user:
        return _deny(401, "неверный ключ агента")
    body = await request.json()
    body["user"] = email                                  # учёт в журнале шлюза — по сотруднику
    headers = {"Authorization": f"Bearer {LLM_KEY}"}
    return await _proxy(f"{LLM_BASE}/chat/completions", headers, body)


async def llm_models(request: Request) -> Response:
    if not _by_agent_key(_bearer(request))[1]:
        return _deny(401, "неверный ключ агента")
    r = await client.get(f"{LLM_BASE}/models", headers={"Authorization": f"Bearer {LLM_KEY}"})
    return Response(r.content, status_code=r.status_code, media_type="application/json")


def _session_user_id(request: Request):
    token = request.cookies.get("refreshToken")
    if not token or not SECRET:
        return None
    try:
        return str(jwt.decode(token, SECRET, algorithms=["HS256"])["id"])
    except (jwt.PyJWTError, KeyError):
        return None


async def agent_file(request: Request) -> Response:
    engine, name = request.path_params["engine"], request.path_params["name"]
    uid = _session_user_id(request)
    if not uid:
        return Response("Войдите в чат и откройте ссылку снова", status_code=401)
    user = next((u for u in users().values() if u.get("id") == uid), None)
    if engine not in ENGINES or not user or not NAME_RE.match(name) or name in (".", ".."):
        return Response("Файл не найден", status_code=404)
    outbox = (USERS_DIR / user["slug"] / f"files-{engine}").resolve()      # в агенте это папка outbox
    path = (outbox / name).resolve()
    if outbox not in path.parents or not path.is_file():
        return Response("Файл не найден", status_code=404)
    # у обоих агентов файл часто называется одинаково («Договор.en.pdf») — при скачивании имя с движком,
    # чтобы браузер не путал их в загрузках; no-store — повторный перевод с тем же именем не берётся из кэша
    return FileResponse(path, filename=f"{path.stem} ({ENGINES[engine]['title']}){path.suffix}",
                        headers={"Cache-Control": "no-store"})


RATE_PAGE = """<!doctype html><html lang=ru><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Оценка ответа</title><style>
:root{{--bg:#f6f7f9;--card:#fff;--text:#1d2433;--muted:#5d6678;--line:#dde1e8;--accent:#2f6fde}}
@media (prefers-color-scheme:dark){{:root{{--bg:#14171c;--card:#1d2128;--text:#e6e9ef;--muted:#9aa3b2;--line:#2e333c;--accent:#6b9bff}}}}
body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,"Segoe UI",sans-serif}}
main{{max-width:560px;margin:40px auto;padding:0 16px}} .card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px}}
textarea{{width:100%;box-sizing:border-box;min-height:90px;font:inherit;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--text)}}
button{{margin-top:10px;font:inherit;padding:8px 14px;border-radius:6px;border:0;background:var(--accent);color:#fff;cursor:pointer}} small{{color:var(--muted)}}
</style><main><div class=card><h2>{title}</h2><p>{text}</p>{form}</div><p><small>Окно можно закрыть.</small></p></main></html>"""


def _run(run_id: str):
    if run_id in runs:
        return runs[run_id]
    try:
        for line in (STATS / "runs.jsonl").read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec.get("run") == run_id:
                return rec
    except (OSError, ValueError):
        pass
    return None


async def agent_rate(request: Request) -> Response:
    run_id, score = request.path_params["run"], request.path_params["score"]
    uid = _session_user_id(request)
    rec = _run(run_id) if re.fullmatch(r"[0-9a-f]{16}", run_id) and score in ("up", "down") else None
    if not uid:
        return HTMLResponse(RATE_PAGE.format(title="Нужен вход", text="Войдите в чат и нажмите ссылку оценки снова.",
                                             form=""), status_code=401)
    if not rec or rec.get("uid") != uid:
        return HTMLResponse(RATE_PAGE.format(title="Не найдено", text="Этот ответ не найден или он не ваш.", form=""),
                            status_code=404)
    title = ENGINES[rec["engine"]]["title"]
    comment = ""
    if request.method == "POST":
        comment = str((await request.form()).get("comment", ""))[:2000].strip()
    _record("ratings", {"run": run_id, "ts": int(time.time()), "engine": rec["engine"], "slug": rec["slug"],
                        "dept": rec.get("dept", ""), "score": score, "seconds": rec.get("seconds"), "comment": comment})
    verdict = "👍 хорошо" if score == "up" else "👎 плохо"
    if comment:
        return HTMLResponse(RATE_PAGE.format(title="Спасибо", text=f"Оценка «{verdict}» и замечание для {title} сохранены.",
                                             form=""))
    form = ("<form method=post><label for=c>Что не так или что понравилось (необязательно):</label>"
            f"<textarea id=c name=comment placeholder='сломалась вёрстка, выдумал данные, долго, не понял задачу…'></textarea>"
            "<button>Сохранить замечание</button></form>")
    return HTMLResponse(RATE_PAGE.format(title="Оценка сохранена",
                                         text=f"{escape(title)}: «{verdict}», ответ за {_duration(rec.get('seconds') or 0)}.",
                                         form=form))


async def health(request: Request) -> Response:
    return JSONResponse({"ok": True, "users": len(users())})


app = Starlette(routes=[
    Route("/health", health),
    Route("/llm/v1/chat/completions", llm_chat, methods=["POST"]),

    Route("/llm/v1/models", llm_models),
    Route("/document-translate/v1/jobs", document_translate_start, methods=["POST"]),
    Route("/document-translate/v1/jobs/{job_id}", document_translate_status),
    Route("/quality/mcp", quality_mcp, methods=["POST"]),
    *[Route(f"/quality/v1/{operation}", quality_endpoint(operation), methods=["POST"],
            name=f"quality-{operation.replace('/', '-')}") for operation in QUALITY_OPERATIONS],
    Route("/agent-files/rate/{run}/{score}", agent_rate, methods=["GET", "POST"]),
    Route("/agent-files/{engine}/{name:path}", agent_file),
    Route("/{engine}/v1/chat/completions", agent_chat, methods=["POST"]),
    Route("/{engine}/v1/models", agent_models),
])
