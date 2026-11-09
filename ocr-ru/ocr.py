"""Распознавание текста без внешних сервисов: PDF (текстовый слой страницы, а если его нет — картинка страницы),
изображения (сканы, фото, чеки), QR-коды кассовых чеков. Tesseract 5 встроен в пакет tesserocr.
Языки страницы выбираются по письменности (OSD): кириллица/латиница — rus+eng, иероглифы — chi_sim+eng.
Смешивать все языки в одном проходе нельзя — качество падает у всех."""
import io
import os
import re
from datetime import datetime
from typing import List
from urllib.parse import parse_qs

import pypdfium2 as pdfium
import tesserocr
import zxingcpp
from PIL import Image, ImageOps

TESSDATA = os.getenv("TESSDATA_PREFIX", "/usr/share/tessdata/")
LANGS = os.getenv("OCR_LANGS", "rus+eng")
LANGS_CJK = os.getenv("OCR_LANGS_CJK", "chi_sim+eng")     # пусто — китайский не распознаётся
# OSD путает китайский с японским и корейским — все иероглифические письменности ведём в китайскую модель
CJK_SCRIPTS = {"Han", "HanS", "HanT", "HanS_vert", "HanT_vert", "Japanese", "Korean", "Hangul"}
LOW_CONF = 60                # средняя уверенность Tesseract ниже — пробуем вторую модель и берём лучший результат
DPI = int(os.getenv("OCR_DPI", "300"))
MAX_PAGES = int(os.getenv("OCR_MAX_PAGES", "300"))
MIN_TEXT_LAYER = 30          # символов на странице PDF: меньше — считаем страницу сканом и распознаём картинку

_apis = {}                   # экземпляры Tesseract по набору языков, на процесс (gunicorn sync — запросы по очереди)

RECEIPT_TYPES = {"1": "приход", "2": "возврат прихода", "3": "расход", "4": "возврат расхода"}


def _tesseract(langs: str, psm=tesserocr.PSM.AUTO) -> tesserocr.PyTessBaseAPI:
    key = (langs, psm)
    if key not in _apis:
        _apis[key] = tesserocr.PyTessBaseAPI(path=TESSDATA, lang=langs, psm=psm)
    return _apis[key]


def page_langs(img: Image.Image) -> str:
    """Письменность страницы по OSD; не определилась (мало текста) — rus+eng."""
    if not LANGS_CJK:
        return LANGS
    try:
        osd = _tesseract("osd", tesserocr.PSM.OSD_ONLY)
        osd.SetImage(img)
        info = osd.DetectOrientationScript() or {}
    except RuntimeError:
        return LANGS
    return LANGS_CJK if info.get("script_name") in CJK_SCRIPTS else LANGS


def _run(img: Image.Image, langs: str):
    api = _tesseract(langs)
    api.SetImage(img)
    text = api.GetUTF8Text().strip()
    if langs == LANGS_CJK:                                # Tesseract ставит пробелы между иероглифами — убрать
        text = re.sub(r"(?<=[\u3000-\u9fff\uff00-\uffef]) (?=[\u3000-\u9fff\uff00-\uffef])", "", text)
    return text, api.MeanTextConf()


def image_text(img: Image.Image) -> str:
    img = ImageOps.exif_transpose(img).convert("L")      # поворот по EXIF (фото с телефона), оттенки серого
    if min(img.size) < 1000:                              # мелкие фото и скриншоты — увеличить, Tesseract любит ~300 dpi
        img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    img = ImageOps.autocontrast(img)
    langs = page_langs(img)
    text, conf = _run(img, langs)
    if LANGS_CJK and conf < LOW_CONF:                     # OSD ошибся или страница плохая — проверить другой моделью
        other = LANGS if langs == LANGS_CJK else LANGS_CJK
        text2, conf2 = _run(img, other)
        if conf2 > conf:
            text = text2
    return text


def image_layout(img: Image.Image) -> list[dict]:
    """Text lines and words with boxes in original image pixel coordinates."""
    original = ImageOps.exif_transpose(img)
    work = original.convert("L")
    scale = 2 if min(work.size) < 1000 else 1
    if scale > 1:
        work = work.resize((work.width * scale, work.height * scale), Image.LANCZOS)
    work = ImageOps.autocontrast(work)
    api = _tesseract(page_langs(work))
    api.SetImage(work)
    # Tesseract creates its ResultIterator only after recognition. Without this
    # explicit call the layout endpoint can return a false empty result.
    api.Recognize()
    iterator = api.GetIterator()
    if iterator is None:
        return []
    lines = []
    level = tesserocr.RIL.TEXTLINE
    while True:
        if iterator.IsAtBeginningOf(level):
            text = (iterator.GetUTF8Text(level) or "").strip()
            bbox = iterator.BoundingBox(level)
            if text and bbox:
                lines.append({"text": text, "bbox": bbox, "words": []})
        if not iterator.Next(level):
            break
    # Component enumeration mutates the Tesseract image state, so perform it
    # after the line iterator has finished.
    for _, box, _, _ in api.GetComponentImages(tesserocr.RIL.WORD, text_only=True):
        x, y, width, height = box["x"], box["y"], box["w"], box["h"]
        api.SetRectangle(x, y, width, height)
        word = (api.GetUTF8Text() or "").strip()
        if not word:
            continue
        item = {"text": word, "bbox": [x // scale, y // scale, (x + width) // scale, (y + height) // scale]}
        for line in lines:
            if line["bbox"][1] <= y + height / 2 <= line["bbox"][3]:
                line["words"].append(item)
                break
    for line in lines:
        line["bbox"] = [v // scale for v in line["bbox"]]
    return lines


def receipt_from_qr(text: str) -> str:
    """QR кассового чека: t=20261021T1530&s=1250.00&fn=…&i=…&fp=…&n=1 → реквизиты для проверки чека в ФНС."""
    q = {k: v[0] for k, v in parse_qs(text).items()}
    if not {"t", "s", "fn"} <= q.keys():
        return ""
    when = q["t"]
    # формат по длине: strptime с %S «съедает» цифры минут (1530 → 15:03:0)
    fmt = {13: "%Y%m%dT%H%M", 15: "%Y%m%dT%H%M%S"}.get(len(q["t"]))
    if fmt:
        try:
            when = datetime.strptime(q["t"], fmt).strftime("%d.%m.%Y %H:%M")
        except ValueError:
            pass
    return ("**QR-код кассового чека**\n"
            f"- Дата и время: {when}\n"
            f"- Сумма: {q['s']} руб.\n"
            f"- Тип: {RECEIPT_TYPES.get(q.get('n', ''), q.get('n', '—'))}\n"
            f"- ФН: {q['fn']}\n"
            f"- ФД: {q.get('i', '—')}\n"
            f"- ФП: {q.get('fp', '—')}")


def qr_blocks(img: Image.Image) -> List[str]:
    blocks = []
    for code in zxingcpp.read_barcodes(img):
        if code.format == zxingcpp.BarcodeFormat.QRCode and code.text:
            blocks.append(receipt_from_qr(code.text) or f"**QR-код:** {code.text}")
    return blocks


def _page(img: Image.Image) -> str:
    parts = [image_text(img)] + qr_blocks(img)
    return "\n\n".join(p for p in parts if p)


def pdf_pages(data: bytes) -> List[str]:
    pdf = pdfium.PdfDocument(data)
    try:
        pages = []
        for i in range(min(len(pdf), MAX_PAGES)):
            page = pdf[i]
            text = page.get_textpage().get_text_range().strip()
            if len(text) >= MIN_TEXT_LAYER:
                pages.append(text)
            else:
                pages.append(_page(page.render(scale=DPI / 72).to_pil()))
        return pages
    finally:
        pdf.close()


def image_pages(data: bytes) -> List[str]:
    img = Image.open(io.BytesIO(data))
    pages = []
    for frame in range(min(getattr(img, "n_frames", 1), MAX_PAGES)):   # многостраничный TIFF
        img.seek(frame)
        pages.append(_page(img.copy()))
    return pages


def image_layout_bytes(data: bytes) -> list[dict]:
    """The layout API is raster-only; PDF callers enumerate their own images."""
    return image_layout(Image.open(io.BytesIO(data)))


def recognize(data: bytes) -> List[str]:
    """Текст по страницам (Markdown). PDF — по сигнатуре, остальное — как изображение."""
    if data[:5] == b"%PDF-":
        return pdf_pages(data)
    return image_pages(data)
