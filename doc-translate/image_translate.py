"""Translate raster-image text in place while preserving the image container and position.

OCR is a local ``ocr-ru`` HTTP service.  If any stage cannot be completed, the
original image bytes are returned unchanged; callers only receive a warning.
"""
import base64
import io
import os
from pathlib import Path
from statistics import median
from typing import Callable, List, Tuple

import httpx
import pymupdf
from docx import Document
from PIL import Image, ImageDraw, ImageFont, ImageOps

from docx_translate import Segment, Translator

OCR_BASE = os.getenv("OCR_BASE", "http://ocr-ru:8000/v1").rstrip("/")
OCR_KEY = os.getenv("OCR_API_KEY", "")
OCR_TIMEOUT = float(os.getenv("OCR_TIMEOUT", "60"))
MIN_IMAGE_SIDE = int(os.getenv("IMAGE_TRANSLATE_MIN_SIDE", "80"))
MIN_RENDER_FONT = int(os.getenv("IMAGE_TRANSLATE_MIN_FONT", "3"))
FONT_PATHS = [p for p in os.getenv("IMAGE_TRANSLATE_FONTS", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
                                  "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf:"
                                  "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc").split(":") if p] + ["C:/Windows/Fonts/arial.ttf"]


def ocr_image_lines(data: bytes) -> List[dict]:
    """Request local OCR line boxes. No URL is fetched by either service."""
    mime = "image/png"
    payload = "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))
    headers = {"Authorization": f"Bearer {OCR_KEY}"} if OCR_KEY else {}
    response = httpx.post(f"{OCR_BASE}/ocr/boxes", headers=headers, timeout=OCR_TIMEOUT,
                          json={"document": {"type": "image_url", "image_url": payload}})
    response.raise_for_status()
    lines = response.json().get("lines")
    if not isinstance(lines, list):
        raise ValueError("OCR returned no line boxes")
    return lines


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_PATHS:
        if Path(path).is_file():
            return ImageFont.truetype(path, size=max(size, 6))
    raise RuntimeError("no Unicode font available for image translation")


def _background(image: Image.Image, box: tuple[int, int, int, int]):
    """Use nearby pixels, rather than a hard-coded white rectangle, to erase text."""
    x0, y0, x1, y1 = box
    pixels = image.convert("RGB").load()
    samples = []
    for x, y in ((x0 - 1, y0 - 1), (x1, y0 - 1), (x0 - 1, y1), (x1, y1)):
        if 0 <= x < image.width and 0 <= y < image.height:
            samples.append(pixels[x, y])
    if not samples:
        return (255, 255, 255)
    return tuple(int(median([pixel[c] for pixel in samples])) for c in range(3))


def _render(image: Image.Image, lines: List[dict], translations: List[str]) -> bytes:
    canvas = ImageOps.exif_transpose(image).convert("RGB")
    draw = ImageDraw.Draw(canvas)
    for line, text in zip(lines, translations):
        raw = line.get("bbox")
        if not (isinstance(raw, (list, tuple)) and len(raw) == 4 and all(isinstance(v, (int, float)) for v in raw)):
            raise ValueError("invalid OCR line box")
        x0, y0, x1, y1 = (int(round(v)) for v in raw)
        if x1 <= x0 or y1 <= y0 or x0 < 0 or y0 < 0 or x1 > canvas.width or y1 > canvas.height:
            raise ValueError("OCR line box outside image")
        # Fit a translated line inside its original area; if it cannot fit, do not corrupt the image.
        font = _font(max(MIN_RENDER_FONT, int((y1 - y0) * 0.9)))
        while font.size > MIN_RENDER_FONT and draw.textbbox((0, 0), text, font=font)[2] > x1 - x0:
            font = _font(font.size - 1)
        if draw.textbbox((0, 0), text, font=font)[2] > x1 - x0:
            raise ValueError("translated text does not fit OCR line box")
        draw.rectangle((x0, y0, x1, y1), fill=_background(canvas, (x0, y0, x1, y1)))
        draw.text((x0, y0), text, fill="black", font=font)
    output = io.BytesIO()
    fmt = image.format if image.format in {"PNG", "JPEG", "WEBP", "BMP", "TIFF"} else "PNG"
    canvas.save(output, fmt)
    return output.getvalue()


def translate_raster_image(data: bytes, target: str, translator: Translator,
                           recognize: Callable[[bytes], List[dict]] = ocr_image_lines) -> Tuple[bytes, List[str]]:
    """Translate text-bearing raster bytes, returning original bytes on every failure."""
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        if min(image.size) < MIN_IMAGE_SIDE:
            return data, []
        try:
            lines = [line for line in recognize(data) if isinstance(line, dict) and str(line.get("text", "")).strip()]
        except Exception as exc:
            raise RuntimeError("OCR failed") from exc
        if not lines:
            return data, []
        segments = [Segment(i + 1, [], str(line["text"])) for i, line in enumerate(lines)]
        translator(segments, lambda _done, _total: None)
        if any(not segment.result for segment in segments):
            raise RuntimeError("translation failed")
        return _render(image, lines, [segment.result for segment in segments]), []
    except Exception as exc:
        stage = "OCR" if "recogn" in getattr(recognize, "__name__", "") or "OCR" in str(exc) else "translation/render"
        return data, [f"image text left unchanged: {stage} failed ({type(exc).__name__})"]


def translate_docx_images(path: str, target: str, translator: Translator,
                          recognize: Callable[[bytes], List[dict]] = ocr_image_lines) -> List[str]:
    """Replace only successfully rendered DOCX image blobs; keep failures byte-for-byte."""
    notes, doc = [], Document(path)
    seen = set()
    for rel in doc.part.rels.values():
        if "image" not in rel.reltype or rel.target_part.partname in seen:
            continue
        seen.add(rel.target_part.partname)
        original = rel.target_part.blob
        rendered, warning = translate_raster_image(original, target, translator, recognize)
        notes.extend(warning)
        if rendered != original:
            rel.target_part._blob = rendered
    doc.save(path)
    return notes


def translate_pdf_images(path: str, target: str, translator: Translator,
                         recognize: Callable[[bytes], List[dict]] = ocr_image_lines) -> List[str]:
    """Replace only successfully rendered raster images embedded on PDF pages."""
    notes, doc, seen = [], pymupdf.open(path), set()
    try:
        for page in doc:
            for image in page.get_images(full=True):
                xref = image[0]
                if xref in seen:
                    continue
                seen.add(xref)
                original = doc.extract_image(xref).get("image")
                if not original:
                    continue
                rendered, warning = translate_raster_image(original, target, translator, recognize)
                notes.extend(warning)
                if rendered != original:
                    page.replace_image(xref, stream=rendered)
        if seen:
            doc.save(path + ".images", garbage=3, deflate=True)
            doc.close()
            os.replace(path + ".images", path)
            return notes
    finally:
        if not doc.is_closed:
            doc.close()
    return notes
