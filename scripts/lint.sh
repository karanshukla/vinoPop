#!/usr/bin/env bash
# The CI lint job, runnable locally and from the pre-commit hook.
set -euo pipefail
cd "$(dirname "$0")/.."

tool() {
    if [[ -x .venv/bin/$1 ]]; then
        echo ".venv/bin/$1"
    else
        echo "missing .venv/bin/$1: run 'uv pip install --group dev'" >&2
        exit 1
    fi
}

"$(tool ruff)" check .
"$(tool ruff)" format --check .
"$(tool mypy)"
