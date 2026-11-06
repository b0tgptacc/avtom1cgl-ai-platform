"""Поддельная удалённая модель: пишет тело каждого запроса в bodies.jsonl, отвечает текстами пользователя (с метками)."""
import json, time
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

@app.post("/v1/chat/completions")
async def chat(req: Request):
    raw = await req.body()
    with open("bodies.jsonl", "ab") as f:
        f.write(raw + b"\n")
    body = json.loads(raw)
    users = [m["content"] if isinstance(m["content"], str) else " ".join(c.get("text", "") for c in m["content"])
             for m in body["messages"] if m["role"] == "user"]
    text = "ОТВЕТ: " + " | ".join(users)
    reasoning = "Думаю про " + users[-1]
    if "ВЫЗОВ" in users[-1]:                  # агент: модель вызывает инструмент с данными пользователя в аргументах
        args = json.dumps({"text": users[-1]}, ensure_ascii=False)
        call = {"id": "call_1", "type": "function", "function": {"name": "write_file", "arguments": args}}
        if body.get("stream"):
            def gen_tool():
                def ch(delta, fin=None):
                    return "data: " + json.dumps({"id": "x", "object": "chat.completion.chunk", "created": int(time.time()),
                        "model": "fake", "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}, ensure_ascii=False) + "\n\n"
                yield ch({"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                                "function": {"name": "write_file", "arguments": ""}}]})
                for i in range(0, len(args), 3):
                    yield ch({"tool_calls": [{"index": 0, "function": {"arguments": args[i:i+3]}}]})
                yield ch({}, "tool_calls")
                yield "data: [DONE]\n\n"
            return StreamingResponse(gen_tool(), media_type="text/event-stream")
        return JSONResponse({"id": "x", "object": "chat.completion", "created": int(time.time()), "model": "fake",
            "choices": [{"index": 0, "finish_reason": "tool_calls",
                         "message": {"role": "assistant", "content": None, "tool_calls": [call]}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
    if body.get("stream"):
        def gen():
            def ch(delta, fin=None):
                return "data: " + json.dumps({"id": "x", "object": "chat.completion.chunk", "created": int(time.time()),
                    "model": "fake", "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}, ensure_ascii=False) + "\n\n"
            yield ch({"role": "assistant"})
            for i in range(0, len(reasoning), 3):
                yield ch({"reasoning_content": reasoning[i:i+3]})
            for i in range(0, len(text), 3):
                yield ch({"content": text[i:i+3]})
            yield ch({}, "stop")
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")
    return JSONResponse({"id": "x", "object": "chat.completion", "created": int(time.time()), "model": "fake",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text, "reasoning_content": reasoning}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
