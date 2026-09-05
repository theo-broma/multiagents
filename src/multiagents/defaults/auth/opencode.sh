#!/bin/sh
# opencode. Credentials live on the HOST even when agents run in a container,
# because the container mounts opencode's data directory rather than masking it.
set -u
BIN="${MULTIAGENTS_BIN:-opencode}"

case "${1:-check}" in
check)
    out=$("$BIN" providers list 2>/dev/null) || {
        echo "could not run '$BIN providers list'"; exit 20; }
    # "0 credentials" means no stored login. An API key in the environment is
    # reported separately and is not a subscription credential.
    case "$out" in
        *"0 credentials"*)
            echo "no stored credentials (free tier / env keys only)"; exit 10 ;;
        *)
            n=$(printf '%s' "$out" | sed -n 's/.*[^0-9]\([0-9][0-9]*\) credential.*/\1/p' | head -1)
            echo "${n:-1} stored credential(s)"; exit 0 ;;
    esac
    ;;
login)
    echo "opencode sign-in."
    echo "You will be asked to pick a provider, then a login method."
    echo "For an OpenCode Go subscription choose 'OpenCode' and follow the link."
    echo
    echo "Credentials are stored on the host (~/.local/share/opencode/auth.json)"
    echo "and are shared with the container, so this only has to be done once."
    echo
    exec "$BIN" providers login
    ;;
*)  echo "usage: $0 check|login" >&2; exit 64 ;;
esac
