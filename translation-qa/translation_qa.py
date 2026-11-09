"""Deterministic, local-only checks for translated-document delivery.

The checker deliberately compares opaque tokens, numeric literals, and date literals
as multisets. It does not evaluate linguistic quality and makes no network calls.
"""
from __future__ import annotations

from collections import Counter
from enum import Enum
from pathlib import Path
from typing import Iterable
import re


class QAStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


_TOKEN_RE = re.compile(r"<[^<>\s]+>|\{\{[^{}\s]+\}\}|⟦/?\d+⟧")
_DATE_RE = re.compile(
    r"(?<!\d)(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4})(?!\d)"
)
_NUMBER_RE = re.compile(
    r"(?<![\w.,])[-+]?\d{1,3}(?:[ ,]\d{3})+(?:[.,]\d+)?(?![\w.,])"
    r"|(?<![\w.,])[-+]?\d+(?:[.,]\d+)?(?![\w.,])"
)


def validate_translation(
    source_text: str,
    translated_text: str,
    *,
    required_artifacts: Iterable[Path | str] = (),
) -> dict[str, object]:
    """Return a stable pass/fail report for literal preservation and artifacts.

    A value is missing when it occurs fewer times in the translation than in the
    source. Required artifacts must exist as regular files at audit time.
    """
    source_tokens = _TOKEN_RE.findall(source_text)
    translated_tokens = _TOKEN_RE.findall(translated_text)
    source_dates = _DATE_RE.findall(source_text)
    translated_dates = _DATE_RE.findall(translated_text)
    source_numbers = _numbers_without_tokens_and_dates(source_text)
    translated_numbers = _numbers_without_tokens_and_dates(translated_text)
    missing_artifacts = tuple(
        str(path) for path in required_artifacts if not Path(path).is_file()
    )
    result = {
        "missing_tokens": _missing_occurrences(source_tokens, translated_tokens),
        "missing_numbers": _missing_occurrences(source_numbers, translated_numbers),
        "missing_dates": _missing_occurrences(source_dates, translated_dates),
        "missing_artifacts": missing_artifacts,
    }
    return {
        "status": QAStatus.PASS.value if not any(result.values()) else QAStatus.FAIL.value,
        **result,
    }


def _numbers_without_tokens_and_dates(text: str) -> list[str]:
    scrubbed = _TOKEN_RE.sub(" ", text)
    scrubbed = _DATE_RE.sub(" ", scrubbed)
    return _NUMBER_RE.findall(scrubbed)


def _missing_occurrences(source: list[str], translated: list[str]) -> tuple[str, ...]:
    remaining = Counter(translated)
    missing: list[str] = []
    for value in source:
        if remaining[value]:
            remaining[value] -= 1
        else:
            missing.append(value)
    return tuple(missing)
