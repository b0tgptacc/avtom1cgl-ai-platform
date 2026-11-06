"""Перевод документов с сохранением вёрстки: https://ai.<домен>/translate/ (nginx → doc-translate:8000).
Вход — та же сессия, что в LibreChat: cookie refreshToken, подпись JWT_REFRESH_SECRET. Свой пароль не нужен.
Файлы — только на ВМ, удаляются через сутки; скачать может только тот, кто загрузил. Текст — через шлюз маскирования.
/internal/* — для инструмента агента (mcp_server.py): не проксируется nginx, требует INTERNAL_KEY; файл берётся из загрузок
LibreChat этого пользователя (/uploads/<id пользователя>, только чтение)."""
import logging
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

import jwt
from flask import Flask, abort, jsonify, request, send_file

from convert import is_scan, pdf_to_docx, to_docx, to_pdf
from docx_translate import translate_docx
from llm import LANGS, make_translator
from pdf_translate import ocr_layer, translate_pdf

SECRET = os.getenv("JWT_REFRESH_SECRET", "")
JOBS_DIR = Path(os.getenv("JOBS_DIR", "/tmp/jobs"))
JOB_TTL = 24 * 3600
MAX_PARALLEL_JOBS = int(os.getenv("MAX_PARALLEL_JOBS", "2"))
ALLOWED = {".docx", ".pdf", ".doc", ".rtf", ".odt"}
INTERNAL_KEY = os.getenv("INTERNAL_KEY", "")
UPLOADS = Path(os.getenv("LIBRECHAT_UPLOADS", "/uploads"))
UPLOAD_MAX_AGE = 24 * 3600                   # файл из чата ищем среди загруженных за последние сутки
ID_RE = re.compile(r"^[0-9a-f]{24}$")         # id пользователя LibreChat (ObjectId MongoDB)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("doc-translate")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_MB", "50")) * 1024 * 1024
JOBS_DIR.mkdir(parents=True, exist_ok=True)
jobs: dict = {}
slots = threading.Semaphore(MAX_PARALLEL_JOBS)
PAGE = (Path(__file__).parent / "page.html").read_text(encoding="utf-8")


def current_user() -> str | None:
    token = request.cookies.get("refreshToken")
    if not token or not SECRET:
        return None
    try:
        return str(jwt.decode(token, SECRET, algorithms=["HS256"])["id"])
    except (jwt.PyJWTError, KeyError):
        return None


def require_user() -> str:
    user = current_user()
    if not user:
        abort(401)
    return user


def cleanup():
    now = time.time()
    for job_id, job in list(jobs.items()):
        if now - job["created"] > JOB_TTL:
            shutil.rmtree(JOBS_DIR / job_id, ignore_errors=True)
            jobs.pop(job_id, None)


def run_job(job_id: str, src: Path, target: str):
    job, workdir = jobs[job_id], JOBS_DIR / job_id
    with slots:
        job["status"] = "running"
        started = time.time()
        try:
            def progress(done, total):
                job.update(stage="перевод", done=done, total=total)

            job["stage"] = "подготовка документа"
            out_docx, out_pdf = workdir / f"{src.stem}.{target}.docx", workdir / f"{src.stem}.{target}.pdf"
            if src.suffix.lower() == ".pdf" and not is_scan(src):
                # PDF с текстом переводится на месте: линии, колонки и таблицы не пересобираются;
                # скан с готовым слоем OCR — тоже на месте, но под строками забеливается картинка
                layered = ocr_layer(str(src))
                segments = translate_pdf(str(src), str(out_pdf), make_translator(target), target, progress,
                                         scan=layered)
                if layered:
                    job["notes"].append("скан с распознанным текстом: перевод поверх картинки — "
                                        "сверьте цифры, даты и имена с оригиналом")
                job["stage"] = "сборка DOCX"
                pdf_to_docx(out_pdf, out_docx)
                job["notes"].append("PDF переведён на месте, вёрстка как в оригинале; "
                                    "DOCX собран из переведённого PDF для правки — в нём вёрстка может отличаться")
            else:
                docx_in, how = to_docx(src, workdir)
                if how:
                    job["notes"].append(how)
                segments = translate_docx(str(docx_in), str(out_docx), make_translator(target), progress)
                job["stage"] = "сборка PDF"
                out_pdf = to_pdf(out_docx, workdir)
            failed = sum(1 for s in segments if s.result is None)
            lost = sum(1 for s in segments if any("маркеры" in n for n in s.notes))
            if failed:
                job["notes"].append(f"не переведено абзацев: {failed} (оставлен оригинал)")
            if lost:
                job["notes"].append(f"абзацев без сохранённого выделения слов: {lost}")
            job.update(status="done", stage="готово", files={"docx": out_docx.name, "pdf": out_pdf.name})
            log.info("job %s: user %s, %d segments, %d failed, %.0f s",
                     job_id, job["user"], len(segments), failed, time.time() - started)
        except Exception as e:      # пользователю — понятное сообщение, в журнал — тип ошибки без содержимого
            job.update(status="error", stage="ошибка", error=str(e) if isinstance(e, ValueError)
                       else "Не удалось обработать документ. Попробуйте DOCX или обратитесь в ИТ.")
            log.warning("job %s failed: %s", job_id, type(e).__name__)
        finally:
            src.unlink(missing_ok=True)


@app.get("/translate/health")
def health():
    return "doc-translate is up"


@app.get("/translate/")
def page():
    return PAGE


@app.get("/translate/api/me")
def me():
    return jsonify(authenticated=current_user() is not None, languages=LANGS)


def start_job(user: str, name: str, target: str, save) -> str:
    """Создать задание: save(path) кладёт исходный файл в каталог задания."""
    cleanup()
    job_id = uuid.uuid4().hex
    workdir = JOBS_DIR / job_id
    workdir.mkdir()
    src = workdir / f"{(Path(name).stem[:80] or 'document')}{Path(name).suffix.lower()}"
    save(src)
    jobs[job_id] = {"user": user, "created": time.time(), "status": "queued", "stage": "в очереди",
                    "done": 0, "total": 0, "notes": [], "files": {}, "name": name, "target": target}
    threading.Thread(target=run_job, args=(job_id, src, target), daemon=True).start()
    return job_id


@app.post("/translate/api/jobs")
def create_job():
    user = require_user()
    f = request.files.get("file")
    target = request.form.get("target", "en")
    if f is None or not f.filename or target not in LANGS:
        abort(400)
    if Path(f.filename).suffix.lower() not in ALLOWED:
        return jsonify(error="Поддерживаются DOCX, PDF, DOC, RTF, ODT"), 400
    return jsonify(id=start_job(user, f.filename, target, f.save))


@app.get("/translate/api/jobs")
def list_jobs():
    user = require_user()
    mine = sorted(((k, j) for k, j in jobs.items() if j["user"] == user), key=lambda kv: -kv[1]["created"])
    return jsonify([{"id": k, "name": j["name"], "status": j["status"]} for k, j in mine[:20]])


# --- для инструмента агента (mcp_server.py) ---------------------------------------------------------

def _internal(user: str) -> None:
    if not INTERNAL_KEY or request.headers.get("X-Internal-Key") != INTERNAL_KEY or not ID_RE.match(user or ""):
        abort(403)


def _norm(name: str) -> str:
    """Имя без учёта регистра, пробелов и знаков — LibreChat «чистит» имя файла при сохранении."""
    return re.sub(r"[\W_]+", "", Path(name).stem.lower())


def find_upload(user: str, filename: str) -> Path | None:
    """Файл, который пользователь прикрепил в чате: uploads/<user>/<file_id>__<имя>. По имени — самый свежий
    совпавший; без совпадения по имени — самый свежий документ за сутки."""
    folder = UPLOADS / user
    if not folder.is_dir():
        return None
    now = time.time()
    docs = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in ALLOWED
            and now - p.stat().st_mtime < UPLOAD_MAX_AGE]
    docs.sort(key=lambda p: -p.stat().st_mtime)
    want = _norm(filename) if filename else ""
    named = [p for p in docs if want and want in _norm(p.name.split("__", 1)[-1])]
    return (named or docs or [None])[0]


@app.post("/internal/jobs")
def internal_create():
    body = request.get_json(force=True) or {}
    user, target = body.get("user", ""), body.get("target", "ru")
    _internal(user)
    if target not in LANGS:
        return jsonify(error=f"язык: {', '.join(LANGS)}"), 400
    src = find_upload(user, body.get("filename", ""))
    if src is None:
        return jsonify(error="Не нашёл прикреплённый документ (DOCX, PDF, DOC, RTF, ODT) за последние сутки"), 404
    name = src.name.split("__", 1)[-1]
    job_id = start_job(user, name, target, lambda dst: shutil.copyfile(src, dst))
    return jsonify(id=job_id, name=name)


@app.get("/internal/jobs/<job_id>")
def internal_status(job_id):
    user = request.args.get("user", "")
    _internal(user)
    job = _job(job_id, user)
    return jsonify({k: job[k] for k in ("status", "stage", "done", "total", "notes", "files", "name")}
                   | ({"error": job["error"]} if "error" in job else {}))


def _job(job_id: str, user: str) -> dict:
    job = jobs.get(job_id)
    if not job or job["user"] != user:
        abort(404)
    return job


@app.get("/translate/api/jobs/<job_id>")
def job_status(job_id):
    job = _job(job_id, require_user())
    return jsonify({k: job[k] for k in ("status", "stage", "done", "total", "notes", "files", "name", "target")}
                   | ({"error": job["error"]} if "error" in job else {}))


@app.get("/translate/api/jobs/<job_id>/<fmt>")
def download(job_id, fmt):
    job = _job(job_id, require_user())
    name = job["files"].get(fmt)
    if not name:
        abort(404)
    return send_file(JOBS_DIR / job_id / name, as_attachment=True, download_name=name)
