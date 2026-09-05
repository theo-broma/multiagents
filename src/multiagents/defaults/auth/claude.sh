#!/bin/sh
# Claude Code. Has a first-class auth surface, so this is thin.
set -u
BIN="${MULTIAGENTS_BIN:-claude}"

case "${1:-check}" in
check)
    out=$("$BIN" auth status --json 2>/dev/null) || {
        echo "could not run '$BIN auth status'"; exit 20; }
    case "$out" in
        *'"loggedIn": true'*|*'"loggedIn":true'*)
            email=$(printf '%s' "$out" | sed -n 's/.*"email"[ ]*:[ ]*"\([^"]*\)".*/\1/p')
            echo "logged in${email:+ as $email}"; exit 0 ;;
        *)  echo "not logged in"; exit 10 ;;
    esac
    ;;
login)
    echo "Claude Code sign-in."
    echo "A browser window will open; complete the sign-in there."
    echo
    exec "$BIN" auth login
    ;;
*)  echo "usage: $0 check|login" >&2; exit 64 ;;
esac
