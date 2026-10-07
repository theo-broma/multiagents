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
BIN="${MULTIAGENTS_BIN:-}"
require_bin() {
    if [ -z "$BIN" ]; then
        printf '%s\n' "${MULTIAGENTS_BIN_ERROR:-MULTIAGENTS_BIN is not set}" >&2
        exit 20
    fi
}
case "${1:-check}" in
    prepare|launch|budget) require_bin ;;
esac
# Direct script invocations historically read the local account. The core
# always supplies this variable, including an empty value for unknown scope.
EXECUTOR="${MULTIAGENTS_EXECUTOR-local}"
TOKEN_REL=".gemini/antigravity-cli/antigravity-oauth-token"

# Host auth is an explicit request for the orchestrator's keyring login.
# Budget always reads the executor's account, regardless of that auth scope.
case "${1:-check}" in
    check|login)
        if [ "${MULTIAGENTS_PROFILE:-}" = "host" ]; then
            EXECUTOR="local"
        fi ;;
esac

case "${1:-check}" in
identity)
    # The active login is a keyring/opaque token, not the legacy oauth_creds
    # profile. No verified account metadata is available for that login.
    exit 64
    ;;
check)
    if [ "$EXECUTOR" = "docker" ] && [ -n "${MULTIAGENTS_PRIVATE_BACKING:-}" ]; then
        # The opaque token file only answers "was a login attempted";
        # its presence cannot verify whether the account is usable.
        backing="${MULTIAGENTS_PRIVATE_BACKING%/.gemini}"
        if [ -s "$backing/$TOKEN_REL" ]; then
            echo "container token present (not verified) ($backing/$TOKEN_REL)"; exit 0
        fi
        echo "no container token; agy has not been logged in inside the container"
        exit 10
    fi
    # On the host the credential is in the keyring and cannot be inspected, so
    # ask the CLI. It fails fast when unauthenticated, before spending anything.
    require_bin
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
    require_bin
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
        # docker login runs the action inside the container already. Host-side
        # auth login still needs the docker exec below to reach the same home.
        if [ "${MULTIAGENTS_CONTAINER_ACTION:-}" = "1" ]; then
            exec "$BIN"
        fi
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
    # agy's quota IS reachable, just not where a CLI usually puts it: the only
    # surface is the interactive `/usage` slash command. Print mode expands
    # slash commands — that is precisely what --disable-slash-commands turns
    # off — and `/usage` is answered LOCALLY from quota_manager's cache. The
    # reply comes back with num_turns 0 and total_tokens 0, so this reaches no
    # model and costs nothing, which is what the contract requires here.
    #
    # Read `.command.data`, never the `.response` text: the text is a rounded
    # four-line table, while the structured payload carries the exact
    # remaining_fraction (RetrieveUserQuotaResponse_BucketInfo_RemainingFraction,
    # already 0..1 the same way headroom is) and an RFC-3339 reset per bucket.
    # Its absence is also the signal that slash expansion was disabled, in
    # which case "/usage" WOULD have gone to the model as a prompt — so a
    # missing .command is reported unknown rather than parsed out of the text.
    #
    # --log-file /dev/null because agy otherwise drops a ~15KB dated log in
    # ~/.gemini/antigravity-cli/log on every single invocation, and budget is
    # polled on every spawn. That directory held 2,458 logs / 46MB when this
    # was written; a 60s poll would have added a thousand a day.
    login_note=""
    if [ "$EXECUTOR" = "docker" ]; then
        login_note="; run multiagents docker login ${MULTIAGENTS_PROVIDER:-agy}"
        # Read through the persistent directory mount, allowing atomic token
        # refresh. A missing container never selects the host keyring instead.
        body=$(docker exec \
            --user "${MULTIAGENTS_UID:-0}:${MULTIAGENTS_GID:-0}" \
            --env "HOME=$HOME" --env "PATH=$PATH" \
            "${MULTIAGENTS_CONTAINER:?container name not provided}" "$BIN" \
            -p "/usage" --output-format json --log-file /dev/null \
            --print-timeout 8s 2>/dev/null) || body=""
    elif [ "$EXECUTOR" = "local" ]; then
        body=$("$BIN" -p "/usage" --output-format json --log-file /dev/null \
            --print-timeout 8s 2>/dev/null) || true
    else
        printf '{"known": false, "note": "budget executor is unknown; no account was read"}\n'
        exit 0
    fi
    [ -n "$body" ] || {
        MULTIAGENTS_USAGE_LOGIN="$login_note" python3 -c '
import json, os
print(json.dumps({"known": False, "note":
    "agy did not answer /usage; not logged in, or the CLI is unavailable" +
    os.environ["MULTIAGENTS_USAGE_LOGIN"]}))'
        exit 0; }

    # Everything from here to the closing quote is a double-quoted SHELL
    # string, so it carries no backtick and no bare $ — either would be
    # substituted by sh before python ever sees this.
    printf '%s' "$body" | MULTIAGENTS_USAGE_LOGIN="$login_note" python3 -c "
import json, os, sys

try:
    groups = ((json.load(sys.stdin).get('command') or {}).get('data') or {}).get('groups')
except Exception:
    groups = None
if not isinstance(groups, list) or not groups:
    print(json.dumps({'known': False, 'note':
        '/usage returned no quota groups; slash expansion may be disabled' +
        os.environ.get('MULTIAGENTS_USAGE_LOGIN', '')}))
    raise SystemExit

# agy bills two INDEPENDENT pools and serves both from one binary: the Gemini
# models, and a 'Claude and GPT models' group for the third-party models it
# resells. Only the Gemini pool belongs to this provider's headroom. Folding
# them together would strand agy on a number it does not spend against — the
# third-party 5-hour bucket sits at zero for most of the day, and reporting
# that as agy's headroom would park every Gemini agent behind a wall that was
# never in front of it. The other group is still reported, under windows.
GEMINI = 'gemini'

windows, worst, worst_left = {}, None, None
for group in groups:
    if not isinstance(group, dict):
        continue
    name = str(group.get('name') or '')
    # 'Models within this group: Gemini Flash, Gemini Pro' — the only place the
    # payload says which models actually draw on this pool, which is the whole
    # question when deciding where to send a run.
    blurb = str(group.get('description') or '')
    models = blurb.split(':', 1)[1].strip() if ':' in blurb else ''
    for bucket in group.get('buckets') or []:
        if not isinstance(bucket, dict):
            continue
        left = bucket.get('remaining_fraction')
        if not isinstance(left, (int, float)):
            continue
        left = float(left)
        # The bucket ids are stable machine keys ('gemini-weekly', '3p-5h');
        # the group NAME is display text and moves with the model line-up.
        key = str(bucket.get('id') or '%s/%s' % (name, bucket.get('window')))
        counted = key.startswith(GEMINI)
        windows[key] = {
            # Both figures, because the two readers of this dict want
            # opposite ones: headroom is what the contract speaks, while every
            # window renderer in the project shows percent USED, the way
            # claude's and opencode's windows already report it.
            'headroom': round(left, 4),
            'percent': round((1 - left) * 100, 1),
            'resets_at': bucket.get('reset_time'),
            'group': name,
            'models': models,
            'counted': counted,
        }
        # Worst counted bucket wins: whichever is closest to empty is the one
        # that actually stops a run, and which one it is changes the response —
        # a 5-hour window clears over lunch, a weekly one does not.
        if counted and (worst_left is None or left < worst_left):
            worst, worst_left = key, left

if worst is None:
    print(json.dumps({'known': False, 'source': 'agy /usage', 'windows': windows,
                      'note': '/usage reported no Gemini buckets'}))
    raise SystemExit

other = [w for k, w in windows.items() if not w['counted']]
note = '%s is the constraint at %.0f%% used' % (worst, (1 - worst_left) * 100)
if other:
    note += '; the Claude/GPT pool is separate (%s remaining) and not counted' % (
        ', '.join('%.0f%%' % (w['headroom'] * 100) for w in other))

print(json.dumps({
    'known': True,
    'headroom': round(worst_left, 4),
    'resets_at': windows[worst]['resets_at'],
    'source': 'agy /usage',
    'note': note,
    'windows': windows,
}))
"
    exit 0
    ;;
usage)
    # Quota windows are rendered by the monitor; this action supplies extras.
    python3 - <<'PYEOF'
import json, os, sys
b = json.loads(os.environ.get('MULTIAGENTS_BUDGET') or '{}')
lines = []
seen = set()
for key, window in (b.get('windows') or {}).items():
    if not isinstance(window, dict):
        continue
    pool = str(key).partition('-')[0]
    if pool not in seen and window.get('models'):
        seen.add(pool)
        aside = '' if window.get('counted', True) else ' (separate pool)'
        lines.append(pool + ' = ' + str(window['models']) + aside)
if b.get('account'):
    lines.append('vault account ' + str(b['account']))
note = b.get('note') or ''
if note and 'is the constraint at' not in note:
    lines.append(note[:120])
if not lines:
    raise SystemExit(64)
print('\n'.join(lines))
PYEOF
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
compact)
    # No external trigger exists. agy's compaction is configured through a
    # protobuf message (genai.AntigravityAgentConfig.AntigravityCompactionConfig,
    # antigravity.localharness.CompactionConfig), internal and versioned with
    # the CLI. The one thing that looked like a route — sending "/compact" in
    # print mode — is not one: print mode EXPANDS slash commands into the
    # prompt rather than consuming them (agy --help names the flag that would
    # disable that), so the text reaches the model instead of the CLI, the
    # model attempts a tool call that gets auto-denied for lack of a
    # permission rule, and nothing is compacted. Measured 2026-09-22: the one
    # attempt cost 42,752 tokens. Do not retry it — exit 64 without starting
    # the CLI at all.
    # The same answer in check mode (MULTIAGENTS_COMPACT_CHECK=1).
    exit 64
    ;;
*)  echo "usage: $0 check|login|budget|usage|prepare|launch|compact" >&2; exit 64 ;;
esac
