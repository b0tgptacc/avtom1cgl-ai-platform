"""Перевод DOCX с сохранением вёрстки: меняется только текст внутри w:t, всё остальное (стили абзацев, таблицы,
колонтитулы, нумерация, поля, картинки, разрывы) остаётся как было.

Абзац → сегмент. Если в абзаце фрагменты с разным форматированием (жирный, курсив, другой шрифт), они размечаются
маркерами ⟦1⟧…⟦/1⟧, модель переводит текст и оставляет маркеры вокруг соответствующих слов, после перевода
форматирование возвращается на нужные слова. Маркеры не угловые — угловые <PERSON_1> заняты маскированием ПДн.
Маркеры потерялись — весь перевод абзаца уходит в первый фрагмент (вёрстка абзаца сохраняется, выделение слов — нет)."""
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from docx import Document
from docx.oxml.ns import qn
from lxml import etree

# части пакета DOCX, где есть переводимый текст
PART_RE = re.compile(r"^/word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$")
TAG_RE = re.compile(r"⟦(\d+)⟧(.*?)⟦/\1⟧", re.S)
ANY_TAG_RE = re.compile(r"⟦/?\d+⟧")
LETTER_RE = re.compile(r"[^\W\d_]")          # есть хоть одна буква — есть что переводить
W_P, W_R, W_T = qn("w:p"), qn("w:r"), qn("w:t")
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


@dataclass
class Group:
    """Подряд идущие фрагменты (w:r) с одинаковым форматированием."""
    runs: List[etree._Element]
    text: str


@dataclass
class Segment:
    id: int
    groups: List[Group]
    source: str                              # что уходит модели (с маркерами, если групп больше одной)
    result: Optional[str] = None
    notes: List[str] = field(default_factory=list)


def _owner_paragraph(el):
    p = el.getparent()
    while p is not None and p.tag != W_P:
        p = p.getparent()
    return p


def _run_text(run) -> str:
    return "".join(t.text or "" for t in run.iter(W_T))


def _rpr_key(run) -> bytes:
    rpr = run.find(qn("w:rPr"))
    return etree.tostring(rpr) if rpr is not None else b""


def _groups(p) -> List[Group]:
    """Фрагменты абзаца, сгруппированные по форматированию. Фрагменты вложенных абзацев (надписи) — не наши."""
    groups: List[Group] = []
    for run in p.iter(W_R):
        if _owner_paragraph(run) is not p:
            continue
        text = _run_text(run)
        if not text:
            continue
        if groups and _rpr_key(groups[-1].runs[-1]) == _rpr_key(run):
            groups[-1].runs.append(run)
            groups[-1].text += text
        else:
            groups.append(Group([run], text))
    return groups


def _text_roots(doc: Document):
    """Корни XML частей с текстом. Части, которые python-docx не разбирает (сноски), читаются из blob
    и записываются обратно в finalize()."""
    roots, raw = [], []
    for part in doc.part.package.iter_parts():
        if not PART_RE.match(str(part.partname)):
            continue
        if hasattr(part, "_element"):
            roots.append(part._element)
        else:
            root = etree.fromstring(part.blob)
            roots.append(root)
            raw.append((part, root))
    return roots, raw


def collect(doc: Document, roots) -> List[Segment]:
    segments: List[Segment] = []
    for root in roots:
        for p in root.iter(W_P):
            groups = _groups(p)
            full = "".join(g.text for g in groups)
            if not LETTER_RE.search(full):               # номера, даты, суммы без слов — не трогаем
                continue
            if len(groups) == 1:
                source = groups[0].text
            else:
                source = "".join(f"⟦{i}⟧{g.text}⟦/{i}⟧" for i, g in enumerate(groups, 1))
            segments.append(Segment(len(segments) + 1, groups, source))
    return segments


def _set_run_text(run, text: str) -> None:
    """Текст фрагмента: первый w:t получает весь текст, остальные w:t удаляются (табуляции, поля и картинки остаются)."""
    ts = list(run.iter(W_T))
    if not ts:
        t = etree.SubElement(run, W_T)
        ts = [t]
    ts[0].text = text
    ts[0].set(XML_SPACE, "preserve")
    for t in ts[1:]:
        t.getparent().remove(t)


def _set_group_text(group: Group, text: str) -> None:
    _set_run_text(group.runs[0], text)
    for run in group.runs[1:]:
        _set_run_text(run, "")


def apply(segment: Segment) -> None:
    """Вернуть перевод в документ. Маркеры на месте — по группам; иначе — весь текст в первую группу."""
    text = segment.result
    if text is None:
        return
    if len(segment.groups) == 1:
        _set_group_text(segment.groups[0], ANY_TAG_RE.sub("", text))
        return
    found: Dict[int, str] = {int(n): t for n, t in TAG_RE.findall(text)}
    if set(found) == set(range(1, len(segment.groups) + 1)):
        # текст вне маркеров (модель вынесла пробел или слово наружу) — приклеить к соседней группе
        outside = TAG_RE.split(text)
        between = [outside[i] for i in range(0, len(outside), 3)]   # куски до/между/после маркеров
        for i, g in enumerate(segment.groups, 1):
            piece = found[i]
            if i == 1:
                piece = between[0] + piece
            piece += between[i] if i < len(between) else ""
            _set_group_text(g, piece)
        return
    segment.notes.append("маркеры форматирования потеряны — выделение слов в абзаце не сохранено")
    _set_group_text(segment.groups[0], ANY_TAG_RE.sub("", text))
    for g in segment.groups[1:]:
        _set_group_text(g, "")


Translator = Callable[[List[Segment], Callable[[int, int], None]], None]


def translate_docx(src: str, dst: str, translator: Translator,
                   progress: Callable[[int, int], None] = lambda done, total: None) -> List[Segment]:
    """translator заполняет segment.result (сам бьёт на пакеты) и сообщает прогресс (готово, всего)."""
    doc = Document(src)
    roots, raw = _text_roots(doc)
    segments = collect(doc, roots)
    translator(segments, progress)
    for s in segments:
        apply(s)
    for part, root in raw:
        part._blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    doc.save(dst)
    return segments
