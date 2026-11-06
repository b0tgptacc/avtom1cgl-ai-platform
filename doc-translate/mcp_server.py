"""Инструмент агента LibreChat: перевести прикреплённый в чате документ с сохранением вёрстки и вернуть ссылки
на DOCX и PDF. MCP (streamable HTTP) на :8001, путь /mcp. LibreChat передаёт заголовки:
  Authorization: Bearer <DOC_MCP_KEY>      — без него инструмент не работает
  X-User-Id: {{LIBRECHAT_USER_ID}}         — чей файл искать и кому показывать результат
Сам перевод делает doc-translate (внутренний адрес /internal/jobs); ссылки на скачивание открываются только
этому же пользователю (проверка сессии LibreChat на странице)."""
import asyncio
import os
import time

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

WEB = os.getenv("WEB_BASE", "http://doc-translate:8000").rstrip("/")
INTERNAL_KEY = os.getenv("INTERNAL_KEY", "")
MCP_KEY = os.getenv("DOC_MCP_KEY", "")
WAIT = int(os.getenv("TOOL_WAIT_SECONDS", "150"))      # сколько ждать готовности, прежде чем отдать ссылку на прогресс

mcp = FastMCP(
    "doc-translate",
    instructions="Перевод документов (DOCX, PDF, DOC, RTF, ODT) с сохранением вёрстки. Вызывай translate_document, "
                 "когда пользователь просит перевести прикреплённый файл или получить перевод файлом (DOCX/PDF). "
                 "Не формируй HTML и не предлагай сохранять страницу в PDF — инструмент вернёт готовые файлы.",
    host="0.0.0.0", port=8001, stateless_http=True,          # ответы потоком (SSE) — как ждёт клиент LibreChat
    # сеть внутренняя, вход по ключу; проверка Host мешает обращению по имени контейнера
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _caller(ctx: Context) -> str:
    headers = ctx.request_context.request.headers
    if not MCP_KEY or headers.get("authorization") != f"Bearer {MCP_KEY}":
        raise PermissionError("Нет доступа к инструменту перевода")
    return headers.get("x-user-id", "")


@mcp.tool()
async def translate_document(filename: str, target_language: str, ctx: Context) -> str:
    """Перевести документ, который пользователь прикрепил в этот чат, с сохранением вёрстки
    (таблицы, шрифты, нумерация, колонтитулы, выделение слов) и вернуть ссылки на DOCX и PDF.

    filename: имя прикреплённого файла как его видит пользователь, например «Supply Agreement.docx».
    target_language: язык перевода — "ru" (русский), "en" (английский) или "zh" (китайский).
    Результат — готовый ответ пользователю со ссылками; передай его как есть."""
    user = _caller(ctx)
    lang = {"русский": "ru", "английский": "en", "китайский": "zh"}.get(target_language.lower(), target_language.lower())
    async with httpx.AsyncClient(base_url=WEB, headers={"X-Internal-Key": INTERNAL_KEY}, timeout=60) as web:
        r = await web.post("/internal/jobs", json={"user": user, "filename": filename, "target": lang})
        if r.status_code != 200:
            return "Не удалось начать перевод: " + r.json().get("error", f"ошибка {r.status_code}")
        job_id, name = r.json()["id"], r.json()["name"]

        deadline = time.time() + WAIT          # ожидание не блокирует других пользователей (async)
        job = {}
        while time.time() < deadline:
            job = (await web.get(f"/internal/jobs/{job_id}", params={"user": user})).json()
            if job.get("status") in ("done", "error"):
                break
            await asyncio.sleep(3)

    if job.get("status") == "done":
        notes = "".join(f"\n- {n}" for n in job.get("notes", []))
        return (f"Перевод документа «{name}» готов:\n\n"
                f"- [Скачать DOCX](/translate/api/jobs/{job_id}/docx)\n"
                f"- [Скачать PDF](/translate/api/jobs/{job_id}/pdf)\n"
                + (f"\nПримечания:{notes}" if notes else ""))
    if job.get("status") == "error":
        return f"Перевод «{name}» не удался: {job.get('error', 'ошибка')}"
    return (f"Документ «{name}» большой, перевод ещё идёт ({job.get('done', 0)} из {job.get('total', '…')} абзацев). "
            f"Прогресс и ссылки на скачивание: [Перевод документов](/translate/?job={job_id})")


app = mcp.streamable_http_app()
