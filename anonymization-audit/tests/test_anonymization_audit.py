"""Tests for deterministic, local anonymization checks."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anonymization_audit import AuditStatus, RequiredPattern, audit_anonymization  # noqa: E402


EMAIL = RequiredPattern("email", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = RequiredPattern("phone", r"\+7 \d{3} \d{3}-\d{2}-\d{2}")


def test_audit_passes_for_well_formed_placeholders_without_unmasked_patterns() -> None:
    result = audit_anonymization(
        "Contact <EMAIL_1> or <PHONE_1>.",
        required_patterns=(EMAIL, PHONE),
    )

    assert result == {
        "status": AuditStatus.PASS.value,
        "invalid_placeholders": (),
        "unmasked": (),
    }


def test_audit_detects_unmasked_required_patterns_in_stable_pattern_order() -> None:
    result = audit_anonymization(
        "Contact ivan@example.com or +7 999 123-45-67.",
        required_patterns=(EMAIL, PHONE),
    )

    assert result == {
        "status": AuditStatus.FAIL.value,
        "invalid_placeholders": (),
        "unmasked": (
            {"pattern": "email", "matches": ("ivan@example.com",)},
            {"pattern": "phone", "matches": ("+7 999 123-45-67",)},
        ),
    }


def test_audit_rejects_malformed_and_unknown_placeholders() -> None:
    result = audit_anonymization(
        "<EMAIL_0> <email_1> <PERSON_1 <UNKNOWN_2>",
        required_patterns=(EMAIL,),
    )

    assert result == {
        "status": AuditStatus.FAIL.value,
        "invalid_placeholders": ("<EMAIL_0>", "<email_1>", "<PERSON_1", "<UNKNOWN_2>"),
        "unmasked": (),
    }
