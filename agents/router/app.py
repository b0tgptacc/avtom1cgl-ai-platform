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
    body = await request.json()
    body["model"] = ENGINES[engine]["model"]
    for m in body.get("messages") or []:                # строку «⏱ время · оценить» агенту в истории не отдаём
        if m.get("role") == "assistant" and isinstance(m.get("content"), str):
            m["content"] = FOOTER_RE.sub("", m["content"])
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
    log.info("chat %s %s conv=%s stream=%s", engine, user["slug"], conv[:8], bool(body.get("stream")))
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
    Route("/agent-files/rate/{run}/{score}", agent_rate, methods=["GET", "POST"]),
    Route("/agent-files/{engine}/{name:path}", agent_file),
    Route("/{engine}/v1/chat/completions", agent_chat, methods=["POST"]),
    Route("/{engine}/v1/models", agent_models),
])
