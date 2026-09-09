#!/bin/sh
# Claude Code. Has a first-class auth surface, so this is thin.
set -u
BIN="${MULTIAGENTS_BIN:-claude}"

# The container's own claude profile, when there is one: a DIRECTORY, because a
# bind-mounted credential FILE is frozen at its inode on the host side and
# cannot be rewritten at all on the container side ("mv: Resource busy").
#
# It is READ here, and CLAUDE_CONFIG_DIR is exported only where this script acts
# on that profile — `check` and `login`. NOT for `launch`: the orchestrator runs
# on the host even in a docker project, and pointing it at the container's
# profile would leave it trying to start as an account it was never logged into.
# Agents need no variable at all; their per-agent HOME already resolves
# ~/.claude to whatever is mounted there.
PROFILE=""
if [ "${MULTIAGENTS_EXECUTOR:-local}" = "docker" ] \
   && [ -n "${MULTIAGENTS_PRIVATE_BACKING:-}" ]; then
    PROFILE="$MULTIAGENTS_PRIVATE_BACKING"
fi

case "${1:-check}" in
check)
    if [ -n "$PROFILE" ]; then
        # Read the file rather than asking the CLI: `auth status` would start a
        # background daemon on the HOST rooted in the container's profile, and
        # the container would then find a lock naming a pid it cannot signal.
        if [ -s "$PROFILE/.credentials.json" ]; then
            echo "container profile is logged in ($PROFILE)"; exit 0
        fi
        echo "the container profile has no credentials yet — run \`multiagents auth login claude\`"
        exit 10
    fi
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
    if [ -n "$PROFILE" ]; then
        echo "Claude Code sign-in for the CONTAINER's profile."
        echo
        echo "It runs here on the host, with your own browser — the container"
        echo "reads a directory, and this writes into it:"
        echo "  $PROFILE"
        echo
        echo "Your own ~/.claude is untouched and is not mounted into the"
        echo "container at all, so agents cannot read your conversations."
        echo
        echo "READ THIS FIRST: this is a SECOND CLI session. If you sign in"
        echo "with the same account as the host and this provider allows only"
        echo "one session at a time, signing in here will sign the host OUT,"
        echo "and the two will keep evicting each other. A second account has"
        echo "no such problem. It is checked below either way."
        echo
        printf 'continue? [y/N] '
        read -r answer
        case "$answer" in
            [yY]*) ;;
            *) echo "nothing was changed."; exit 1 ;;
        esac
        echo
        CLAUDE_CONFIG_DIR="$PROFILE" "$BIN" auth login || exit $?
        # Host-pid state, meaningless inside a container and confusing to the
        # CLI that finds it.
        rm -rf "$PROFILE/daemon" "$PROFILE/daemon.lock" "$PROFILE/daemon.status.json"
        # A backend that caps concurrent CLI sessions would have just evicted
        # the host's login. Cheap to check, and silent otherwise.
        if ! "$BIN" auth status --json 2>/dev/null \
                | grep -q '"loggedIn":[ ]*true'; then
            echo
            echo "WARNING: the HOST profile is no longer logged in. This account"
            echo "appears to allow only one CLI session at a time, so the two"
            echo "profiles will keep evicting each other. Log the host back in"
            echo "with \`claude auth login\` and use one account per profile."
        fi
        exit 0
    fi
    echo "Claude Code sign-in."
    echo "A browser window will open; complete the sign-in there."
    echo
    exec "$BIN" auth login
    ;;
budget)
    # Deliberately unimplemented. Claude's quota lives in ~/.claude.json under
    # cachedUsageUtilization — an undocumented internal cache with several
    # bucket shapes, staleness to account for, and an overage-credits block.
    # Parsing that defensively in shell would be worse code in two places, so
    # multiagents falls back to its built-in reader when a script returns 64.
    # A new provider without a built-in simply implements this action.
    exit 64
    ;;
usage)
    # How this provider's usage is shown in `multiagents monitor`. The budget
    # is already parsed and arrives in MULTIAGENTS_BUDGET, so this formats and
    # never re-fetches — one request per five minutes per machine is the whole
    # budget for asking the account anything.
    #
    # Claude's shape is two rolling windows plus a credit pool, and the pool is
    # worth naming: when it is spent, a full window stops work dead and the CLI
    # announces that as "you've hit your monthly spend limit". Somebody reading
    # this panel at that moment should not have to know that story.
    python3 -c "
import json, os, sys
b = json.loads(os.environ.get('MULTIAGENTS_BUDGET') or '{}')
if not b.get('known'):
    print(b.get('note') or 'no usage reading'); raise SystemExit
used = b.get('used_percent') or 0
bar = '#' * int(round(used / 10)) + '.' * (10 - int(round(used / 10)))
print(f\"{bar}  {used:.0f}% of the tightest window\")
if b.get('resets_at'):
    print(f\"resets {str(b['resets_at'])[:16].replace('T', ' ')}\")
spent = b.get('spent') or {}
u, limit = spent.get('extra_credits_used'), spent.get('extra_credits_limit')
if u is not None and limit:
    print(f\"credits {u / 100:.2f} of {limit / 100:.2f} — {'spent' if u >= limit else 'available'}\")
    if u >= limit:
        print('nothing carries a session past a full window')
note = b.get('note') or ''
if note and 'credits' not in note:
    print(note[:120])
" 2>/dev/null || exit 64
    ;;
prepare)
    # Nothing to register: claude takes its MCP config per invocation, so
    # nothing persists and no other session or subagent inherits it.
    exit 0
    ;;
launch)
    set -- --model "${MULTIAGENTS_MODEL:-sonnet}"
    [ -n "${MULTIAGENTS_MCP_CONFIG:-}" ] && \
        set -- "$@" --mcp-config "$MULTIAGENTS_MCP_CONFIG" --strict-mcp-config
    [ -n "${MULTIAGENTS_PROMPT_FILE:-}" ] && \
        set -- "$@" --append-system-prompt-file "$MULTIAGENTS_PROMPT_FILE"
    # Each launched role owns a session id, because `--continue` resumes the
    # most recent conversation IN THE DIRECTORY and both roles share the project
    # root — so `init-agent` after `run` would reopen the orchestrator's
    # conversation. Naming the session removes the ambiguity: resume it if it
    # exists, create it under that id if it does not.
    if [ -n "${MULTIAGENTS_SESSION_ID:-}" ]; then
        sessions="$HOME/.claude/projects/$(pwd | sed 's|[/._]|-|g')"
        if [ "${MULTIAGENTS_RESUME:-0}" = "1" ] \
           && [ -f "$sessions/$MULTIAGENTS_SESSION_ID.jsonl" ]; then
            set -- "$@" --resume "$MULTIAGENTS_SESSION_ID"
        else
            set -- "$@" --session-id "$MULTIAGENTS_SESSION_ID"
        fi
    elif [ "${MULTIAGENTS_RESUME:-0}" = "1" ]; then
        # No id: an install predating this. Fall back to the old behaviour,
        # which is still better than passing --continue into nothing.
        sessions="$HOME/.claude/projects/$(pwd | sed 's|[/._]|-|g')"
        if ls "$sessions"/*.jsonl >/dev/null 2>&1; then
            set -- "$@" --continue
        else
            echo "no previous conversation in this directory; starting a fresh one" >&2
        fi
    fi

    # A restarted interactive session opens with a message instead of waiting
    # for one to be typed. `claude [options] [prompt]` is interactive WITH a
    # first user turn; `-p` is the non-interactive form and belongs only to the
    # unattended path below.
    if [ "${MULTIAGENTS_UNATTENDED:-0}" != "1" ] && [ -n "${MULTIAGENTS_RESUME_PROMPT:-}" ]; then
        set -- "$@" "$MULTIAGENTS_RESUME_PROMPT"
    fi

    # Unattended: one non-interactive turn, so the supervisor above can decide
    # whether to run another. `-p` IS headless — it prints and exits — which is
    # exactly right here and exactly wrong for the interactive path, where it
    # would turn `multiagents run` into a one-shot.
    if [ "${MULTIAGENTS_UNATTENDED:-0}" = "1" ]; then
        set -- "$@" --permission-mode bypassPermissions -p "${MULTIAGENTS_NUDGE:-continue}"
    fi
    exec "$BIN" "$@"
    ;;
*)  echo "usage: $0 check|login|budget|usage|prepare|launch" >&2; exit 64 ;;
esac
