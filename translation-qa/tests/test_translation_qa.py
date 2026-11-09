"""Tests for deterministic, local translation delivery checks."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from translation_qa import QAStatus, validate_translation  # noqa: E402


def test_translation_passes_when_tokens_numbers_dates_and_artifacts_are_preserved(tmp_path: Path) -> None:
    source = "Send <PERSON_1> invoice {{INV_42}} for 1,250.00 EUR by 31.12.2026."
    translated = "Отправьте <PERSON_1> счёт {{INV_42}} на 1,250.00 EUR до 31.12.2026."
    docx, pdf = tmp_path / "translated.docx", tmp_path / "translated.pdf"
    docx.write_bytes(b"docx")
    pdf.write_bytes(b"pdf")

    result = validate_translation(source, translated, required_artifacts=(docx, pdf))

    assert result == {
        "status": QAStatus.PASS.value,
        "missing_tokens": (),
        "missing_numbers": (),
        "missing_dates": (),
        "missing_artifacts": (),
    }


def test_translation_reports_each_missing_preserved_value_and_missing_artifact(tmp_path: Path) -> None:
    source = "<PERSON_1> pays 1,250.00 on 2026-12-31 under {{CONTRACT_7}}."
    translated = "Лицо платит 1250 31 декабря 2026 года по договору."
    existing = tmp_path / "translated.docx"
    existing.write_bytes(b"docx")
    missing = tmp_path / "translated.pdf"

    result = validate_translation(source, translated, required_artifacts=(existing, missing))

    assert result == {
        "status": QAStatus.FAIL.value,
        "missing_tokens": ("<PERSON_1>", "{{CONTRACT_7}}"),
        "missing_numbers": ("1,250.00",),
        "missing_dates": ("2026-12-31",),
        "missing_artifacts": (str(missing),),
    }


def test_translation_fails_when_a_repeated_number_is_not_preserved_enough_times() -> None:
    source = "Pay 30 days after 30 days."
    translated = "Оплатить через 30 дней."

    result = validate_translation(source, translated)

    assert result["status"] == QAStatus.FAIL.value
    assert result["missing_numbers"] == ("30",)
