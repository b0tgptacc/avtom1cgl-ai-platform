"""Authenticated internal fan-out proxy for one approved upstream LiteLLM endpoint.

LiteLLM performs PII masking before this service. This proxy only picks an
upstream credential, forwards the already-masked request unchanged, and keeps
credential values out of logs and responses.
"""
from __future__ import annotations

import os
import secrets
from typing import AsyncIterator

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from pool import KeyPool, KeySlot, parse_keys

UPSTREAM_BASE = os.getenv("UPSTREAM_BASE", "").rstrip("/")
AUTH_TOKEN = os.getenv("KEYPOOL_AUTH_TOKEN", "")
PRIMARY_KEY = os.getenv("UPSTREAM_API_KEY", "")
POOLED_KEYS = os.getenv("UPSTREAM_API_KEYS", "")
TIMEOUT = httpx.Timeout(float(os.getenv("UPSTREAM_TIMEOUT", "600")), connect=10)
MAX_ATTEMPTS = max(1, int(os.getenv("KEYPOOL_MAX_ATTEMPTS", "2")))
COOLDOWN_SECONDS = max(1.0, float(os.getenv("KEYPOOL_COOLDOWN_SECONDS", "30")))
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

if not UPSTREAM_BASE or not AUTH_TOKEN:
    raise RuntimeError("UPSTREAM_BASE and KEYPOOL_AUTH_TOKEN are required")

pool = KeyPool(parse_keys(PRIMARY_KEY, POOLED_KEYS), cooldown_seconds=COOLDOWN_SECONDS)
client = httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False)


def _authorized(request: Request) -> bool:
    value = request.headers.get("authorization", "")
    return value.lower().startswith("bearer ") and secrets.compare_digest(value[7:], AUTH_TOKEN)


def _upstream_url(path: str) -> str:
    base = UPSTREAM_BASE[:-3] if UPSTREAM_BASE.endswith("/v1") else UPSTREAM_BASE
    return f"{base}/v1/{path}"


def _headers(request: Request, slot: KeySlot) -> dict[str, str]:
    allowed = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"authorization", "host", "content-length", "connection"}
    }
    allowed["Authorization"] = f"Bearer {slot.key}"
    return allowed


async def _send(request: Request, path: str) -> tuple[httpx.Response, KeySlot] | None:
    content = await request.body()
    for _ in range(min(MAX_ATTEMPTS, len((await pool.metrics())))):
        slot = await pool.acquire()
        upstream_request = client.build_request(
            request.method,
            _upstream_url(path),
            headers=_headers(request, slot),
            content=content,
            params=request.query_params,
        )
        try:
            response = await client.send(upstream_request, stream=True)
        except httpx.HTTPError:
            await pool.release(slot, 503)
            continue
        if response.status_code in RETRYABLE_STATUS:
            await response.aclose()
            await pool.release(slot, response.status_code)
            continue
        return response, slot
    return None


async def proxy(request: Request) -> Response:
    if not _authorized(request):
        return JSONResponse({"error": {"message": "unauthorized keypool request"}}, status_code=401)
    path = request.path_params["path"]
    sent = await _send(request, path)
    if sent is None:
        return JSONResponse({"error": {"message": "all upstream credentials are temporarily unavailable"}}, status_code=503)
    response, slot = sent
    content_type = response.headers.get("content-type", "application/json")
    if "text/event-stream" not in content_type:
        try:
            body = await response.aread()
            return Response(body, status_code=response.status_code, media_type=content_type)
        finally:
            await response.aclose()
            await pool.release(slot, response.status_code)

    async def stream() -> AsyncIterator[bytes]:
        status = response.status_code
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        except httpx.HTTPError:
            status = 503
            raise
        finally:
            await response.aclose()
            await pool.release(slot, status)

    return StreamingResponse(stream(), status_code=response.status_code, media_type="text/event-stream")


async def health(_: Request) -> Response:
    return JSONResponse({"ok": True, "keys": await pool.metrics()})


app = Starlette(routes=[
    Route("/health", health),
    Route("/v1/{path:path}", proxy, methods=["GET", "POST"]),
])
