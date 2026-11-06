"""Contract tests for the internal-only local quality-tools REST service."""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app  # noqa: E402


def client(tmp_path: Path) -> TestClient:
    uploads, work = tmp_path / "uploads", tmp_path / "work"
    uploads.mkdir()
    work.mkdir()
    return TestClient(create_app(uploads_dir=uploads, work_dir=work))


def test_health_and_all_quality_operations_are_local_and_typed(tmp_path: Path) -> None:
    api = client(tmp_path)
    uploads = tmp_path / "uploads"
    (uploads / "contract.docx").write_bytes(b"not read in this contract test")

    assert api.get("/health").json() == {"status": "ok", "service": "quality-tools"}
    inventory = api.post("/v1/evidence/inventory", json={"root": "uploads", "path": "."})
    assert inventory.status_code == 200
    assert inventory.json() == [{"path": "contract.docx", "type": "docx", "size_bytes": 30}]

    calculation = api.post(
        "/v1/calculator/invoice",
        json={
            "quantity": "3",
            "unit_price": "19.99",
            "discount_percent": "10",
            "vat_percent": "20",
            "currency": "EUR",
        },
    )
    assert calculation.status_code == 200
    assert calculation.json()["total_amount"] == "64.76"

    translation = api.post(
        "/v1/translation-qa",
        json={"source_text": "Pay <PERSON_1> 30 by 2026-12-31.", "translated_text": "Оплатить <PERSON_1> 30 до 2026-12-31."},
    )
    assert translation.status_code == 200
    assert translation.json()["status"] == "PASS"

    anonymization = api.post(
        "/v1/anonymization-audit",
        json={
            "text": "Contact <EMAIL_1>.",
            "required_patterns": [{"identifier": "email", "pattern": "[A-Za-z]+@[A-Za-z]+\\.[A-Za-z]+"}],
        },
    )
    assert anonymization.status_code == 200
    assert anonymization.json()["status"] == "PASS"

    offer = api.post(
        "/v1/document-compose/offer",
        json={
            "fields": {
                "number": "OF-1", "issue_date": "2026-11-06", "seller": "Northwind", "buyer": "Contoso",
                "currency": "EUR", "valid_until": "2026-12-06",
                "items": [{"description": "Implementation", "quantity": "1", "unit_price": "1000.00"}],
            }
        },
    )
    assert offer.status_code == 200
    assert offer.json()["template"] == "approved-offer-v1"

    reports = api.post(
        "/v1/document-compose/reports",
        json={"output_dir": "run-1", "records": [{"case_id": "one", "expected_status": "PASS", "actual_status": "PASS"}]},
    )
    assert reports.status_code == 200
    assert reports.json() == {"json": "work/run-1/quality-report.json", "csv": "work/run-1/quality-report.csv"}
    assert (tmp_path / "work" / "run-1" / "quality-report.json").is_file()


def test_user_supplied_paths_cannot_escape_the_mounted_uploads_or_work_roots(tmp_path: Path) -> None:
    api = client(tmp_path)

    absolute = api.post("/v1/evidence/inventory", json={"root": "uploads", "path": "/etc"})
    traversal = api.post("/v1/evidence/inventory", json={"root": "work", "path": "../uploads"})
    report_escape = api.post(
        "/v1/document-compose/reports",
        json={"output_dir": "../outside", "records": []},
    )

    assert absolute.status_code == 400
    assert traversal.status_code == 400
    assert report_escape.status_code == 400
