#!/usr/bin/env python
"""Secret-free QA acceptance harness for the document-quality platform.

Default mode is fully deterministic: synthetic browser/session and job fixtures,
plus compose contract checks. Live health checks are opt-in and accept only known
unauthenticated internal health endpoints.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse
from urllib.request import urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
SAFE_HEALTH_ENDPOINTS = {
    "doc-translate": "/translate/health",
    "quality-tools": "/health",
    "upstream-keypool": "/health",
    "ocr-ru": "/health",
    "litellm": "/health/liveliness",
}
INTERNAL_ONLY = {"quality-tools", "upstream-keypool"}
PUBLISHED_SERVICES = {"nginx"}
_LOCAL_TRANSLATE_RE = re.compile(
    r"(?i)(?:^|[\\/\s])(?:local[_-]?translate|translate[_-]?document[_-]?local|docx[_-]?translate)(?:\.py)?(?:\s|$)"
)


@dataclass
class SyntheticBrowserSession:
    """Minimal browser/session double used to acceptance-test chat UX contracts."""

    user: str
    _conversations: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    _next_id: int = 1
    _pending: set[str] = field(default_factory=set)

    def create_chat(self) -> str:
        conversation = f"chat-{self._next_id}"
        self._next_id += 1
        self._conversations[conversation] = []
        return conversation

    def send(self, conversation: str, prompt: str, *, reply: str) -> str:
        self._require_conversation(conversation)
        if conversation in self._pending:
            raise RuntimeError("response must be completed or aborted before continuing")
        self._conversations[conversation].extend((("user", prompt), ("assistant", reply)))
        return reply

    def begin_response(self, conversation: str, partial_reply: str) -> None:
        self._require_conversation(conversation)
        if conversation in self._pending:
            raise RuntimeError("response already pending")
        self._conversations[conversation].append(("assistant", partial_reply))
        self._pending.add(conversation)

    def abort_response(self, conversation: str) -> None:
        self._require_conversation(conversation)
        if conversation not in self._pending:
            raise RuntimeError("no response to abort")
        self._conversations[conversation][-1] = ("assistant", "[aborted]")
        self._pending.remove(conversation)

    def messages(self, conversation: str) -> list[tuple[str, str]]:
        self._require_conversation(conversation)
        return list(self._conversations[conversation])

    def _require_conversation(self, conversation: str) -> None:
        if conversation not in self._conversations:
            raise KeyError("unknown conversation")


@dataclass
class _Job:
    owner: str
    name: str
    target: str
    status: str = "queued"
    done: int = 0
    total: int = 0
    notes: list[str] = field(default_factory=list)
    files: tuple[str, ...] = ()


@dataclass
class SyntheticDocumentTranslation:
    """Async document-job double matching the public bridge status contract."""

    _jobs: dict[str, _Job] = field(default_factory=dict)
    _next_id: int = 1

    def start(self, owner: str, name: str, target: str) -> str:
        job_id = f"{self._next_id:032x}"
        self._next_id += 1
        self._jobs[job_id] = _Job(owner=owner, name=name, target=target)
        return job_id

    def progress(self, job_id: str, *, done: int, total: int) -> None:
        job = self._jobs[job_id]
        if total <= 0 or done < 0 or done > total:
            raise ValueError("progress must satisfy 0 <= done <= total")
        job.status, job.done, job.total = "running", done, total

    def complete_with_warnings(self, job_id: str, *, notes: Iterable[str] = ()) -> None:
        job = self._jobs[job_id]
        job.status = "completed_with_warnings"
        job.notes = list(notes)
        job.files = ("docx", "qa_report")

    def status(self, owner: str, job_id: str) -> dict | None:
        job = self._jobs.get(job_id)
        if job is None or job.owner != owner:
            return None
        return {
            "job_id": job_id,
            "name": job.name,
            "status": job.status,
            "progress": {"done": job.done, "total": job.total},
            "notes": list(job.notes),
            "artifacts": [
                {"kind": kind, "url": f"/translate/api/jobs/{job_id}/{kind}"} for kind in job.files
            ],
        }

    def download(self, owner: str, job_id: str, kind: str) -> str | None:
        job = self._jobs.get(job_id)
        if job is None or job.owner != owner or kind not in job.files:
            return None
        return f"/translate/api/jobs/{job_id}/{kind}"


def find_stuck_local_translate_processes(command_lines: Iterable[str]) -> list[str]:
    """Identify prohibited local translation workers without killing any process."""
    return [line for line in command_lines if _LOCAL_TRANSLATE_RE.search(line)]


def platform_contract(repo_root: Path = REPO_ROOT) -> dict[str, object]:
    """Read compose as a lightweight contract; no Docker daemon is required."""
    compose = (repo_root / "docker-compose.yml").read_text(encoding="utf-8")
    service_blocks = dict(re.findall(r"^  ([\w-]+):\r?\n(.*?)(?=^  [\w-]+:|^volumes:|^networks:|\Z)", compose, re.M | re.S))
    published = {name for name, block in service_blocks.items() if re.search(r"^    ports:", block, re.M)}
    missing = set(SAFE_HEALTH_ENDPOINTS) - set(service_blocks)
    if missing:
        raise ValueError(f"compose is missing expected services: {', '.join(sorted(missing))}")
    return {
        "safe_health_endpoints": dict(SAFE_HEALTH_ENDPOINTS),
        "internal_only": {name for name in INTERNAL_ONLY if name not in published},
        "published_services": published,
    }


def parse_live_health_targets(assignments: Iterable[str]) -> dict[str, str]:
    """Accept only explicit URLs for known, read-only health endpoints."""
    parsed: dict[str, str] = {}
    for assignment in assignments:
        if "=" not in assignment:
            raise ValueError("live health target must use SERVICE=URL")
        service, url = assignment.split("=", 1)
        expected_path = SAFE_HEALTH_ENDPOINTS.get(service)
        parts = urlparse(url)
        if not expected_path or parts.scheme not in {"http", "https"} or not parts.netloc or parts.path != expected_path:
            raise ValueError(f"invalid safe health target: {assignment}")
        parsed[service] = url
    return parsed


def check_live_health(targets: dict[str, str], *, timeout: float = 5.0) -> list[str]:
    """GET explicitly provided safe endpoints; never sends credentials or request bodies."""
    failures: list[str] = []
    for service, url in targets.items():
        try:
            with urlopen(url, timeout=timeout) as response:  # noqa: S310 -- URL validated above.
                if not 200 <= response.status < 400:
                    failures.append(f"{service}: HTTP {response.status}")
        except OSError as error:
            failures.append(f"{service}: {error}")
    return failures


def _host_process_lines() -> list[str]:
    """Read process command lines without mutating the host; supports deployment Linux hosts."""
    try:
        result = subprocess.run(["ps", "-eo", "args="], text=True, capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError("process audit requires a host with `ps -eo args=` or --processes-file") from error
    return [line for line in result.stdout.splitlines() if line.strip()]


def component_python(cwd: Path) -> Path:
    """Prefer a checked-in service virtual environment when the service has one."""
    for relative in (Path(".venv/Scripts/python.exe"), Path(".venv/bin/python")):
        candidate = cwd / relative
        if candidate.is_file():
            return candidate
    return Path(sys.executable)


def _run_pytest(paths: list[str], *, cwd: Path = REPO_ROOT) -> int:
    return subprocess.run([str(component_python(cwd)), "-m", "pytest", "-q", *paths], cwd=cwd).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit", action="store_true", help="run deterministic synthetic acceptance tests")
    parser.add_argument("--components", action="store_true", help="run relevant component contract tests")
    parser.add_argument("--live-health", nargs="*", metavar="SERVICE=URL", default=None,
                        help="opt-in GET checks for known unauthenticated health endpoints")
    parser.add_argument("--audit-processes", action="store_true", help="fail if prohibited local translate workers run")
    parser.add_argument("--processes-file", type=Path, help="newline-delimited process list (for deterministic audits)")
    args = parser.parse_args(argv)

    run_default = not any((args.unit, args.components, args.live_health is not None, args.audit_processes, args.processes_file))
    if run_default or args.unit:
        if _run_pytest(["qa-acceptance/tests"]) != 0:
            return 1
    if args.components:
        suites = [
            (REPO_ROOT / "agents", ["tests/test_translate_document_bridge.py"]),
            (REPO_ROOT / "agents/router", ["tests/test_document_translate_bridge.py"]),
            (REPO_ROOT / "keypool", ["tests/test_pool.py"]),
            (REPO_ROOT / "quality-tools", ["tests/test_quality_tools_api.py"]),
            (REPO_ROOT / "doc-translate", ["tests/test_doc_translate.py"]),
        ]
        for cwd, paths in suites:
            if _run_pytest(paths, cwd=cwd) != 0:
                return 1
    if args.live_health is not None:
        try:
            targets = parse_live_health_targets(args.live_health)
        except ValueError as error:
            parser.error(str(error))
        failures = check_live_health(targets)
        if failures:
            print("LIVE HEALTH FAILED:\n" + "\n".join(failures), file=sys.stderr)
            return 1
        print("LIVE HEALTH PASSED: " + ", ".join(sorted(targets)))
    if args.audit_processes or args.processes_file:
        try:
            lines = args.processes_file.read_text(encoding="utf-8").splitlines() if args.processes_file else _host_process_lines()
        except (OSError, RuntimeError) as error:
            print(f"PROCESS AUDIT FAILED: {error}", file=sys.stderr)
            return 1
        stuck = find_stuck_local_translate_processes(lines)
        if stuck:
            print("STUCK LOCAL TRANSLATE PROCESS(ES):\n" + "\n".join(stuck), file=sys.stderr)
            return 1
        print("PROCESS AUDIT PASSED: no prohibited local translate workers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
