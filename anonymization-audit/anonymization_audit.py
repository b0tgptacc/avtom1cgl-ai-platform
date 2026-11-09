"""Deterministic, local-only audit of anonymized text.

Required patterns are regular expressions for values that must no longer appear.
Placeholders are accepted only as ``<KIND_positive-integer>`` where KIND is one
of the configured required-pattern identifiers in uppercase.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence
import re


class AuditStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


@dataclass(frozen=True)
class RequiredPattern:
    """Named regular expression whose matches must be masked."""

    identifier: str
    pattern: str


_PLACEHOLDER_CANDIDATE_RE = re.compile(r"<[^>\s]*(?:>|(?=\s|$))")
_VALID_PLACEHOLDER_RE = re.compile(r"<([A-Z][A-Z0-9]*)_([1-9]\d*)>")


def audit_anonymization(
    text: str,
    *,
    required_patterns: Sequence[RequiredPattern],
) -> dict[str, object]:
    """Return a stable report of malformed placeholders and values left unmasked."""
    allowed_kinds = _allowed_kinds(required_patterns)
    invalid_placeholders = tuple(
        candidate
        for candidate in _PLACEHOLDER_CANDIDATE_RE.findall(text)
        if not _is_valid_placeholder(candidate, allowed_kinds)
    )
    unmasked = tuple(
        {"pattern": requirement.identifier, "matches": tuple(re.findall(requirement.pattern, text))}
        for requirement in required_patterns
        if re.findall(requirement.pattern, text)
    )
    return {
        "status": AuditStatus.PASS.value if not invalid_placeholders and not unmasked else AuditStatus.FAIL.value,
        "invalid_placeholders": invalid_placeholders,
        "unmasked": unmasked,
    }


def _allowed_kinds(required_patterns: Sequence[RequiredPattern]) -> frozenset[str]:
    identifiers = tuple(requirement.identifier for requirement in required_patterns)
    if any(not identifier or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", identifier) for identifier in identifiers):
        raise ValueError("required pattern identifiers must be non-empty alphanumeric labels")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("required pattern identifiers must be unique")
    return frozenset(identifier.upper() for identifier in identifiers)


def _is_valid_placeholder(candidate: str, allowed_kinds: frozenset[str]) -> bool:
    match = _VALID_PLACEHOLDER_RE.fullmatch(candidate)
    return bool(match and match.group(1) in allowed_kinds)
