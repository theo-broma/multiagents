#!/usr/bin/env bash
set -euo pipefail

# Ensure consistent sorting across locales
export LC_ALL=C

# Resolve repository root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

list_only=0
if [ "${1:-}" = "--list" ]; then
    list_only=1
    shift
fi

if [ $# -lt 2 ]; then
    echo "Usage: $0 [--list] K N [extra pytest args...]" >&2
    exit 2
fi

K="$1"
N="$2"
shift 2
extra_args=("$@")

if ! [[ "$K" =~ ^[0-9]+$ ]] || ! [[ "$N" =~ ^[0-9]+$ ]] || [ "$K" -lt 1 ] || [ "$N" -lt 1 ] || [ "$K" -gt "$N" ]; then
    echo "Error: K and N must be positive integers with 1 <= K <= N (got K=$K, N=$N)" >&2
    exit 2
fi

shopt -s nullglob
all_files=(tests/test_*.py)

chunk_files=()
for (( idx = K - 1; idx < ${#all_files[@]}; idx += N )); do
    chunk_files+=("${all_files[idx]}")
done

if [ "$list_only" -eq 1 ]; then
    if [ "${#chunk_files[@]}" -gt 0 ]; then
        printf '%s\n' "${chunk_files[@]}"
    fi
    exit 0
fi

# Print which files are run first
if [ "${#chunk_files[@]}" -gt 0 ]; then
    printf '%s\n' "${chunk_files[@]}"
fi

if [ -d .venv ]; then
    export PYTHONPATH=src
    exec .venv/bin/python -m pytest "${chunk_files[@]}" "${extra_args[@]}"
else
    exec uv run --frozen python -m pytest "${chunk_files[@]}" "${extra_args[@]}"
fi
