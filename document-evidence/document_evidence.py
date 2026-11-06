"""Deterministic, local-only evidence extraction for office documents.

The public functions return tuples of plain dictionaries so their results are easy to
serialize, compare, and retain as audit evidence.  No network or secret-dependent
configuration is used.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping, Sequence
import re

import pymupdf
from docx import Document
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

_SUPPORTED_TYPES = {".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx"}


class CoverageStatus(str, Enum):
    PASS = "PASS"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class Requirement:
    """A named requirement represented by phrases that must all be evidenced."""

    identifier: str
    phrases: tuple[str, ...]


def inventory(root: Path | str) -> tuple[dict[str, object], ...]:
    """Return supported files under *root*, sorted by their relative POSIX paths."""
    base = Path(root)
    files = [path for path in base.rglob("*") if path.is_file() and path.suffix.lower() in _SUPPORTED_TYPES]
    return tuple(
        {
            "path": path.relative_to(base).as_posix(),
            "type": _SUPPORTED_TYPES[path.suffix.lower()],
            "size_bytes": path.stat().st_size,
        }
        for path in sorted(files, key=lambda item: item.relative_to(base).as_posix())
    )


def canonical_records(paths: Iterable[Path | str], *, root: Path | str) -> tuple[dict[str, str], ...]:
    """Extract non-empty text into deterministic document, locator, text records."""
    base = Path(root).resolve()
    sources = sorted((Path(path) for path in paths), key=lambda item: _document_name(item, base))
    records: list[dict[str, str]] = []
    for source in sources:
        suffix = source.suffix.lower()
        if suffix == ".pdf":
            records.extend(_pdf_records(source, base))
        elif suffix == ".docx":
            records.extend(_docx_records(source, base))
        elif suffix == ".xlsx":
            records.extend(_xlsx_records(source, base))
        else:
            raise ValueError(f"unsupported document type: {source.suffix}")
    return tuple(records)


def assess_coverage(
    requirements: Sequence[Requirement],
    records: Sequence[Mapping[str, str]],
    *,
    blockers: Sequence[str] = (),
) -> tuple[dict[str, object], ...]:
    """Assess phrase coverage, failing closed if a caller reports extraction blockers."""
    stable_blockers = tuple(sorted(set(blockers)))
    normalized_records = tuple(dict(record) for record in records)
    results: list[dict[str, object]] = []
    for requirement in requirements:
        phrases = tuple(_canonical_text(phrase).casefold() for phrase in requirement.phrases)
        matched = tuple(
            phrase for phrase, normalized in zip(requirement.phrases, phrases)
            if any(normalized in _canonical_text(record["text"]).casefold() for record in normalized_records)
        )
        missing = tuple(phrase for phrase in requirement.phrases if phrase not in matched)
        evidence = tuple(
            record for record in normalized_records
            if any(_canonical_text(phrase).casefold() in _canonical_text(record["text"]).casefold() for phrase in matched)
        )
        status = (
            CoverageStatus.BLOCKED
            if stable_blockers
            else CoverageStatus.PASS if not missing else CoverageStatus.PARTIAL
        )
        result: dict[str, object] = {
            "requirement": requirement.identifier,
            "status": status.value,
            "matched": matched,
            "missing": missing,
            "evidence": evidence,
        }
        if stable_blockers:
            result["blockers"] = stable_blockers
        results.append(result)
    return tuple(results)


def _pdf_records(path: Path, root: Path) -> list[dict[str, str]]:
    document = _document_name(path, root)
    with pymupdf.open(path) as pdf:
        return [
            {"document": document, "locator": f"pdf:page={number}", "text": text}
            for number, page in enumerate(pdf, start=1)
            if (text := _canonical_text(page.get_text()))
        ]


def _docx_records(path: Path, root: Path) -> list[dict[str, str]]:
    document, output = _document_name(path, root), []
    source = Document(path)
    for number, paragraph in enumerate(source.paragraphs, start=1):
        if text := _canonical_text(paragraph.text):
            output.append({"document": document, "locator": f"docx:paragraph={number}", "text": text})
    for table_number, table in enumerate(source.tables, start=1):
        for row_number, row in enumerate(table.rows, start=1):
            for column_number, cell in enumerate(row.cells, start=1):
                if text := _canonical_text(cell.text):
                    output.append({
                        "document": document,
                        "locator": f"docx:table={table_number},row={row_number},col={column_number}",
                        "text": text,
                    })
    return output


def _xlsx_records(path: Path, root: Path) -> list[dict[str, str]]:
    document, output = _document_name(path, root), []
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows():
                for cell in row:
                    if cell.value is not None and (text := _canonical_text(str(cell.value))):
                        output.append({
                            "document": document,
                            "locator": f"xlsx:sheet={sheet.title},row={cell.row},col={get_column_letter(cell.column)}",
                            "text": text,
                        })
    finally:
        workbook.close()
    return output


def _document_name(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root).as_posix()


def _canonical_text(value: str) -> str:
    return re.sub(r"[ \t]+", " ", value.replace("\r\n", "\n").replace("\r", "\n")).strip()
