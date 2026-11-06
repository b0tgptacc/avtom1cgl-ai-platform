"""Проверка распознавания и API. Запуск: pytest -q (из каталога ocr-ru). Нужен шрифт с кириллицей (DejaVu)."""
import base64
import io
import os
import sys

import pytest
import zxingcpp
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OCR_API_KEY", "test-key")

from app import app  # noqa: E402
from ocr import receipt_from_qr, recognize  # noqa: E402

FONT = os.getenv("TEST_FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
FONT_CJK = os.getenv("TEST_FONT_CJK", "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc")
AUTH = {"Authorization": "Bearer test-key"}
LINES = ["Договор поставки № 15/2026", "Покупатель: ООО «Ромашка»", "Сумма договора 1 250 000,00 руб."]
QR = "t=20261021T1530&s=1250.00&fn=7380440700000000&i=12345&fp=1234567890&n=1"


def scan(lines=LINES, qr=None, font_path=FONT) -> Image.Image:
    """Имитация скана: текст на белом листе, при необходимости — QR-код чека."""
    img = Image.new("L", (1700, 200 + 70 * len(lines) + (400 if qr else 0)), 255)
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(font_path, 40)
    for i, line in enumerate(lines):
        draw.text((100, 100 + 70 * i), line, font=font, fill=0)
    if qr:
        m = memoryview(zxingcpp.create_barcode(qr, zxingcpp.BarcodeFormat.QRCode).to_image(scale=6))
        img.paste(Image.frombytes("L", (m.shape[1], m.shape[0]), m.tobytes()), (100, 120 + 70 * len(lines)))
    return img


def as_bytes(img: Image.Image, fmt: str) -> bytes:
    buf = io.BytesIO()
    img.save(buf, fmt, resolution=300)
    return buf.getvalue()


def text_pdf(text: str) -> bytes:
    """PDF с текстовым слоем (латиница, стандартный шрифт Helvetica)."""
    stream = f"BT /F1 14 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for i, obj in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref))
    return out.getvalue()


def norm(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").split())


def test_scan_png():
    text = norm(recognize(as_bytes(scan(), "PNG"))[0])
    assert "договор поставки" in text and "ромашка" in text and "1 250 000,00" in text


def test_scanned_pdf():
    # PDF без текстового слоя (как со сканера) — страница распознаётся как картинка
    pages = recognize(as_bytes(scan(), "PDF"))
    assert len(pages) == 1 and "договор поставки" in norm(pages[0])


def test_text_layer_pdf():
    pages = recognize(text_pdf("Supply Agreement No 15 dated 15 October 2026 between Acme Ltd and Romashka LLC"))
    assert pages == ["Supply Agreement No 15 dated 15 October 2026 between Acme Ltd and Romashka LLC"]


def test_chinese_scan():
    # страница с иероглифами распознаётся китайской моделью (выбор по OSD), без пробелов между иероглифами
    lines = ["供货合同 第15号", "甲方：北京贸易有限公司", "合同金额：人民币壹佰贰拾伍万元整"]
    text = recognize(as_bytes(scan(lines, font_path=FONT_CJK), "PNG"))[0]
    # проверяем выбор китайской модели и долю распознанных иероглифов, а не точность до знака:
    # Tesseract на китайском ошибается в отдельных иероглифах, и каких именно — зависит от шрифта
    expected = [c for c in "".join(lines) if "\u4e00" <= c <= "\u9fff"]
    share = sum(c in text for c in expected) / len(expected)
    assert share >= 0.7, (share, text)        # русская модель на китайском даёт ~0 — порог отличает выбор модели
    assert "合同" in text and " 合同" not in text.replace("\n", "")


def test_receipt_qr():
    page = recognize(as_bytes(scan(["Кассовый чек", "ИТОГ 1250.00"], qr=QR), "PNG"))[0]
    assert "21.10.2026 15:30" in page and "Сумма: 1250.00 руб." in page and "ФН: 7380440700000000" in page
    assert receipt_from_qr("https://example.ru") == ""


def test_api_flow():
    client = app.test_client()
    assert client.post("/v1/ocr", json={}).status_code == 401
    up = client.post("/v1/files", headers=AUTH,
                     data={"purpose": "ocr", "file": (io.BytesIO(as_bytes(scan(), "PNG")), "скан.png")})
    file_id = up.get_json()["id"]
    url = client.get(f"/v1/files/{file_id}/url", headers=AUTH).get_json()["url"]
    res = client.post("/v1/ocr", headers=AUTH,
                      json={"model": "x", "document": {"type": "image_url", "image_url": url}}).get_json()
    assert "ромашка" in norm(res["pages"][0]["markdown"]) and res["pages"][0]["images"] == []
    assert client.delete(f"/v1/files/{file_id}", headers=AUTH).get_json()["deleted"] is True


def test_api_data_url_and_no_external_fetch():
    client = app.test_client()
    data_url = "data:image/png;base64," + base64.b64encode(as_bytes(scan(), "PNG")).decode()
    res = client.post("/v1/ocr", headers=AUTH, json={"document": {"type": "image_url", "image_url": data_url}})
    assert res.status_code == 200
    ext = client.post("/v1/ocr", headers=AUTH,
                      json={"document": {"type": "document_url", "document_url": "http://example.com/a.pdf"}})
    assert ext.status_code == 400


def test_broken_file():
    client = app.test_client()
    data_url = "data:application/pdf;base64," + base64.b64encode(b"%PDF-1.4 broken").decode()
    assert client.post("/v1/ocr", headers=AUTH,
                       json={"document": {"type": "document_url", "document_url": data_url}}).status_code == 422
