#!/usr/bin/env bash
# Run the secret-free QA acceptance harness from any working directory.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
python scripts/qa_acceptance.py "$@"
