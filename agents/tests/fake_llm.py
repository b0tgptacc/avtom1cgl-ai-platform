"""Поддельная модель для проверки стендов без Qwen (OpenAI-совместимая, с вызовом инструментов).
Пишет каждый запрос в /tmp/fake/requests.jsonl. Поведение по последнему сообщению пользователя:
  «ЗАПИШИ <имя> <текст>»  — вызывает инструмент записи файла (ищет его среди предложенных: write_file / write / …)
  «ВЫПОЛНИ <команда>»     — вызывает инструмент терминала (terminal / exec / bash / shell)
  иначе                   — отвечает «ОТВЕТ: <текст>»; после результата инструмента — «ГОТОВО: <результат>»."""
import json
import os
import re
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
LOG = "/tmp/fake/requests.jsonl"
os.makedirs("/tmp/fake", exist_ok=True)


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return " ".join(c.get("text", "") for c in content or [] if isinstance(c, dict))


def _tool(tools, names):
    for t in tools or []:
        n = (t.get("function") or {}).get("name", "")
        if n in names:
            return t["function"]
    for t in tools or []:
        n = (t.get("function") or {}).get("name", "")
        if any(x in n for x in names):
            return t["function"]
    return None


def _args_for(fn, kind, a, b=""):
    props = list(((fn.get("parameters") or {}).get("properties") or {}).keys())
    if kind == "write":
        path_key = next((p for p in props if p in ("path", "file_path", "filename", "file")), props[0] if props else "path")
        body_key = next((p for p in props if p in ("content", "contents", "text", "data")), "content")
        return {path_key: a, body_key: b}
    cmd_key = next((p for p in props if p in ("command", "cmd", "script")), props[0] if props else "command")
    return {cmd_key: a}


def decide(body):
    msgs = body.get("messages", [])
    last = msgs[-1] if msgs else {}
    try:                                           # запрос translate-document: пакет сегментов → «перевод» с пометкой
        segs = json.loads(_text(last.get("content")))["segments"]
        return {"content": json.dumps({"segments": [{"id": s["id"], "text": "ПЕРЕВОД " + s["text"]} for s in segs]},
                                      ensure_ascii=False)}, "stop"
    except (ValueError, KeyError, TypeError):
        pass
    if last.get("role") == "tool":
        return {"content": "ГОТОВО: " + _text(last.get("content"))[:2000]}, "stop"
    user = next((_text(m.get("content")) for m in reversed(msgs) if m.get("role") == "user"), "")
    m = re.search(r"ЗАПИШИ (\S+) (.+)", user, re.S)
    if m:
        fn = _tool(body.get("tools"), ("write_file", "write", "file_write", "create_file"))
        if fn:
            return {"tool": fn["name"], "args": _args_for(fn, "write", m.group(1), m.group(2).strip())}, "tool_calls"
    m = re.search(r"ВЫПОЛНИ (.+)", user, re.S)
    if m:
        fn = _tool(body.get("tools"), ("terminal", "exec", "bash", "shell", "run_command"))
        if fn:
            return {"tool": fn["name"], "args": _args_for(fn, "cmd", m.group(1).strip())}, "tool_calls"
    return {"content": "ОТВЕТ: " + user[-500:]}, "stop"


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": m, "object": "model"} for m in ("qwen", "qwen-fast")]}


@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"auth": req.headers.get("authorization", ""), "body": body}, ensure_ascii=False) + "\n")
    out, fin = decide(body)
    created = int(time.time())
    if "tool" in out:
        call = {"id": f"call_{created}", "type": "function",
                "function": {"name": out["tool"], "arguments": json.dumps(out["args"], ensure_ascii=False)}}
        message = {"role": "assistant", "content": None, "tool_calls": [call]}
    else:
        message = {"role": "assistant", "content": out["content"]}
    usage = {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}
    if not body.get("stream"):
        return JSONResponse({"id": "x", "object": "chat.completion", "created": created, "model": body.get("model"),
                             "choices": [{"index": 0, "message": message, "finish_reason": fin}], "usage": usage})

    def gen():
        def ch(delta, f=None, **extra):
            return "data: " + json.dumps({"id": "x", "object": "chat.completion.chunk", "created": created,
                                          "model": body.get("model"), "choices": [{"index": 0, "delta": delta,
                                                                                    "finish_reason": f}], **extra},
                                         ensure_ascii=False) + "\n\n"
        if "tool_calls" in message:
            c = message["tool_calls"][0]
            yield ch({"role": "assistant", "tool_calls": [{"index": 0, **c}]})
        else:
            yield ch({"role": "assistant", "content": ""})
            for i in range(0, len(message["content"]), 20):
                yield ch({"content": message["content"][i:i + 20]})
        yield ch({}, fin)
        yield ch({}, None, usage=usage)
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")
