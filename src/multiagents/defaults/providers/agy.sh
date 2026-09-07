#!/bin/sh
# Antigravity (agy).
#
# The awkward one, and the reason this contract exists. agy has no auth
# subcommand, and on the host it keeps its credential in the GNOME keyring
# rather than in a file — so there is nothing to bind-mount into a container.
# (~/.gemini/oauth_creds.json exists but is a stale legacy artifact; ignore it.)
#
# Inside a container there is no keyring, so agy falls back to a FILE token at
# .gemini/antigravity-cli/antigravity-oauth-token. That is why the container
# needs a login of its own, and why `container_private_home` masks the host's
# ~/.gemini instead of sharing it.
set -u
BIN="${MULTIAGENTS_BIN:-agy}"
EXECUTOR="${MULTIAGENTS_EXECUTOR:-local}"
TOKEN_REL=".gemini/antigravity-cli/antigravity-oauth-token"

case "${1:-check}" in
check)
    if [ "$EXECUTOR" = "docker" ] && [ -n "${MULTIAGENTS_PRIVATE_BACKING:-}" ]; then
        # The container's own token is a plain file, so this is a free check.
        backing="${MULTIAGENTS_PRIVATE_BACKING%/.gemini}"
        if [ -s "$backing/$TOKEN_REL" ]; then
            echo "container token present ($backing/$TOKEN_REL)"; exit 0
        fi
        echo "no container token; agy has not been logged in inside the container"
        exit 10
    fi
    # On the host the credential is in the keyring and cannot be inspected, so
    # ask the CLI. It fails fast when unauthenticated, before spending anything.
    out=$("$BIN" -p "ok" --model gemini-3.8-flash-low --output-format json 2>&1) || true
    case "$out" in
        *"authentication required"*|*"authentication failed"*|*"log in"*)
            echo "not logged in on the host"; exit 10 ;;
        *'"status":"SUCCESS"'*)
            echo "logged in (host keyring)"; exit 0 ;;
        *)  echo "unclear: $(printf '%s' "$out" | head -c 120)"; exit 20 ;;
    esac
    ;;
login)
    if [ "$EXECUTOR" = "docker" ]; then
        container="${MULTIAGENTS_CONTAINER:?container name not provided}"
        echo "Antigravity sign-in, INSIDE the container."
        echo
        echo "The host keeps its credential in the GNOME keyring, which a container"
        echo "cannot reach, so the container needs a login of its own. It will be"
        echo "stored as a file in:"
        echo "  ${MULTIAGENTS_PRIVATE_BACKING:-<container-private .gemini>}"
        echo "Your host credentials are masked and cannot be touched."
        echo
        echo "What to do:"
        echo "  1. agy will print a Google sign-in URL — open it in your browser."
        echo "  2. Authorise, then copy the code it gives you."
        echo "  3. Paste the code back here and press Enter."
        echo "  4. When the CLI is up and shows your account, quit with ctrl-c."
        echo
        exec docker exec -it \
            --user "${MULTIAGENTS_UID:-0}:${MULTIAGENTS_GID:-0}" \
            --env "HOME=$HOME" \
            --env "PATH=$PATH" \
            --env "TERM=${TERM:-xterm-256color}" \
            "$container" "$BIN"
    fi
    echo "Antigravity sign-in on the host."
    echo
    echo "What to do:"
    echo "  1. agy will print a Google sign-in URL — open it in your browser."
    echo "  2. Authorise, copy the code, paste it back here."
    echo "  3. When the CLI is up, quit with ctrl-c."
    echo
    exec "$BIN"
    ;;
budget)
    # agy has a full quota subsystem internally (quota_manager.go,
    # RetrieveUserQuotaSummary, refreshed every few minutes per its logs) but
    # exposes none of it — no subcommand, no cached file. Exhaustion is detected
    # reactively from a failed run.
    printf '{"known": false, "note": "CLI exposes no quota surface; exhaustion detected from failed runs"}\n'
    exit 0
    ;;
prepare)
    # agy's MCP registry is a GLOBAL profile with no per-invocation scope, so
    # this registration is visible to every agy session on this machine —
    # including subagents. That is why the mutating MCP tools are gated by
    # ownership server-side rather than by who can see them.
    # One entry serves every project: the server resolves the project from cwd.
    cmd="${MULTIAGENTS_MCP_COMMAND:-uv}"
    # shellcheck disable=SC2086
    IFS="$(printf '\037')"; set -- ${MULTIAGENTS_MCP_ARGS:-}; unset IFS
    "$BIN" mcp add multiagents "$cmd" "$@" >/dev/null 2>&1 \
        && echo "registered multiagents in agy's MCP profile" \
        || { echo "could not register the MCP server with agy" >&2; exit 1; }
    exit 0
    ;;
launch)
    if [ "${MULTIAGENTS_UNATTENDED:-0}" = "1" ]; then
        # Headless turn. -p prints and exits, which is what the supervisor
        # wants; --prompt-interactive would sit waiting for a person.
        set -- --model "${MULTIAGENTS_MODEL:-}"
        [ "${MULTIAGENTS_RESUME:-0}" = "1" ] && set -- "$@" --continue
        exec "$BIN" "$@" -p "${MULTIAGENTS_NUDGE:-continue}"
    fi
    if [ "${MULTIAGENTS_RESUME:-0}" = "1" ]; then
        set -- --model "${MULTIAGENTS_MODEL:-}" --continue
        # agy seeds an interactive session through --prompt-interactive.
        [ -n "${MULTIAGENTS_RESUME_PROMPT:-}" ] && \
            set -- "$@" --prompt-interactive "$MULTIAGENTS_RESUME_PROMPT"
        exec "$BIN" "$@"
    fi
    prompt=""
    [ -n "${MULTIAGENTS_PROMPT_FILE:-}" ] && [ -f "$MULTIAGENTS_PROMPT_FILE" ] \
        && prompt="$(cat "$MULTIAGENTS_PROMPT_FILE")"
    # agy has no system-prompt flag; the brief is seeded as the opening message
    # and the session continues interactively from there.
    exec "$BIN" --model "${MULTIAGENTS_MODEL:-}" --prompt-interactive "$prompt"
    ;;
*)  echo "usage: $0 check|login|budget|prepare|launch" >&2; exit 64 ;;
esac
