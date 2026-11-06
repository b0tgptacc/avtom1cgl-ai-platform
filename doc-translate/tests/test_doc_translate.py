"""Проверка перевода документов. Запуск: pytest -q (из каталога doc-translate).
Модель подменена поддельным сервером (переводит в ВЕРХНИЙ РЕГИСТР) — проверяется вёрстка, а не качество перевода.
Тесты с LibreOffice пропускаются, если soffice не установлен (в образе он есть)."""
import io
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import jwt
import pytest
from docx import Document
from werkzeug.serving import make_server

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SECRET = "test-refresh-secret-0123456789abcdef0123"
os.environ["JWT_REFRESH_SECRET"] = SECRET
os.environ.setdefault("JOBS_DIR", "/tmp/doc-translate-test-jobs")
UPLOADS = Path(os.environ.setdefault("LIBRECHAT_UPLOADS", "/tmp/doc-translate-test-uploads"))
USER_ID = "0123456789abcdef01234567"
os.environ["INTERNAL_KEY"] = "internal-test-key"
os.environ["DOC_MCP_KEY"] = "mcp-test-key"

import llm  # noqa: E402
from docx_translate import translate_docx  # noqa: E402

needs_soffice = pytest.mark.skipif(shutil.which("soffice") is None, reason="нет LibreOffice")


# --- образец договора ------------------------------------------------------------------------------

def sample_docx(path: Path) -> None:
    doc = Document()
    doc.sections[0].header.paragraphs[0].text = "Confidential"
    doc.add_heading("Supply Agreement No. 15", level=1)
    p = doc.add_paragraph("1.1. The ")
    p.add_run("Supplier").bold = True
    p.add_run(" shall deliver the Goods to ")
    p.add_run("<PERSON_1>").italic = True
    p.add_run(".")
    doc.add_paragraph("Delivery within thirty (30) days.", style="List Number")
    t = doc.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "Price", "Currency"
    t.cell(1, 0).text, t.cell(1, 1).text = "1,250,000.00", "USD"
    doc.save(path)


def upper_translator(segments, progress):
    for s in segments:
        s.result = s.source.upper()
    progress(len(segments), len(segments))


def runs(doc, contains):
    for p in doc.paragraphs:
        if contains in p.text:
            return [(r.text, bool(r.bold), bool(r.italic)) for r in p.runs]
    raise AssertionError(contains)


# --- вёрстка ---------------------------------------------------------------------------------------

def test_layout_preserved(tmp_path):
    sample_docx(tmp_path / "in.docx")
    translate_docx(str(tmp_path / "in.docx"), str(tmp_path / "out.docx"), upper_translator)
    src, out = Document(tmp_path / "in.docx"), Document(tmp_path / "out.docx")
    assert [p.style.name for p in src.paragraphs] == [p.style.name for p in out.paragraphs]
    assert out.paragraphs[0].text == "SUPPLY AGREEMENT NO. 15"
    # жирное слово осталось жирным, курсивная метка ПДн — курсивной и без изменений
    assert ("SUPPLIER", True, False) in runs(out, "SUPPLIER")
    assert ("<PERSON_1>", False, True) in runs(out, "SUPPLIER")
    assert out.sections[0].header.paragraphs[0].text == "CONFIDENTIAL"
    cells = out.tables[0]
    assert cells.cell(0, 0).text == "PRICE" and cells.cell(1, 0).text == "1,250,000.00"


def test_markers_lost_keeps_document(tmp_path):
    sample_docx(tmp_path / "in.docx")

    def careless(segments, progress):            # модель выбросила маркеры
        for s in segments:
            s.result = "ПЕРЕВОД: " + "".join(ch for ch in s.source if ch not in "⟦⟧/0123456789")
    segs = translate_docx(str(tmp_path / "in.docx"), str(tmp_path / "out.docx"), careless)
    out = Document(tmp_path / "out.docx")
    para = next(p for p in out.paragraphs if "ПЕРЕВОД" in p.text and "Supplier" in p.text)
    assert para.text.startswith("ПЕРЕВОД:")
    assert any("маркеры" in n for s in segs for n in s.notes)


def test_text_outside_markers(tmp_path):
    sample_docx(tmp_path / "in.docx")

    def outside(segments, progress):              # модель вынесла пробелы и точку за маркеры
        for s in segments:
            s.result = s.source.replace("⟦2⟧Supplier⟦/2⟧", " ⟦2⟧ПОСТАВЩИК⟦/2⟧ ").upper()
    translate_docx(str(tmp_path / "in.docx"), str(tmp_path / "out.docx"), outside)
    out = Document(tmp_path / "out.docx")
    assert "ПОСТАВЩИК" in "".join(r[0] for r in runs(out, "ПОСТАВЩИК") if r[1])


# --- запросы к модели через поддельный сервер ------------------------------------------------------

@pytest.fixture()
def fake_llm(monkeypatch):
    from flask import Flask, request
    srv_app, calls = Flask("fake"), {"n": 0, "drop_first": True}

    @srv_app.post("/v1/chat/completions")
    def chat():
        calls["n"] += 1
        body = request.get_json()
        segs = json.loads(body["messages"][-1]["content"])["segments"]
        if calls["drop_first"] and len(segs) > 1:          # первый пакет: «забыть» один сегмент
            calls["drop_first"] = False
            segs = segs[1:]
        out = {"segments": [{"id": s["id"], "text": s["text"].upper()} for s in segs]}
        return {"choices": [{"message": {"content": "```json\n" + json.dumps(out, ensure_ascii=False) + "\n```"}}]}

    server = make_server("127.0.0.1", 0, srv_app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(llm, "BASE", f"http://127.0.0.1:{server.server_port}/v1")
    yield calls
    server.shutdown()


def test_llm_translator(tmp_path, fake_llm):
    sample_docx(tmp_path / "in.docx")
    seen = []
    segs = translate_docx(str(tmp_path / "in.docx"), str(tmp_path / "out.docx"), llm.make_translator("ru"),
                          lambda d, t: seen.append((d, t)))
    assert all(s.result is not None for s in segs)       # пропущенный сегмент переведён повторным запросом
    assert seen[-1][0] == seen[-1][1] == len(segs)
    assert ("SUPPLIER", True, False) in runs(Document(tmp_path / "out.docx"), "SUPPLIER")


# --- служба: вход по сессии LibreChat и полный путь ------------------------------------------------

def token(**extra):
    return jwt.encode({"id": "u1", "exp": int(time.time()) + 600, **extra}, SECRET, algorithm="HS256")


def test_auth():
    from app import app
    c = app.test_client()
    assert c.get("/translate/api/me").get_json()["authenticated"] is False
    c.set_cookie("refreshToken", jwt.encode({"id": "u1"}, "wrong-secret-0123456789abcdef0123456", algorithm="HS256"))
    assert c.get("/translate/api/me").get_json()["authenticated"] is False
    c.set_cookie("refreshToken", jwt.encode({"id": "u1", "exp": 1}, SECRET, algorithm="HS256"))
    assert c.get("/translate/api/me").get_json()["authenticated"] is False      # срок истёк
    c.set_cookie("refreshToken", token())
    assert c.get("/translate/api/me").get_json()["authenticated"] is True
    assert app.test_client().post("/translate/api/jobs").status_code == 401


def _wait(c, job_id):
    for _ in range(120):
        j = c.get(f"/translate/api/jobs/{job_id}").get_json()
        if j["status"] in ("done", "error"):
            return j
        time.sleep(0.5)
    raise AssertionError("задание не завершилось")


@needs_soffice
def test_job_docx_and_pdf(tmp_path, fake_llm):
    from app import app
    sample_docx(tmp_path / "Договор.docx")
    c = app.test_client()
    c.set_cookie("refreshToken", token())
    r = c.post("/translate/api/jobs", data={"target": "ru",
                                             "file": (io.BytesIO((tmp_path / "Договор.docx").read_bytes()), "Договор.docx")})
    job_id = r.get_json()["id"]
    j = _wait(c, job_id)
    assert j["status"] == "done", j
    docx = c.get(f"/translate/api/jobs/{job_id}/docx")
    assert docx.status_code == 200 and "SUPPLY AGREEMENT" in Document(io.BytesIO(docx.data)).paragraphs[0].text
    pdf = c.get(f"/translate/api/jobs/{job_id}/pdf")
    assert pdf.status_code == 200 and pdf.data[:5] == b"%PDF-"
    other = app.test_client()
    other.set_cookie("refreshToken", jwt.encode({"id": "u2", "exp": int(time.time()) + 600}, SECRET, algorithm="HS256"))
    assert other.get(f"/translate/api/jobs/{job_id}/docx").status_code == 404     # чужое задание не видно


@needs_soffice
def test_job_from_pdf(tmp_path, fake_llm):
    from app import app
    from convert import soffice
    sample_docx(tmp_path / "contract.docx")
    pdf = soffice(tmp_path / "contract.docx", "pdf", tmp_path)
    c = app.test_client()
    c.set_cookie("refreshToken", token())
    job_id = c.post("/translate/api/jobs", data={"target": "ru", "file": (io.BytesIO(pdf.read_bytes()), "contract.pdf")}).get_json()["id"]
    j = _wait(c, job_id)
    assert j["status"] == "done", j
    assert any("PDF переведён на месте" in n for n in j["notes"])
    import pymupdf
    out = pymupdf.open(stream=c.get(f"/translate/api/jobs/{job_id}/pdf").data, filetype="pdf")
    src = pymupdf.open(pdf)
    assert "SUPPLY AGREEMENT" in out[0].get_text()
    assert [d["rect"] for d in out[0].get_drawings()] == [d["rect"] for d in src[0].get_drawings()]   # линии таблицы
    text = "\n".join(p.text for p in Document(io.BytesIO(c.get(f"/translate/api/jobs/{job_id}/docx").data)).paragraphs)
    assert "SUPPLY AGREEMENT" in text


def sample_pdf(path: Path) -> None:
    """Две колонки, таблица с линиями, подпись внизу страницы."""
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Supply Agreement", fontname="hebo", fontsize=14)
    left = ["1.1. The Supplier shall deliver the", "Goods to the Buyer within thirty days."]
    right = ["2.1. The Buyer shall pay the price", "within ten days."]
    for i, (a, b) in enumerate(zip(left, right)):
        page.insert_text((72, 110 + 14 * i), a, fontname="helv", fontsize=11)
        page.insert_text((320, 110 + 14 * i), b, fontname="helv", fontsize=11)
    for x in (72, 220, 380):
        page.draw_line((x, 160), (x, 200))
    for y in (160, 180, 200):
        page.draw_line((72, y), (380, y))
    page.insert_text((76, 174), "Item", fontname="helv", fontsize=10)
    page.insert_text((224, 174), "Unit price", fontname="helv", fontsize=10)
    page.insert_text((76, 194), "Steel pipes", fontname="helv", fontsize=10)
    page.insert_text((224, 194), "1,250.00", fontname="helv", fontsize=10)
    page.insert_text((72, 800), "Confidential", fontname="helv", fontsize=9)
    doc.save(path)


def test_pdf_in_place(tmp_path):
    import pymupdf
    from pdf_translate import translate_pdf
    sample_pdf(tmp_path / "in.pdf")
    segs = translate_pdf(str(tmp_path / "in.pdf"), str(tmp_path / "out.pdf"), upper_translator, "ru")
    sources = [s.source for s in segs]
    assert "1.1. The Supplier shall deliver the Goods to the Buyer within thirty days." in sources     # абзац из двух строк
    assert "2.1. The Buyer shall pay the price within ten days." in sources               # вторая колонка — отдельно
    assert "1,250.00" not in " ".join(sources)                                           # без букв — не трогаем
    src, out = pymupdf.open(tmp_path / "in.pdf")[0], pymupdf.open(tmp_path / "out.pdf")[0]
    assert [d["rect"] for d in out.get_drawings()] == [d["rect"] for d in src.get_drawings()]
    words = {w[4]: w for w in out.get_text("words")}
    assert words["STEEL"][0] > 72 and words["STEEL"][3] < 200          # остался в своей ячейке
    assert words["TEN"][0] >= 320                                       # во второй колонке
    assert words["CONFIDENTIAL"][1] > 780                               # подпись страницы внизу
    assert "1,250.00" in words


def sample_ocr_scan(path: Path) -> None:
    """Скан, уже распознанный ABBYY/Acrobat: страница — картинка, поверх неё невидимый текст тех же слов.
    Слой OCR режет строку на куски (как ABBYY), в оглавлении — отточие и номер страницы."""
    import pymupdf
    src = pymupdf.open()
    page = src.new_page(width=420, height=595)
    lines = [((60, 80), "1.0.1 The Supplier shall deliver the Goods", 11),
             ((60, 96), "to the Buyer within thirty days.", 11),
             ((60, 140), "2.1 General provisions ....................................", 11)]
    for pos, text, size in lines:
        page.insert_text(pos, text, fontname="tiro", fontsize=size)
    page.insert_text((340, 140), "12", fontname="tiro", fontsize=11)
    pix = page.get_pixmap(dpi=150)
    words = page.get_text("words")
    doc = pymupdf.open()
    scan = doc.new_page(width=420, height=595)
    scan.insert_image(scan.rect, pixmap=pix)
    for x0, y0, x1, y1, w, *_ in words:          # каждое слово — отдельной строкой слоя, как у ABBYY
        scan.insert_text((x0, y1 - 2.5), w, fontname="tiro", fontsize=11, render_mode=3)
    doc.save(path)


def test_ocr_layer_scan(tmp_path):
    import pymupdf
    from pdf_translate import ocr_layer, translate_pdf
    sample_ocr_scan(tmp_path / "scan.pdf")
    sample_pdf(tmp_path / "text.pdf")
    assert ocr_layer(str(tmp_path / "scan.pdf")) and not ocr_layer(str(tmp_path / "text.pdf"))
    segs = translate_pdf(str(tmp_path / "scan.pdf"), str(tmp_path / "out.pdf"), upper_translator, "ru", scan=True)
    sources = [s.source for s in segs]
    assert "1.0.1 The Supplier shall deliver the Goods to the Buyer within thirty days." in sources   # слова — в абзац
    assert "2.1 General provisions" in sources                                    # без отточия и номера страницы
    out = pymupdf.open(tmp_path / "out.pdf")[0]
    text = out.get_text()
    assert "SUPPLIER" in text and "Supplier" not in text      # невидимый слой оригинала убран
    assert "GENERAL PROVISIONS" in text and "....." in text  # отточие нарисовано заново после перевода
    pix = out.get_pixmap(dpi=72, clip=pymupdf.Rect(60, 72, 330, 100))
    import numpy as np
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    assert (img[..., 0] < 100).mean() < 0.25                 # картинка под строкой забелена: только перевод
    num = out.get_pixmap(dpi=150, clip=pymupdf.Rect(338, 128, 356, 144))
    assert min(num.samples) < 100                             # номер страницы на скане не тронут


def test_rejects_unsupported():
    from app import app
    c = app.test_client()
    c.set_cookie("refreshToken", token())
    r = c.post("/translate/api/jobs", data={"target": "ru", "file": (io.BytesIO(b"x"), "a.exe")})
    assert r.status_code == 400


# --- инструмент агента: файл из чата → перевод → ссылки ---------------------------------------------

def put_upload(name: str) -> Path:
    folder = UPLOADS / USER_ID
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"5f0c6a1e-1111-2222-3333-444455556666__{name}"
    sample_docx(path)
    return path


def test_find_upload():
    from app import find_upload
    put_upload("Old_Contract.docx")
    time.sleep(0.05)
    put_upload("Supply_Agreement_2026.docx")
    assert find_upload(USER_ID, "Supply Agreement 2026.docx").name.endswith("Supply_Agreement_2026.docx")
    assert find_upload(USER_ID, "Old Contract.docx").name.endswith("Old_Contract.docx")
    assert find_upload(USER_ID, "нет такого.docx").name.endswith("Supply_Agreement_2026.docx")   # самый свежий
    assert find_upload("ffffffffffffffffffffffff", "x.docx") is None


def test_internal_requires_key():
    from app import app
    c = app.test_client()
    assert c.post("/internal/jobs", json={"user": USER_ID, "filename": "x"}).status_code == 403
    assert c.post("/internal/jobs", headers={"X-Internal-Key": "internal-test-key"},
                  json={"user": "../etc", "filename": "x"}).status_code == 403


@needs_soffice
def test_mcp_tool(fake_llm, monkeypatch):
    import asyncio
    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    from app import app
    import mcp_server

    put_upload("Supply_Agreement_2026.docx")
    web = make_server("127.0.0.1", 0, app)
    threading.Thread(target=web.serve_forever, daemon=True).start()
    monkeypatch.setattr(mcp_server, "WEB", f"http://127.0.0.1:{web.server_port}")
    cfg = uvicorn.Config(mcp_server.app, host="127.0.0.1", port=18431, log_level="warning")
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(50):
        if srv.started:
            break
        time.sleep(0.1)

    async def call(key):
        headers = {"Authorization": f"Bearer {key}", "X-User-Id": USER_ID}
        async with streamablehttp_client("http://127.0.0.1:18431/mcp", headers=headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert [t.name for t in tools.tools] == ["translate_document"]
                res = await session.call_tool("translate_document",
                                              {"filename": "Supply Agreement 2026.docx", "target_language": "ru"})
                return res.content[0].text, res.isError

    text, is_error = asyncio.run(call("mcp-test-key"))
    assert not is_error and "Скачать DOCX" in text and "/translate/api/jobs/" in text, text
    text, is_error = asyncio.run(call("wrong"))
    assert is_error and "Нет доступа" in text
    srv.should_exit = True
    web.shutdown()
