"""Перевод PDF с текстовым слоем «на месте»: линии, рамки, таблицы, картинки, колонки и подписи страницы не
трогаются, меняется только текст. Каждый текстовый блок страницы (абзац, ячейка, строка колонтитула) стирается
и на то же место пишется перевод тем же размером, цветом и начертанием; не помещается — шрифт уменьшается.

Блок может немного вырасти вправо и вниз — до соседнего текста, линии таблицы или края колонки, чтобы русский
текст (он длиннее английского на 15–30 %) не мельчал. Выделение слов внутри блока — маркеры ⟦n⟧, как в DOCX.
Повёрнутый текст и блоки без букв (номера, суммы, даты) остаются как были."""
import functools
import html
import os
import re
from dataclasses import dataclass, field
from typing import Callable, List, Tuple

import pymupdf

from docx_translate import ANY_TAG_RE, LETTER_RE, TAG_RE, Segment, Translator

FONT_DIRS = [d for d in os.getenv("FONT_DIRS", "/usr/share/fonts/truetype/liberation:"
                                  "/usr/share/fonts/truetype/crosextra:/usr/share/fonts/truetype/wqy").split(":")
             if os.path.isdir(d)]
# семейство исходного шрифта → файлы (обычный, жирный, курсив, жирный курсив)
FAMILIES = {
    "serif": ("LiberationSerif-Regular.ttf", "LiberationSerif-Bold.ttf",
              "LiberationSerif-Italic.ttf", "LiberationSerif-BoldItalic.ttf"),
    "sans": ("LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf",
             "LiberationSans-Italic.ttf", "LiberationSans-BoldItalic.ttf"),
    "mono": ("LiberationMono-Regular.ttf", "LiberationMono-Bold.ttf",
             "LiberationMono-Italic.ttf", "LiberationMono-BoldItalic.ttf"),
    "calibri": ("Carlito-Regular.ttf", "Carlito-Bold.ttf", "Carlito-Italic.ttf", "Carlito-BoldItalic.ttf"),
    "cambria": ("Caladea-Regular.ttf", "Caladea-Bold.ttf", "Caladea-Italic.ttf", "Caladea-BoldItalic.ttf"),
    "cjk": ("wqy-microhei.ttc",) * 4,
}
SERIF_RE = re.compile(r"times|serif|roman|georgia|garamond|book|minion|pt.?astra.?serif", re.I)
MONO_RE = re.compile(r"courier|mono|consol", re.I)
GAP = 1.5                                     # зазор до соседнего текста и линий, пт
REDACT = dict(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
              text=pymupdf.PDF_REDACT_TEXT_REMOVE)


@dataclass
class Block:
    page: int
    rect: pymupdf.Rect
    spans: List[pymupdf.Rect]
    styles: List[Tuple[bool, bool]]           # (жирный, курсив) каждой группы маркеров
    size: float
    color: int
    family: str
    align: str
    segment: Segment = field(default=None)
    leader: pymupdf.Rect = None               # пункт оглавления на скане: отточие после названия
    number: pymupdf.Rect = None               # и номер страницы за ним


def _style(span) -> Tuple[bool, bool]:
    font = span["font"].lower()
    return (bool(span["flags"] & 16) or "bold" in font or "black" in font,
            bool(span["flags"] & 2) or "italic" in font or "oblique" in font)


def _family(font: str) -> str:
    low = font.lower()
    if "calibri" in low or "carlito" in low:
        return "calibri"
    if "cambria" in low or "caladea" in low:
        return "cambria"
    if MONO_RE.search(low):
        return "mono"
    return "serif" if SERIF_RE.search(low) else "sans"


def _horizontal(line) -> bool:
    return abs(line["dir"][0] - 1) < 0.01 and abs(line["dir"][1]) < 0.01


def _rect(spans) -> pymupdf.Rect:
    r = pymupdf.Rect(spans[0]["bbox"])
    for sp in spans[1:]:
        r |= pymupdf.Rect(sp["bbox"])
    return r


def _chunks(page: pymupdf.Page) -> List[list]:
    """Строки страницы, разрезанные по большим промежуткам (табуляция, соседняя ячейка или колонка)."""
    chunks = []
    d = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
    for b in d["blocks"]:
        for ln in b.get("lines", []):
            if not _horizontal(ln):
                continue
            cur = []
            for sp in ln["spans"]:
                if not sp["text"]:
                    continue
                if cur and cur[-1]["text"].strip() and sp["bbox"][0] - cur[-1]["bbox"][2] > 1.5 * sp["size"]:
                    chunks.append(cur)
                    cur = []
                if sp["text"].strip() or cur:
                    cur.append(sp)
            if cur:
                chunks.append(cur)
    out = []
    for c in chunks:
        while c and not c[-1]["text"].strip():
            c = c[:-1]
        if c:
            out.append(c)
    return sorted(out, key=lambda c: (round(c[0]["bbox"][1]), c[0]["bbox"][0]))


CLAUSE_RE = re.compile(r"\s*\d{1,2}(\s?[.．]\s?[\dOo]{1,2}){1,3}[.．]?(\s|$)")  # «1.0.3», «1. O. 2» — новый пункт
LEADER_RE = re.compile(r"[.．·•…‥_]{3,}|…{2,}")    # отточие оглавления


def _split_leaders(words: list) -> list:
    """«措施…….» одним словом OCR → «措施» и «…….»: ширина делится пропорционально числу знаков."""
    out = []
    for x0, y0, x1, y1, word in words:
        m = LEADER_RE.search(word)
        if m and m.start() > 0:
            cut = x0 + (x1 - x0) * m.start() / len(word)
            out += [(x0, y0, cut, y1, word[:m.start()]), (cut, y0, x1, y1, word[m.start():])]
        else:
            out.append((x0, y0, x1, y1, word))
    return out


def _word_chunks(page: pymupdf.Page) -> List[list]:
    """Скан после OCR: строки из слов, разрезанные по промежуткам больше высоты строки. OCR склеивает в одну строку
    слова соседних колонок и ячеек таблицы — по словам их можно разделить. И наоборот, режет одну строку на куски
    (ABBYY: «1.0.3 …为6、» | «7、» | «8 和9 度…») — поэтому строки собираются заново, по высоте слов на странице."""
    rows: List[list] = []
    for x0, y0, x1, y1, word, *_ in sorted(page.get_text("words"), key=lambda w: (w[1] + w[3]) / 2):
        mid, h = (y0 + y1) / 2, y1 - y0
        if rows and abs(mid - rows[-1][0]) < 0.35 * min(h, rows[-1][1]):
            rows[-1][2].append((x0, y0, x1, y1, word))
        else:
            rows.append([mid, h, [(x0, y0, x1, y1, word)]])
    chunks = []
    for _, _, words in rows:
        words.sort()
        height = sorted(w[3] - w[1] for w in words)[len(words) // 2]
        # кегль — по самому длинному слову с буквами (у номера пункта и отточия рамка OCR выше), но не меньше 3/4
        # самой высокой рамки строки: в оглавлении OCR обжимает иероглифы до 0,7 кегля, а номер страницы — нет
        longest = max(words, key=lambda w: len(w[4]) if LETTER_RE.search(w[4]) else 0)
        size = round(max(longest[3] - longest[1], 0.75 * max(w[3] - w[1] for w in words)) * 0.8, 1)
        cur = []
        for x0, y0, x1, y1, word in _split_leaders(words):
            leader = LEADER_RE.match(word)
            if cur and (x0 - cur[-1]["bbox"][2] > 1.0 * height or leader
                        or (LEADER_RE.match(cur[0]["text"]) and LETTER_RE.search(word))):
                if leader:                              # отточие оглавления с номером страницы — отдельно:
                    cur[-1]["toc"] = True               # в нём нет букв, оно остаётся как на скане; пункт
                chunks.append(cur)                      # оглавления на этом кончается
                cur = []
            if cur:
                cur[-1]["text"] += " "
            cur.append({"text": word, "bbox": (x0, y0, x1, y1), "size": size,
                        "font": "GlyphLessFont", "flags": 0, "color": 0})
        if cur:
            chunks.append(cur)
    return sorted(chunks, key=lambda c: (round(c[0]["bbox"][1]), c[0]["bbox"][0]))


def ocr_layer(path: str) -> bool:
    """Скан, на который OCR (ABBYY, Acrobat, ocrmypdf) уже наложил невидимый текст: текстовый слой есть, но буквы,
    которые видно, — картинка. Такой PDF переводится как скан (scan=True), иначе перевод ляжет поверх оригинала."""
    hidden = shown = covered = 0
    with pymupdf.open(path) as doc:
        for page in doc:
            for t in page.get_texttrace():
                if t["type"] == 3 or t["opacity"] == 0:
                    hidden += len(t["chars"])
                else:
                    shown += len(t["chars"])
            area = page.rect.get_area()
            covered += any((pymupdf.Rect(i["bbox"]) & page.rect).get_area() > area * 0.5
                           for i in page.get_image_info())
        return hidden > 0 and hidden >= 0.7 * (hidden + shown) and covered >= len(doc) / 2


def _visible(page: pymupdf.Page) -> List[Tuple[pymupdf.Rect, int]]:
    """Видимый текст поверх скана (надпись, штамп, колонтитул) — рамка и цвет: перевод пишется тем же цветом."""
    out = []
    for t in page.get_texttrace():
        if t["type"] != 3 and t["opacity"] > 0 and t["chars"]:
            c = tuple(t["color"])
            if len(c) == 1:                                       # оттенок серого
                c = c * 3
            elif len(c) == 4:                                     # CMYK
                c = tuple((1 - v) * (1 - c[3]) for v in c[:3])
            c = [int(v * 255) for v in c] if len(c) == 3 else [0, 0, 0]
            out.append((pymupdf.Rect(t["bbox"]), (c[0] << 16) | (c[1] << 8) | c[2]))
    return out


def _gray(page: pymupdf.Page, dpi: int = 100):
    import numpy as np
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, :pix.width] < 128


def _ink(img, rects: List[pymupdf.Rect], dpi: int = 100) -> float:
    """Доля тёмных точек скана под словами: у жирного текста она заметно выше, чем у обычного того же кегля."""
    k = dpi / 72
    dark = total = 0
    for r in rects:
        part = img[max(int(r.y0 * k), 0):max(int(r.y1 * k), 0), max(int(r.x0 * k), 0):max(int(r.x1 * k), 0)]
        dark += int(part.sum())
        total += part.size
    return dark / total if total else 0.0


def _scan_lines(page: pymupdf.Page) -> List[pymupdf.Rect]:
    """Линии таблиц и рамок на картинке скана: длинные непрерывные тёмные отрезки (от 40 пт)."""
    import numpy as np
    dpi = 100
    img = _gray(page, dpi)
    k = 72 / dpi
    min_run = int(40 / k)
    out = []

    def runs(row):
        d = np.diff(np.concatenate(([0], row.astype(np.int8), [0])))
        starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
        return [(a, b) for a, b in zip(starts, ends) if b - a >= min_run]
    for y in range(img.shape[0]):
        for a, b in runs(img[y]):
            out.append(pymupdf.Rect(a * k, y * k, b * k, (y + 1) * k))
    for x in range(img.shape[1]):
        for a, b in runs(img[:, x]):
            out.append(pymupdf.Rect(x * k, a * k, (x + 1) * k, b * k))
    return out


def _clip_to_lines(r: pymupdf.Rect, core: pymupdf.Rect, lines: List[pymupdf.Rect]) -> pymupdf.Rect:
    """Сузить r (забеливание вокруг строки core) так, чтобы не задеть линии таблиц."""
    r = pymupdf.Rect(r)
    for ln in lines:
        if ln.width >= ln.height:                    # горизонтальная
            if ln.x0 < r.x1 and ln.x1 > r.x0:
                if core.y1 <= ln.y0 < r.y1:
                    r.y1 = ln.y0 - 0.3
                elif r.y0 < ln.y1 <= core.y0:
                    r.y0 = ln.y1 + 0.3
        else:                                         # вертикальная
            if ln.y0 < r.y1 and ln.y1 > r.y0:
                if core.x1 <= ln.x0 < r.x1:
                    r.x1 = ln.x0 - 0.3
                elif r.x0 < ln.x1 <= core.x0:
                    r.x0 = ln.x1 + 0.3
    return r


def _clip_to_boxes(r: pymupdf.Rect, core: pymupdf.Rect, boxes: List[pymupdf.Rect]) -> pymupdf.Rect:
    """Сузить r так, чтобы не задеть соседний текст, который остаётся как есть (числа, суммы)."""
    r = pymupdf.Rect(r)
    for o in boxes:
        if not o.intersects(r) or o.intersects(core):
            continue
        if o.x0 >= core.x1:
            r.x1 = min(r.x1, o.x0 - 1)
        elif o.x1 <= core.x0:
            r.x0 = max(r.x0, o.x1 + 1)
        elif o.y0 >= core.y1:
            r.y1 = min(r.y1, o.y0 - 1)
        elif o.y1 <= core.y0:
            r.y0 = max(r.y0, o.y1 + 1)
    return r


def _separated(a: pymupdf.Rect, b: pymupdf.Rect, lines: List[pymupdf.Rect]) -> bool:
    """Между строками проходит линия (граница строки таблицы, подчёркивание раздела)."""
    ya, yb = (a.y0 + a.y1) / 2, (b.y0 + b.y1) / 2
    return any(ln.height <= 2 and ya < (ln.y0 + ln.y1) / 2 < yb and ln.x0 < min(a.x1, b.x1) and ln.x1 > max(a.x0, b.x0)
               for ln in lines)


def _first_word(line: list) -> float:
    """Ширина первого слова строки: не влезло в конец предыдущей строки — значит, это продолжение абзаца."""
    sp = line[0]
    text = sp["text"].lstrip()
    word = text.split(" ", 1)[0]
    return (sp["bbox"][2] - sp["bbox"][0]) * (len(word) + 1) / max(len(sp["text"]), 1)


def _paragraphs(chunks: List[list], lines: List[pymupdf.Rect], scan: bool = False) -> List[List[list]]:
    """Строки → абзацы: следующая строка сразу под предыдущей, с того же левого края (или первая строка с отступом),
    того же размера, первое слово не поместилось бы в конец предыдущей (иначе там конец абзаца), между ними нет линии.
    На скане размер и промежуток берутся по рамкам слов OCR — они гуляют от строки к строке, поэтому допуск больше,
    а строка с номером пункта («1.0.3») всегда начинает новый абзац."""
    paras: List[List[list]] = []
    for c in chunks:
        r, size = _rect(c), max(sp["size"] for sp in c)
        for p in reversed(paras[-12:]):
            last = _rect(p[-1])
            lsize = max(sp["size"] for sp in p[-1])
            first_indent = len(p) == 1 and 0 < last.x0 - r.x0 < 40
            right = max(_rect(ln).x1 for ln in p + [c])
            if (abs(size - lsize) < (max(0.6, 0.15 * size) if scan else 0.6)
                    and -0.3 * size < r.y0 - last.y1 < (0.9 if scan else 0.6) * size
                    and (abs(r.x0 - last.x0) < 3 or first_indent)
                    and last.x1 + _first_word(c) > right - 2 and not _separated(last, r, lines)
                    and not p[-1][-1].get("toc") and not LEADER_RE.match(p[-1][0]["text"])
                    and not (scan and CLAUSE_RE.match("".join(sp["text"] for sp in c[:4])))):
                p.append(c)
                break
        else:
            paras.append([c])
    return paras


def _align(lines: List[pymupdf.Rect], page: pymupdf.Rect, content: pymupdf.Rect) -> str:
    if len(lines) == 1:
        b = lines[0]
        if abs((b.x0 + b.x1) / 2 - page.width / 2) < 3 and b.width < content.width * 0.8:
            return "center"
        if abs(b.x1 - content.x1) < 3 and b.x0 > page.width / 2:
            return "right"
        return "left"
    lefts = [b.x0 for b in lines]
    rights = [b.x1 for b in lines]
    centers = [(b.x0 + b.x1) / 2 for b in lines]
    if max(centers) - min(centers) < 2 and max(lefts) - min(lefts) > 4:
        return "center"
    if max(rights[:-1]) - min(rights[:-1]) < 2 and max(lefts[1:]) - min(lefts[1:]) < 2 and rights[-1] < rights[0] - 8:
        return "justify"
    return "left"


def _text(para: List[list]) -> Tuple[List[str], List[Tuple[bool, bool]]]:
    """Текст абзаца группами одинакового начертания; перенос по дефису склеивается."""
    groups: List[str] = []
    styles: List[Tuple[bool, bool]] = []
    for i, line in enumerate(para):
        if i:
            nxt = line[0]["text"].lstrip()[:1]
            if groups[-1].endswith("-") and nxt.islower():
                groups[-1] = groups[-1][:-1]
            elif not groups[-1].endswith(" "):
                groups[-1] += " "
        for sp in line:
            st = _style(sp) if sp["text"].strip() or not styles else styles[-1]
            if styles and styles[-1] == st:
                groups[-1] += sp["text"]
            else:
                groups.append(sp["text"])
                styles.append(st)
    return groups, styles


def _drawn(page: pymupdf.Page) -> List[pymupdf.Rect]:
    area = page.rect.get_area()
    return [pymupdf.Rect(p["rect"]) for p in page.get_drawings() if pymupdf.Rect(p["rect"]).get_area() < area * 0.8]


def collect(doc: pymupdf.Document, scan: bool = False) -> List[Block]:
    blocks: List[Block] = []
    for pno, page in enumerate(doc):
        chunks = _word_chunks(page) if scan else _chunks(page)
        if not chunks:
            continue
        content = _rect([sp for c in chunks for sp in c])
        first = len(blocks)
        for para in _paragraphs(chunks, _scan_lines(page) if scan else _drawn(page), scan):
            groups, styles = _text(para)
            full = "".join(groups)
            if not LETTER_RE.search(full):
                continue
            spans = [sp for line in para for sp in line]
            main = max(spans, key=lambda sp: len(sp["text"]))
            source = full.strip() if len(groups) == 1 else \
                "".join(f"⟦{i}⟧{g}⟦/{i}⟧" for i, g in enumerate(groups, 1))
            line_rects = [_rect(line) for line in para]
            blk = Block(pno, _rect(spans), [pymupdf.Rect(sp["bbox"]) for sp in spans], styles,
                        main["size"], main["color"], _family(main["font"]),
                        _align(line_rects, page.rect, content))
            blk.segment = Segment(len(blocks) + 1, [], source)
            if para[-1][-1].get("toc"):
                _toc_leader(blk, para[-1], chunks)
            blocks.append(blk)
        if scan and len(blocks) - first >= 3:
            _scan_style(page, blocks[first:])
    return blocks


def _toc_leader(blk: Block, line: list, chunks: List[list]) -> None:
    """Пункт оглавления: найти отточие и номер страницы справа на той же строке. Перевод длиннее названия —
    забеливаем название вместе с отточием и рисуем отточие заново до номера."""
    r = _rect(line)
    right = [c for c in chunks if LEADER_RE.match(c[0]["text"]) and c[0]["bbox"][0] >= r.x1 - 2
             and c[0]["bbox"][1] < r.y1 and c[0]["bbox"][3] > r.y0]
    if not right:
        return
    lc = min(right, key=lambda c: c[0]["bbox"][0])
    dots = [sp for sp in lc if LEADER_RE.fullmatch(sp["text"].strip())]
    rest = [sp for sp in lc if sp not in dots]
    if dots:
        blk.leader = _rect(dots)
        blk.number = _rect(rest) if rest else None
        blk.align = "left"


def _scan_style(page: pymupdf.Page, blocks: List[Block]) -> None:
    """У слов скана нет шрифта: берём шрифт с засечками (так набрано большинство документов), жирный — по насыщенности
    строки относительно обычного текста страницы, цвет — у видимого текста поверх картинки (надпись, штамп)."""
    img = _gray(page)
    ink = {id(b): _ink(img, b.spans) for b in blocks}
    normal = sorted(ink.values())[len(blocks) // 2]
    shown = _visible(page)
    for b in blocks:
        b.family = "serif"
        if normal and ink[id(b)] > 1.3 * normal:
            b.styles = [(True, False)] * len(b.styles)
        colors = [c for r, c in shown if r.intersects(b.rect)]
        b.color = colors[0] if colors else 0


def _room(page: pymupdf.Page, blk: Block, others: List[pymupdf.Rect], lines: List[pymupdf.Rect],
          content: pymupdf.Rect) -> pymupdf.Rect:
    """Сколько места можно занять: до соседнего текста (с отступом в кегль), линий таблицы и края текста страницы;
    внутри рамки или заливки, которая блок окружает (ячейка таблицы). Линии — нулевой площади, поэтому
    intersects() для них ложно, и они всегда считаются препятствием."""
    r = blk.rect
    x0, x1, y1 = content.x0, max(content.x1, page.rect.x1 - content.x0), page.rect.y1 - 18   # правое поле = левому
    frames = [c for c in lines if c.x0 <= r.x0 + 1 and c.y0 <= r.y0 + 1 and c.x1 >= r.x1 - 1 and c.y1 >= r.y1 - 1]
    if frames:                                                                # рамка или заливка вокруг — ячейка
        f = min(frames, key=lambda c: c.get_area())
        x0, x1, y1 = f.x0 + GAP, f.x1 - GAP, min(y1, f.y1 - GAP)
    gutter = max(GAP, blk.size)                                               # до соседней колонки текста
    for o, gap in [(o, gutter) for o in others] + [(o, GAP) for o in lines]:
        if o.intersects(r):
            continue
        if o.y0 < r.y1 and o.y1 > r.y0:                                       # сбоку
            if o.x0 >= r.x1:
                x1 = min(x1, o.x0 - gap)
            elif o.x1 <= r.x0:
                x0 = max(x0, o.x1 + gap)
    if blk.align == "left" or blk.align == "justify":
        x0 = r.x0
    elif blk.align == "right":
        x1 = r.x1
    else:                                                                     # по центру — симметрично
        half = min(r.x0 - x0, x1 - r.x1)
        x0, x1 = r.x0 - max(half, 0), r.x1 + max(half, 0)
    x0, x1 = min(x0, r.x0), max(x1, r.x1)
    for o in others + lines:
        if o.y0 >= r.y1 - 0.5 and o.x0 < x1 and o.x1 > x0 and not o.intersects(r):   # ниже
            y1 = min(y1, o.y0 - GAP)
    return pymupdf.Rect(x0, r.y0, x1, max(y1, r.y1))


def _html(blk: Block, text: str) -> str:
    def wrap(piece: str, style: Tuple[bool, bool]) -> str:
        out = html.escape(piece).replace("\n", "<br>")
        if style[1]:
            out = f"<i>{out}</i>"
        if style[0]:
            out = f"<b>{out}</b>"
        return out

    if len(blk.styles) > 1:
        found = {int(n): t for n, t in TAG_RE.findall(text)}
        if set(found) == set(range(1, len(blk.styles) + 1)):
            parts = TAG_RE.split(text)
            out = []
            for i in range(0, len(parts), 3):
                out.append(html.escape(parts[i]).replace("\n", "<br>"))
                if i + 2 < len(parts):
                    out.append(wrap(parts[i + 2], blk.styles[int(parts[i + 1]) - 1]))
            return "".join(out).strip()
        blk.segment.notes.append("маркеры форматирования потеряны — выделение слов в абзаце не сохранено")
    main = max(set(blk.styles), key=blk.styles.count)
    return wrap(ANY_TAG_RE.sub("", text).strip(), main)


@functools.lru_cache(maxsize=None)
def _has_cyrillic(fam: str) -> bool:
    """Caladea (замена Cambria) — без кириллицы: для перевода на русский берём Liberation Serif."""
    for d in FONT_DIRS:
        path = os.path.join(d, FAMILIES[fam][0])
        if os.path.exists(path):
            return pymupdf.Font(fontfile=path).has_glyph(ord("Ж")) > 0
    return False


def _fam(blk: Block, target: str) -> str:
    fam = "cjk" if target == "zh" else blk.family
    if target == "ru" and not _has_cyrillic(fam):
        fam = "serif" if fam == "cambria" else "sans"
    return fam


def _draw_leader(page: pymupdf.Page, blk: Block, room: pymupdf.Rect) -> None:
    """Пункт оглавления: отточие от конца перевода до места, где было исходное отточие (перед номером страницы)."""
    words = page.get_text("words", clip=room)
    if not words:
        return
    last = max(words, key=lambda w: (round(w[3]), w[2]))
    size = min(blk.size, last[3] - last[1])
    x, end = last[2] + size * 0.4, blk.leader.x1
    dot = pymupdf.get_text_length(".", fontname="tiro", fontsize=size)
    n = int((end - x) / dot)
    if n >= 3:
        page.insert_text((end - n * dot, last[3] - size * 0.22), "." * n, fontname="tiro", fontsize=size,
                         color=pymupdf.sRGB_to_pdf(blk.color))


def _css(blk: Block, target: str, scale: float = 1.0) -> str:
    files = FAMILIES[_fam(blk, target)]
    faces = "".join(
        f"@font-face {{font-family: F; src: url({f}); font-weight: {'bold' if i & 1 else 'normal'}; "
        f"font-style: {'italic' if i & 2 else 'normal'};}}" for i, f in enumerate(files))
    return (faces + f"* {{font-family: F; font-size: {blk.size * scale:.2f}px; line-height: 1.12; "
            f"color: #{blk.color:06x}; margin: 0; padding: 0; text-align: {blk.align};}}")


def translate_pdf(src: str, dst: str, translator: Translator, target: str,
                  progress: Callable[[int, int], None] = lambda done, total: None, scan: bool = False) -> List[Segment]:
    """scan=True — PDF после ocrmypdf: текст невидимым слоем поверх картинки страницы. Тогда под каждой строкой
    картинка забеливается и перевод пишется в границах строки (линии таблиц на скане — пиксели, их не видно)."""
    doc = pymupdf.open(src)
    blocks = collect(doc, scan)
    segments = [b.segment for b in blocks]
    translator(segments, progress)

    archive = pymupdf.Archive()
    for d in FONT_DIRS:
        archive.add(d)
    for pno, page in enumerate(doc):
        todo = [b for b in blocks if b.page == pno and b.segment.result]
        if not todo:
            continue
        everything = [_rect(c) for c in (_word_chunks(page) if scan else _chunks(page))] + \
            [pymupdf.Rect(b["bbox"]) for b in page.get_text("dict")["blocks"] if b["type"] == 1]
        content = pymupdf.Rect(min(r.x0 for r in everything), 0, max(r.x1 for r in everything), page.rect.y1)
        drawn = _drawn(page)
        if scan:
            # линии таблиц на скане — пиксели: находим их по картинке, чтобы не залезть на них ни переводом, ни белым
            lines = _scan_lines(page)
            others = [_rect(c) for c in _word_chunks(page)]
            rooms = {}
            for b in todo:
                r = _room(page, b, [o for o in others if not b.rect.contains(o)], lines, content)
                x1 = b.leader.x1 if b.leader else min(r.x1, b.rect.x1 + max(40, b.rect.width * 0.5))
                rooms[id(b)] = pymupdf.Rect(r.x0, b.rect.y0, x1, b.rect.y1 + 0.3 * b.size)
            for b in todo:                     # невидимый текст OCR — убрать, чтобы не мешал поиску и копированию
                for s in b.spans + ([b.leader] if b.leader else []):
                    page.add_redact_annot(s, fill=False)
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                  text=pymupdf.PDF_REDACT_TEXT_REMOVE)
            for b in todo:                     # белым поверх картинки: строка с большим запасом (рамки слов OCR
                h = b.size                     # ниже и уже настоящих букв), но не дальше линий таблиц
                core = pymupdf.Rect(b.rect)
                if b.leader:                   # пункт оглавления — вместе с отточием, по высоте строки номера
                    core |= b.leader
                    if b.number:
                        core.y0, core.y1 = min(core.y0, b.number.y0), max(core.y1, b.number.y1)
                wide = pymupdf.Rect(core.x0 - 0.3 * h, core.y0 - 0.5 * h, core.x1 + 0.8 * h, core.y1 + 0.9 * h)
                gone = [pymupdf.Rect(sp) for t in todo for sp in t.spans] + [t.leader for t in todo if t.leader]
                keep = [o for o in others if not any(o.intersects(g) for g in gone)] + \
                    [t.number for t in todo if t.number]   # отточия оглавления забеливаются, номера страниц — нет
                wide = _clip_to_boxes(_clip_to_lines(wide, core, lines), core, keep)
                page.draw_rect(wide, color=None, fill=(1, 1, 1), overlay=True)
        else:
            rooms = {id(b): _room(page, b, [r for r in everything if not b.rect.contains(r)], drawn, content)
                     for b in todo}
            for b in todo:
                for s in b.spans:              # только сам текст: заливку и линии не трогаем
                    h = s.height * 0.2
                    page.add_redact_annot(pymupdf.Rect(s.x0, s.y0 + h, s.x1, s.y1 - h))
            page.apply_redactions(**REDACT)
        for b in todo:
            spare, _ = page.insert_htmlbox(rooms[id(b)], _html(b, b.segment.result), css=_css(b, target),
                                           archive=archive, scale_low=0)
            if b.leader:
                _draw_leader(page, b, rooms[id(b)])
            if spare < 0:
                b.segment.notes.append("перевод не поместился в блок")
    doc.subset_fonts()
    doc.save(dst, garbage=3, deflate=True)
    return segments
