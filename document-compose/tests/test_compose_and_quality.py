"""Tests for deterministic approved document composition and quality artifacts."""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from document_compose import (  # noqa: E402
    DocumentValidationError,
    compose_invoice,
    compose_offer,
    synthetic_evaluation_corpus,
    write_quality_reports,
)


def test_compose_offer_uses_only_approved_fields_and_marks_unknown_values() -> None:
    document = compose_offer(
        {
            "number": "OF-2026-001",
            "issue_date": "2026-11-06",
            "seller": "Northwind LLC",
            "buyer": "Contoso Ltd",
            "currency": "EUR",
            "valid_until": "2026-12-06",
            "items": ({"description": "Implementation", "quantity": "1", "unit_price": "1000.00"},),
            "payment_terms": None,
        }
    )

    assert document["template"] == "approved-offer-v1"
    assert document["fields"]["payment_terms"] == "[UNKNOWN]"
    assert document["lines"] == (
        {"description": "Implementation", "quantity": "1", "unit_price": "1000.00"},
    )
    assert "Offer OF-2026-001" in document["content"]
    assert "Payment terms: [UNKNOWN]" in document["content"]


def test_compose_invoice_rejects_missing_or_unapproved_fields() -> None:
    valid_fields = {
        "number": "INV-2026-002",
        "issue_date": "2026-11-06",
        "seller": "Northwind LLC",
        "buyer": "Contoso Ltd",
        "currency": "EUR",
        "due_date": "2026-12-06",
        "total_amount": "1200.00",
        "items": ({"description": "Implementation", "quantity": "1", "unit_price": "1000.00"},),
    }

    with pytest.raises(DocumentValidationError, match="missing required fields: due_date"):
        compose_invoice({key: value for key, value in valid_fields.items() if key != "due_date"})
    with pytest.raises(DocumentValidationError, match="unapproved fields: signature"):
        compose_invoice({**valid_fields, "signature": "invented"})


def test_quality_reports_write_deterministic_json_and_csv_with_summary(tmp_path: Path) -> None:
    records = (
        {"case_id": "offer-pass", "expected_status": "PASS", "actual_status": "PASS"},
        {"case_id": "invoice-missing-due-date", "expected_status": "BLOCKED", "actual_status": "PARTIAL"},
    )

    written = write_quality_reports(records, tmp_path)

    assert written == {"json": tmp_path / "quality-report.json", "csv": tmp_path / "quality-report.csv"}
    report = json.loads(written["json"].read_text(encoding="utf-8"))
    assert report["summary"] == {"total": 2, "passed": 1, "failed": 1}
    assert report["results"][1]["passed"] is False
    with written["csv"].open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows == [
        {"case_id": "offer-pass", "expected_status": "PASS", "actual_status": "PASS", "passed": "true"},
        {"case_id": "invoice-missing-due-date", "expected_status": "BLOCKED", "actual_status": "PARTIAL", "passed": "false"},
    ]


def test_synthetic_evaluation_corpus_contains_safe_offer_and_invoice_cases() -> None:
    corpus = synthetic_evaluation_corpus()

    assert {case["document_type"] for case in corpus} == {"offer", "invoice"}
    assert all(case["case_id"] and case["expected_status"] for case in corpus)
    serialized = json.dumps(corpus)
    assert "@" not in serialized
    assert "secret" not in serialized.casefold()
