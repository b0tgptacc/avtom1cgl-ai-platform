"""Deterministic local document evidence library tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pymupdf
from docx import Document
from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from document_evidence import (  # noqa: E402
    CoverageStatus,
    Requirement,
    assess_coverage,
    canonical_records,
    inventory,
)


def test_inventory_lists_supported_documents_in_sorted_relative_order(tmp_path: Path) -> None:
    (tmp_path / "zeta.xlsx").write_bytes(b"spreadsheet")
    (tmp_path / "ignore.txt").write_text("not a source", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "Alpha.PDF").write_bytes(b"pdf")
    (nested / "brief.docx").write_bytes(b"docx")

    assert inventory(tmp_path) == (
        {"path": "nested/Alpha.PDF", "type": "pdf", "size_bytes": 3},
        {"path": "nested/brief.docx", "type": "docx", "size_bytes": 4},
        {"path": "zeta.xlsx", "type": "xlsx", "size_bytes": 11},
    )


def test_canonical_records_have_stable_text_and_locators_for_each_format(tmp_path: Path) -> None:
    pdf_path = tmp_path / "report.pdf"
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "PDF finding")
    pdf.save(pdf_path)
    pdf.close()

    docx_path = tmp_path / "report.docx"
    document = Document()
    document.add_paragraph("DOCX finding")
    table = document.add_table(rows=1, cols=1)
    table.cell(0, 0).text = "Table finding"
    document.save(docx_path)

    xlsx_path = tmp_path / "report.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Summary"
    sheet["A1"] = "Spreadsheet finding"
    workbook.save(xlsx_path)

    records = canonical_records((pdf_path, docx_path, xlsx_path), root=tmp_path)

    assert records == (
        {"document": "report.docx", "locator": "docx:paragraph=1", "text": "DOCX finding"},
        {"document": "report.docx", "locator": "docx:table=1,row=1,col=1", "text": "Table finding"},
        {"document": "report.pdf", "locator": "pdf:page=1", "text": "PDF finding"},
        {"document": "report.xlsx", "locator": "xlsx:sheet=Summary,row=1,col=A", "text": "Spreadsheet finding"},
    )


def test_coverage_is_pass_when_all_required_phrases_have_evidence() -> None:
    result = assess_coverage(
        (Requirement("payment", ("invoice", "30 days")),),
        (
            {"document": "contract.pdf", "locator": "pdf:page=2", "text": "Invoice is payable in 30 days."},
        ),
    )

    assert result == (
        {
            "requirement": "payment",
            "status": CoverageStatus.PASS.value,
            "matched": ("invoice", "30 days"),
            "missing": (),
            "evidence": (
                {"document": "contract.pdf", "locator": "pdf:page=2", "text": "Invoice is payable in 30 days."},
            ),
        },
    )


def test_coverage_is_partial_when_only_some_phrases_have_evidence() -> None:
    result = assess_coverage(
        (Requirement("payment", ("invoice", "30 days")),),
        ({"document": "contract.pdf", "locator": "pdf:page=2", "text": "Invoice is payable."},),
    )

    assert result[0]["status"] == CoverageStatus.PARTIAL.value
    assert result[0]["matched"] == ("invoice",)
    assert result[0]["missing"] == ("30 days",)


def test_coverage_is_blocked_when_local_extraction_reported_a_blocker() -> None:
    result = assess_coverage(
        (Requirement("payment", ("invoice",)),),
        (),
        blockers=("broken.pdf: unreadable PDF",),
    )

    assert result == (
        {
            "requirement": "payment",
            "status": CoverageStatus.BLOCKED.value,
            "matched": (),
            "missing": ("invoice",),
            "evidence": (),
            "blockers": ("broken.pdf: unreadable PDF",),
        },
    )
