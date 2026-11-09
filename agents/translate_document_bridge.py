#!/usr/bin/env python3
"""Bounded agent-side client for asynchronous document translation jobs.

Normal translation never executes a local model.  It asks the authenticated
agents-router bridge to create a job and polls it only a small, bounded number
of times.  The browser-facing links are session-protected doc-translate links.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


POLL_ATTEMPTS = int(os.getenv("DOCUMENT_TRANSLATE_POLL_ATTEMPTS", "3"))
POLL_SECONDS = float(os.getenv("DOCUMENT_TRANSLATE_POLL_SECONDS", "1"))
REQUEST_TIMEOUT = float(os.getenv("DOCUMENT_TRANSLATE_REQUEST_TIMEOUT", "8"))


class UsageError(ValueError):
    pass


def remote_request(argv: list[str]) -> tuple[str, str]:
    """Parse the intentionally tiny remote API: uploads/<filename> --to <lang>."""
    if len(argv) != 3 or argv[1] != "--to" or not argv[2].strip():
        raise UsageError("Используйте: translate-document uploads/<имя-файла> --to ru|en|zh")
    path = PurePosixPath(argv[0])
    if path.is_absolute() or len(path.parts) != 2 or path.parts[0] != "uploads" or path.name in ("", ".", ".."):
        raise UsageError("Для перевода укажите только вложение вида uploads/<имя-файла>")
    return path.name, argv[2].lower()


def local_command(argv: list[str]) -> list[str] | None:
    """Allow legacy extract/apply only when a separately configured local helper exists.

    Those modes are non-model preparation/rendering steps.  A regular `--to`
    invocation is never delegated locally, so it cannot start a long model run
    inside Hermes.
    """
    extract, apply = "--extract" in argv, "--apply" in argv
    if not extract and not apply:
        return None
    if extract == apply:
        raise UsageError("Укажите ровно один безопасный локальный режим: --extract или --apply")
    helper = os.getenv("TRANSLATE_DOCUMENT_LOCAL", "")
    if not helper:
        return None
    if not os.path.isabs(helper):
        raise UsageError("TRANSLATE_DOCUMENT_LOCAL должен быть абсолютным путём")
    if extract:
        valid = (len(argv) == 6 and argv[1] == "--to" and bool(argv[2].strip()) and argv[3:] == ["--extract", "--out", "work"])
        source = PurePosixPath(argv[0])
        valid = valid and not source.is_absolute() and len(source.parts) == 2 and source.parts[0] == "uploads"
    else:
        valid = len(argv) == 4 and argv[1:] == ["--apply", "--out", "outbox"] and argv[0].startswith("work/")
    if not valid:
        raise UsageError("Локальные режимы разрешены только в безопасных формах --extract … --out work или --apply --out outbox")
    return [helper, *argv]


class RouterClient:
    def __init__(self) -> None:
        self.base = os.getenv("DOCUMENT_TRANSLATE_BASE", "").rstrip("/")
        self.key = os.getenv("DOCUMENT_TRANSLATE_API_KEY", "")
        if not self.base or not self.key:
            raise UsageError("Мост перевода документов не настроен")

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = Request(f"{self.base}{path}", data=data, method=method, headers={
            "Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
        })
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                parsed = json.loads(response.read())
        except HTTPError as error:
            detail = error.read().decode(errors="replace")[:500]
            raise UsageError(f"Мост перевода вернул HTTP {error.code}: {detail}") from error
        except (URLError, TimeoutError) as error:
            raise UsageError("Мост перевода недоступен или не ответил вовремя") from error
        if not isinstance(parsed, dict):
            raise UsageError("Мост перевода вернул неверный ответ")
        return parsed

    def start(self, filename: str, target: str) -> dict:
        return self._request("POST", "/jobs", {"filename": filename, "target": target})

    def status(self, job_id: str) -> dict:
        return self._request("GET", f"/jobs/{job_id}")


def _print_result(job: dict) -> None:
    status = job.get("status")
    if status == "completed_with_warnings":
        print("Перевод выдан с ПРЕДУПРЕЖДЕНИЕМ; сверьте с оригиналом и отчётом QA.")
    else:
        print("Перевод готов.")
    for artifact in job.get("artifacts", []):
        if isinstance(artifact, dict) and isinstance(artifact.get("url"), str):
            print(f"- {artifact.get('kind', 'файл')}: {artifact['url']}")
    for note in job.get("notes", []):
        if isinstance(note, str):
            print(f"- замечание: {note}")


def translate(argv: list[str], router, *, poll_attempts: int = POLL_ATTEMPTS, poll_seconds: float = POLL_SECONDS) -> int:
    filename, target = remote_request(argv)
    created = router.start(filename, target)
    job_id = created.get("job_id")
    if not isinstance(job_id, str):
        raise UsageError("Мост перевода не вернул идентификатор задания")
    job = {"status": "pending", "progress_url": created.get("progress_url", "")}
    for attempt in range(max(0, poll_attempts)):
        if attempt and poll_seconds:
            time.sleep(poll_seconds)
        job = router.status(job_id)
        if job.get("status") in ("completed", "completed_with_warnings", "failed"):
            break
    if job.get("status") in ("completed", "completed_with_warnings"):
        _print_result(job)
        return 0
    if job.get("status") == "failed":
        print("Перевод не завершён: " + str(job.get("error", "ошибка")), file=sys.stderr)
        return 1
    progress = job.get("progress") if isinstance(job.get("progress"), dict) else {}
    print(f"Перевод «{filename}» ещё выполняется ({progress.get('done', 0)} из {progress.get('total', '…')}).")
    print("Прогресс и скачивание: " + str(job.get("progress_url") or created.get("progress_url", "")))
    return 0


def main(argv: list[str]) -> int:
    try:
        command = local_command(argv)
        if command is not None:
            # This explicit configuration is the only escape hatch for the
            # existing non-model --extract/--apply workflow.
            return os.spawnv(os.P_WAIT, command[0], command)
        if "--extract" in argv or "--apply" in argv:
            raise UsageError("Локальный --extract/--apply не настроен; автоматический перевод в агенте запрещён")
        return translate(argv, RouterClient())
    except UsageError as error:
        print(f"translate-document: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
