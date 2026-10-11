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

# PS2-R6: pytest accepts repeated options; only its last basetemp is used.
basetemp=""
for (( idx = 0; idx < ${#extra_args[@]}; idx++ )); do
    case "${extra_args[idx]}" in
        --basetemp=*) basetemp="${extra_args[idx]#--basetemp=}" ;;
        --basetemp)
            basetemp=""
            if (( idx + 1 < ${#extra_args[@]} )); then
                idx=$((idx + 1))
                basetemp="${extra_args[idx]}"
            fi ;;
        --) break ;;
    esac
done

cleanup() {
    if [ -z "$basetemp" ]; then return; fi
    local resolved
    resolved="$(realpath -m -- "$basetemp")" || return
    if [ ! -e "$resolved" ]; then return; fi
    if [ -d "$resolved" ] && [ "$(dirname -- "$resolved")" = /var/tmp ] &&
       [[ "$(basename -- "$resolved")" = pt-* ]] &&
       [ ! -e "$resolved/.git" ] &&
       ! git -c safe.directory='*' -C "$resolved" rev-parse --git-dir >/dev/null 2>&1; then
        rm -rf -- "$resolved" || echo "Could not clean pytest basetemp: $resolved" >&2
    else
        echo "Keeping pytest basetemp: $resolved (outside cleanup scope)" >&2
    fi
}
trap cleanup EXIT
# Signal the direct child as well: signals sent just to this shell must
# still interrupt pytest. wait is retried by the handler before cleanup.
pytest_pid=""
interrupt() {
    local sig="$1" status="$2"
    trap '' INT TERM HUP
    if [ -n "$pytest_pid" ]; then
        kill -s "$sig" "$pytest_pid" 2>/dev/null || true
        wait "$pytest_pid" 2>/dev/null || true
    fi
    exit "$status"
}
trap 'interrupt INT 130' INT
trap 'interrupt TERM 143' TERM
trap 'interrupt HUP 129' HUP

# Bash background jobs inherit ignored INT/QUIT; restore them for pytest.
if [ -d .venv ]; then
    export PYTHONPATH=src
    env --default-signal=INT,QUIT .venv/bin/python -m pytest "${chunk_files[@]}" "${extra_args[@]}" &
else
    env --default-signal=INT,QUIT uv run --frozen python -m pytest "${chunk_files[@]}" "${extra_args[@]}" &
fi
pytest_pid=$!
status=0
wait "$pytest_pid" || status=$?
exit "$status"
