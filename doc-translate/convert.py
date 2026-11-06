"""Преобразования форматов: PDF с текстом → DOCX (pdf2docx; перевод PDF идёт на месте — pdf_translate.py), скан PDF → текст через ocr-ru → DOCX,
DOC/RTF/ODT → DOCX и DOCX → PDF (LibreOffice без интерфейса)."""
import base64
import os
import subprocess
from pathlib import Path

import pymupdf                                # зависимость pdf2docx
import httpx
from docx import Document
from pdf2docx import Converter

OCR_BASE = os.getenv("OCR_BASE", "http://ocr-ru:8000/v1").rstrip("/")
OCR_KEY = os.getenv("OCR_API_KEY", "")
MIN_TEXT_PER_PAGE = 50                        # символов текстового слоя на страницу: меньше — скан


def soffice(src: Path, fmt: str, outdir: Path) -> Path:
    """LibreOffice: отдельный профиль на каждый вызов — параллельные задания не блокируют друг друга."""
    profile = outdir / ".lo-profile"
    subprocess.run(["soffice", f"-env:UserInstallation=file://{profile}", "--headless", "--norestore",
                    "--convert-to", fmt, "--outdir", str(outdir), str(src)],
                   check=True, capture_output=True, timeout=600)
    out = outdir / f"{src.stem}.{fmt.split(':')[0]}"
    if not out.exists():
        raise RuntimeError(f"LibreOffice не создал {out.name}")
    return out


def is_scan(pdf: Path) -> bool:
    with pymupdf.open(pdf) as doc:
        text = sum(len(page.get_text().strip()) for page in doc)
        return text < MIN_TEXT_PER_PAGE * max(1, len(doc))


def ocr_to_docx(pdf: Path, dst: Path) -> None:
    data = "data:application/pdf;base64," + base64.b64encode(pdf.read_bytes()).decode()
    resp = httpx.post(f"{OCR_BASE}/ocr", timeout=900, headers={"Authorization": f"Bearer {OCR_KEY}"},
                      json={"model": "ocr-ru", "document": {"type": "document_url", "document_url": data}})
    resp.raise_for_status()
    doc = Document()
    for i, page in enumerate(resp.json()["pages"]):
        if i:
            doc.add_page_break()
        for line in page["markdown"].splitlines():
            doc.add_paragraph(line)
    doc.save(dst)


def pdf_to_docx(src: Path, dst: Path) -> None:
    cv = Converter(str(src))
    try:
        cv.convert(str(dst))
    finally:
        cv.close()


def to_docx(src: Path, workdir: Path) -> tuple[Path, str]:
    """Входной файл → DOCX для перевода. Второе значение — пояснение пользователю (пусто, если пояснять нечего)."""
    ext = src.suffix.lower()
    if ext == ".docx":
        return src, ""
    if ext == ".pdf":
        dst = workdir / f"{src.stem}.docx"
        if is_scan(src):
            ocr_to_docx(src, dst)
            return dst, "скан: текст распознан, вёрстка упрощена"
        pdf_to_docx(src, dst)
        return dst, "PDF преобразован в DOCX; сложная вёрстка (колонки, рамки) может немного сместиться"
    if ext in (".doc", ".rtf", ".odt"):
        return soffice(src, "docx", workdir), f"{ext[1:].upper()} преобразован в DOCX"
    raise ValueError("Поддерживаются DOCX, PDF, DOC, RTF, ODT")


def to_pdf(docx: Path, outdir: Path) -> Path:
    return soffice(docx, "pdf", outdir)
