"""Локальный распознаватель для LibreChat — тот же API, что у Mistral OCR (/v1/files, /v1/files/{id}/url, /v1/ocr),
поэтому подключается стратегией mistral_ocr с baseURL http://ocr-ru:8000/v1. Файлы наружу не уходят.
Внешние URL не скачиваются: документ — только загруженный сюда файл или data: URL."""
import base64
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, request

from ocr import recognize

API_KEY = os.getenv("OCR_API_KEY", "")
STORE = Path(os.getenv("OCR_STORE", "/tmp/ocr-files"))
FILE_TTL = 3600                                   # секунд: загруженный файл живёт час, потом удаляется
STORE.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("ocr-ru")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("OCR_MAX_MB", "50")) * 1024 * 1024

ID_RE = re.compile(r"^[0-9a-f]{32}$")


@app.before_request
def check_key():
    if request.path == "/health" or not API_KEY:
        return
    if request.headers.get("Authorization", "") != f"Bearer {API_KEY}":
        abort(401)


def _path(file_id: str) -> Path:
    if not ID_RE.match(file_id):
        abort(404)
    return STORE / file_id


def _cleanup():
    now = time.time()
    for p in STORE.iterdir():
        if now - p.stat().st_mtime > FILE_TTL:
            p.unlink(missing_ok=True)


@app.get("/health")
def health():
    return "ocr-ru is up"


@app.post("/v1/files")
def upload():
    _cleanup()
    f = request.files.get("file")
    if f is None:
        abort(400)
    file_id = uuid.uuid4().hex
    data = f.read()
    _path(file_id).write_bytes(data)
    (STORE / f"{file_id}.json").write_text(json.dumps({"filename": f.filename}))
    return jsonify(id=file_id, object="file", bytes=len(data), created_at=int(time.time()),
                   filename=f.filename, purpose="ocr")


@app.get("/v1/files/<file_id>/url")
def signed_url(file_id):
    if not _path(file_id).exists():
        abort(404)
    return jsonify(url=f"local-file://{file_id}")


@app.delete("/v1/files/<file_id>")
def delete(file_id):
    _path(file_id).unlink(missing_ok=True)
    (STORE / f"{file_id}.json").unlink(missing_ok=True)
    return jsonify(id=file_id, object="file", deleted=True)


def _load(url: str) -> bytes:
    if url.startswith("local-file://"):
        p = _path(url[len("local-file://"):])
        if not p.exists():
            abort(404)
        return p.read_bytes()
    if url.startswith("data:") and ";base64," in url:
        return base64.b64decode(url.split(";base64,", 1)[1])
    abort(400, "Принимаются только загруженные файлы и data: URL")


@app.post("/v1/ocr")
def ocr():
    body = request.get_json(force=True) or {}
    doc = body.get("document") or {}
    url = doc.get("document_url") or doc.get("image_url") or ""
    data = _load(url)
    started = time.time()
    try:
        pages = recognize(data)
    except Exception as e:      # битый/неподдерживаемый файл — LibreChat перейдёт на свой разбор текстового слоя
        log.warning("ocr failed: %s", type(e).__name__)
        abort(422, "Не удалось распознать файл")
    # сам текст в журнал не пишем — только объём
    log.info("ocr: %d pages, %d chars, %.1f s", len(pages), sum(map(len, pages)), time.time() - started)
    return jsonify(
        pages=[{"index": i, "markdown": text, "images": [], "dimensions": None} for i, text in enumerate(pages)],
        model=body.get("model") or "ocr-ru",
        usage_info={"pages_processed": len(pages), "doc_size_bytes": len(data)},
    )
