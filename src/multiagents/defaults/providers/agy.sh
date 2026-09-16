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

# Asking about the HOST profile means asking about the keyring, which is the
# non-docker path below — so the cheapest way to answer is to stop being a
# docker run for the length of this call. The orchestrator runs on the host
# whatever the executor is, so somebody has to be able to ask.
if [ "${MULTIAGENTS_PROFILE:-}" = "host" ]; then
    EXECUTOR="local"
fi

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
    body=$("$BIN" -p "/usage" --output-format json --log-file /dev/null \
        --print-timeout 8s 2>/dev/null) || true
    [ -n "$body" ] || {
        printf '{"known": false, "note": "agy did not answer /usage; not logged in, or the CLI is unavailable"}\n'
        exit 0; }

    # Everything from here to the closing quote is a double-quoted SHELL
    # string, so it carries no backtick and no bare $ — either would be
    # substituted by sh before python ever sees this.
    printf '%s' "$body" | python3 -c "
import json, sys

try:
    groups = ((json.load(sys.stdin).get('command') or {}).get('data') or {}).get('groups')
except Exception:
    groups = None
if not isinstance(groups, list) or not groups:
    print(json.dumps({'known': False, 'note':
        '/usage returned no quota groups; slash expansion may be disabled'}))
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
    # How agy's own /usage screen reads, as far as a few lines allow. The
    # generic view flattens all four buckets into "N% used" rows, which loses
    # the two things agy's display leads with: that the buckets belong to two
    # different pools, and how long until each one refreshes.
    #
    # Formats MULTIAGENTS_BUDGET rather than re-running the CLI: the budget was
    # already read, and a 4s probe behind a 30s line cache would make the
    # monitor pause on every refresh.
    #
    # The FIRST FOUR LINES must each stand alone. The curses monitor shows only
    # four lines per provider, so the group legend below them is a bonus for the
    # web view, never where the percentages live.
    #
    # Phrased as USED, and the bar fills as the quota is spent. agy's own
    # screen counts remaining, but this panel is not agy's screen: the header
    # directly above these lines says "20% used", and so does every other
    # provider in the monitor. Counting the other way round here made a FULL
    # bar mean untouched on one row and exhausted on the next, which is the
    # one thing a bar has to get right.
    [ -n "${MULTIAGENTS_BUDGET:-}" ] || exit 64
    # Captured rather than streamed, so that the formatter's exit 64 survives.
    # Ending this block with a bare `exit 0` swallowed it, and the fallback then
    # happened only because stdout was empty — the right view for the wrong
    # reason, and one stray print away from showing a blank panel instead.
    rendered=$(printf '%s' "$MULTIAGENTS_BUDGET" | python3 -c "
import json, sys
from datetime import datetime, timezone

try:
    windows = (json.load(sys.stdin).get('windows') or {})
except Exception:
    raise SystemExit(64)
if not windows:
    raise SystemExit(64)

def until(stamp):
    'Computed now, not at probe time: a 5-hour window moves while we look.'
    try:
        when = datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return ''
    left = (when - datetime.now(timezone.utc)).total_seconds()
    if left <= 0:
        return 'due'
    days, rest = divmod(int(left), 86400)
    hours, rest = divmod(rest, 3600)
    if days:
        return '%dd%02dh' % (days, hours)
    if hours:
        return '%dh%02dm' % (hours, rest // 60)
    if rest < 60:
        return '<1m'        # seconds away; '0m' reads as a broken number
    return '%dm' % (rest // 60)

rows, legend = [], []
seen = set()

# Ordered here rather than taken as given. The caller serialises the budget with
# sort_keys, so insertion order is not ours to rely on and relying on it put the
# 5-hour row above the weekly one only when read through the monitor.
# Counted pool first, because it is the one that decides whether a run can
# start; then longest window first, which is how agy's own screen reads.
SPAN = {'weekly': 0, '5h': 1}

def rank(item):
    key, w = item
    span = str(key).partition('-')[2]
    return (not w.get('counted', True), SPAN.get(span, 2), span, key)

for key, w in sorted(windows.items(), key=rank):
    if not isinstance(w, dict) or w.get('headroom') is None:
        continue
    used = 1.0 - float(w['headroom'])
    pool, _, span = str(key).partition('-')
    filled = int(round(used * 10))
    rows.append('%-6s %-6s %s %3.0f%% used%s%s' % (
        pool, span or '?',
        '█' * filled + '░' * (10 - filled),
        used * 100,
        ' · ' + until(w.get('resets_at')) if w.get('resets_at') else '',
        '' if w.get('counted', True) else ' · not counted',
    ))
    if pool not in seen and w.get('models'):
        seen.add(pool)
        legend.append('%-6s = %s%s' % (
            pool, w['models'],
            '' if w.get('counted', True) else ' (separate pool)'))

if not rows:
    raise SystemExit(64)
print('\n'.join(rows + legend))
") || exit 64
    [ -n "$rendered" ] || exit 64
    printf '%s\n' "$rendered"
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
*)  echo "usage: $0 check|login|budget|usage|prepare|launch" >&2; exit 64 ;;
esac
