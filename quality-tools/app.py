"""Internal-only, local REST adapter for deterministic document quality libraries.

The service deliberately has no authentication, secrets, outbound HTTP clients, or
published port. Compose confines it to the platform network. Every user-supplied file
path is a relative path resolved beneath the only two mounted roots: /uploads and /work.
"""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
import os
from pathlib import Path
import sys
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

_REPO = Path(__file__).resolve().parent.parent
for _library in ("document-evidence", "translation-qa", "anonymization-audit", "document-compose"):
    sys.path.insert(0, str(_REPO / _library))
sys.path.insert(0, str(_REPO))

from anonymization_audit import RequiredPattern, audit_anonymization
from calculator import calculate_invoice
from document_compose import compose_invoice, compose_offer, write_quality_reports
from document_evidence import Requirement, assess_coverage, canonical_records, inventory
from translation_qa import validate_translation


class FileLocation(BaseModel):
    root: Literal["uploads", "work"]
    path: str = "."


class EvidenceRecordsRequest(BaseModel):
    root: Literal["uploads", "work"]
    paths: list[str] = Field(min_length=1)


class RequirementInput(BaseModel):
    identifier: str
    phrases: list[str] = Field(min_length=1)


class CoverageRequest(BaseModel):
    requirements: list[RequirementInput]
    records: list[dict[str, str]]
    blockers: list[str] = Field(default_factory=list)


class InvoiceRequest(BaseModel):
    quantity: Decimal
    unit_price: Decimal
    discount_percent: Decimal
    vat_percent: Decimal
    currency: str


class TranslationQARequest(BaseModel):
    source_text: str
    translated_text: str
    required_artifacts: list[FileLocation] = Field(default_factory=list)


class PatternInput(BaseModel):
    identifier: str
    pattern: str


class AnonymizationRequest(BaseModel):
    text: str
    required_patterns: list[PatternInput]


class ComposeRequest(BaseModel):
    fields: dict[str, Any]


class ReportRequest(BaseModel):
    output_dir: str
    records: list[dict[str, str]]


def create_app(*, uploads_dir: Path | str = "/uploads", work_dir: Path | str = "/work") -> FastAPI:
    """Create the service with explicit roots, allowing isolated filesystem tests."""
    roots = {"uploads": Path(uploads_dir).resolve(), "work": Path(work_dir).resolve()}
    app = FastAPI(title="quality-tools", version="1.0.0", docs_url=None, redoc_url=None)

    def safe_path(root_name: Literal["uploads", "work"], user_path: str) -> Path:
        if not isinstance(user_path, str) or not user_path:
            raise HTTPException(status_code=400, detail="path must be a non-empty relative path")
        candidate = Path(user_path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise HTTPException(status_code=400, detail="path must remain beneath its mounted root")
        resolved = (roots[root_name] / candidate).resolve()
        try:
            resolved.relative_to(roots[root_name])
        except ValueError as error:
            raise HTTPException(status_code=400, detail="path must remain beneath its mounted root") from error
        return resolved

    def work_relative(path: Path) -> str:
        return f"work/{path.resolve().relative_to(roots['work']).as_posix()}"

    @app.exception_handler(ValueError)
    async def invalid_input(_request: object, exc: ValueError):
        return __import__("fastapi").responses.JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "quality-tools"}

    @app.post("/v1/evidence/inventory")
    def evidence_inventory(request: FileLocation) -> tuple[dict[str, object], ...]:
        return inventory(safe_path(request.root, request.path))

    @app.post("/v1/evidence/records")
    def evidence_records(request: EvidenceRecordsRequest) -> tuple[dict[str, str], ...]:
        root = roots[request.root]
        return canonical_records((safe_path(request.root, path) for path in request.paths), root=root)

    @app.post("/v1/evidence/coverage")
    def evidence_coverage(request: CoverageRequest) -> tuple[dict[str, object], ...]:
        requirements = tuple(Requirement(item.identifier, tuple(item.phrases)) for item in request.requirements)
        return assess_coverage(requirements, request.records, blockers=tuple(request.blockers))

    @app.post("/v1/calculator/invoice")
    def invoice_calculation(request: InvoiceRequest) -> dict[str, str]:
        return {name: str(value) for name, value in asdict(calculate_invoice(**request.model_dump())).items()}

    @app.post("/v1/translation-qa")
    def translation_quality(request: TranslationQARequest) -> dict[str, object]:
        artifacts = tuple(safe_path(item.root, item.path) for item in request.required_artifacts)
        return validate_translation(request.source_text, request.translated_text, required_artifacts=artifacts)

    @app.post("/v1/anonymization-audit")
    def anonymization_audit(request: AnonymizationRequest) -> dict[str, object]:
        patterns = tuple(RequiredPattern(item.identifier, item.pattern) for item in request.required_patterns)
        return audit_anonymization(request.text, required_patterns=patterns)

    @app.post("/v1/document-compose/offer")
    def offer_composition(request: ComposeRequest) -> dict[str, object]:
        return compose_offer(request.fields)

    @app.post("/v1/document-compose/invoice")
    def invoice_composition(request: ComposeRequest) -> dict[str, object]:
        return compose_invoice(request.fields)

    @app.post("/v1/document-compose/reports")
    def quality_reports(request: ReportRequest) -> dict[str, str]:
        destination = safe_path("work", request.output_dir)
        return {name: work_relative(path) for name, path in write_quality_reports(request.records, destination).items()}

    return app


app = create_app(
    uploads_dir=os.getenv("QUALITY_UPLOADS_DIR", "/uploads"),
    work_dir=os.getenv("QUALITY_WORK_DIR", "/work"),
)
