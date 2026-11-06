"""Local, deterministic composition for approved offer and invoice documents.

Composition accepts only the fields in the approved specifications.  A missing optional
value becomes ``[UNKNOWN]`` so callers cannot silently invent commercial facts.  The
module renders plain text deliberately; a downstream approved renderer can use the
structured ``fields`` and ``lines`` without changing the business contract.
"""
from __future__ import annotations

import csv
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


_UNKNOWN = "[UNKNOWN]"
_OFFER_REQUIRED = ("number", "issue_date", "seller", "buyer", "currency", "valid_until", "items")
_OFFER_OPTIONAL = ("payment_terms",)
_INVOICE_REQUIRED = (
    "number", "issue_date", "seller", "buyer", "currency", "due_date", "total_amount", "items"
)
_INVOICE_OPTIONAL = ("payment_terms",)
_LINE_FIELDS = ("description", "quantity", "unit_price")


class DocumentValidationError(ValueError):
    """Raised when input does not conform to an approved document specification."""


def compose_offer(fields: Mapping[str, Any]) -> dict[str, object]:
    """Compose a structured offer from the approved offer-v1 specification."""
    normalized, lines = _validate_fields(fields, _OFFER_REQUIRED, _OFFER_OPTIONAL)
    content = "\n".join(
        (
            f"Offer {normalized['number']}",
            f"Issue date: {normalized['issue_date']}",
            f"Seller: {normalized['seller']}",
            f"Buyer: {normalized['buyer']}",
            f"Currency: {normalized['currency']}",
            f"Valid until: {normalized['valid_until']}",
            f"Payment terms: {normalized['payment_terms']}",
            *_render_lines(lines),
        )
    )
    return {"template": "approved-offer-v1", "fields": normalized, "lines": lines, "content": content}


def compose_invoice(fields: Mapping[str, Any]) -> dict[str, object]:
    """Compose a structured invoice from the approved invoice-v1 specification."""
    normalized, lines = _validate_fields(fields, _INVOICE_REQUIRED, _INVOICE_OPTIONAL)
    content = "\n".join(
        (
            f"Invoice {normalized['number']}",
            f"Issue date: {normalized['issue_date']}",
            f"Seller: {normalized['seller']}",
            f"Buyer: {normalized['buyer']}",
            f"Currency: {normalized['currency']}",
            f"Due date: {normalized['due_date']}",
            f"Payment terms: {normalized['payment_terms']}",
            *_render_lines(lines),
            f"Total amount: {normalized['total_amount']} {normalized['currency']}",
        )
    )
    return {"template": "approved-invoice-v1", "fields": normalized, "lines": lines, "content": content}


def write_quality_reports(records: Sequence[Mapping[str, str]], output_dir: Path | str) -> dict[str, Path]:
    """Write matching deterministic JSON and CSV quality reports for evaluation records."""
    results = tuple(_quality_result(record) for record in records)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / "quality-report.json"
    csv_path = destination / "quality-report.csv"
    passed = sum(result["passed"] for result in results)
    report = {
        "summary": {"total": len(results), "passed": passed, "failed": len(results) - passed},
        "results": results,
    }
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=("case_id", "expected_status", "actual_status", "passed"))
        writer.writeheader()
        for result in results:
            writer.writerow({**result, "passed": str(result["passed"]).lower()})
    return {"json": json_path, "csv": csv_path}


def synthetic_evaluation_corpus() -> tuple[dict[str, str], ...]:
    """Return the checked-in, fictional offer/invoice evaluation cases in stable order."""
    corpus_path = Path(__file__).with_name("synthetic_evaluation_corpus.json")
    data = json.loads(corpus_path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(case, dict) for case in data):
        raise ValueError("synthetic corpus must be a JSON array of objects")
    return tuple({str(key): str(value) for key, value in case.items()} for case in data)


def _validate_fields(
    fields: Mapping[str, Any], required: tuple[str, ...], optional: tuple[str, ...]
) -> tuple[dict[str, str], tuple[dict[str, str], ...]]:
    if not isinstance(fields, Mapping):
        raise DocumentValidationError("fields must be a mapping")
    missing = tuple(name for name in required if name not in fields)
    if missing:
        raise DocumentValidationError(f"missing required fields: {', '.join(missing)}")
    allowed = set(required + optional)
    unapproved = tuple(sorted(str(name) for name in fields if name not in allowed))
    if unapproved:
        raise DocumentValidationError(f"unapproved fields: {', '.join(unapproved)}")
    normalized = {name: _required_value(name, fields[name]) for name in required if name != "items"}
    normalized.update({name: _optional_value(fields.get(name)) for name in optional})
    return normalized, _validate_lines(fields["items"])


def _required_value(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DocumentValidationError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_value(value: Any) -> str:
    if value is None or value == "":
        return _UNKNOWN
    if not isinstance(value, str):
        raise DocumentValidationError("optional values must be strings or None")
    return value.strip() or _UNKNOWN


def _validate_lines(items: Any) -> tuple[dict[str, str], ...]:
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)) or not items:
        raise DocumentValidationError("items must be a non-empty sequence")
    output: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, Mapping) or set(item) != set(_LINE_FIELDS):
            raise DocumentValidationError("each item must contain only description, quantity, unit_price")
        output.append({name: _required_value(name, item[name]) for name in _LINE_FIELDS})
    return tuple(output)


def _render_lines(lines: Sequence[Mapping[str, str]]) -> tuple[str, ...]:
    return tuple(
        f"Item {number}: {line['description']} | quantity: {line['quantity']} | unit price: {line['unit_price']}"
        for number, line in enumerate(lines, start=1)
    )


def _quality_result(record: Mapping[str, str]) -> dict[str, object]:
    required = ("case_id", "expected_status", "actual_status")
    if not isinstance(record, Mapping) or any(not isinstance(record.get(field), str) or not record[field] for field in required):
        raise ValueError("each quality record needs non-empty case_id, expected_status, actual_status")
    return {
        "case_id": record["case_id"],
        "expected_status": record["expected_status"],
        "actual_status": record["actual_status"],
        "passed": record["expected_status"] == record["actual_status"],
    }
