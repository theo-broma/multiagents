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
budget)
    # The Go subscription serves real headroom over HTTP:
    #   GET https://opencode.ai/zen/go/v1/usage   Authorization: Bearer <key>
    # returning percent-used and a reset time for three windows (rolling,
    # weekly, monthly). No browser, no cookies, no HTML.
    #
    # There is no per-model breakdown — /zen/go/v1/usage/{models,detail,
    # breakdown,history} are all 404 and /zen/go/v1/models is an
    # OpenAI-style catalogue with no usage in it. Per-model figures come from
    # our own stream accounting instead, which is finer-grained anyway.
    #
    # The key is read from opencode's own auth store and passed in a header. It
    # is never printed, and curl gets it via --config so it cannot appear in
    # the process list either.
    auth="${XDG_DATA_HOME:-$HOME/.local/share}/opencode/auth.json"
    [ -f "$auth" ] || {
        printf '{"known": false, "note": "no opencode auth store; run `opencode auth login`"}\n'
        exit 0; }
    key=$(python3 -c "
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit
for name in ('opencode-go', 'opencode'):
    entry = data.get(name) or {}
    if entry.get('key'):
        print(entry['key']); break
" "$auth" 2>/dev/null)
    [ -n "$key" ] || {
        printf '{"known": false, "note": "no opencode-go key; free tier has no quota surface"}\n'
        exit 0; }

    body=$(printf 'header = "Authorization: Bearer %s"\n' "$key" \
        | curl -sS --max-time 12 --config - https://opencode.ai/zen/go/v1/usage 2>/dev/null)
    [ -n "$body" ] || {
        printf '{"known": false, "note": "usage endpoint unreachable"}\n'
        exit 0; }

    printf '%s' "$body" | python3 -c "
import json, sys

try:
    windows = (json.load(sys.stdin).get('usage') or {})
except Exception:
    print(json.dumps({'known': False, 'note': 'usage endpoint returned no json'}))
    raise SystemExit

# Headroom is the WORST window: whichever bucket is closest to full is the one
# that will actually stop a run, and reporting the roomiest would route work
# at a wall.
worst, best_pct = None, -1.0
detail = {}
for name, w in windows.items():
    if not isinstance(w, dict) or w.get('percent') is None:
        continue
    pct = float(w['percent'])
    detail[name] = {'percent': pct, 'resets_at': w.get('resetsAt')}
    if pct > best_pct:
        worst, best_pct = name, pct

if worst is None:
    print(json.dumps({'known': False, 'note': 'usage endpoint reported no windows'}))
    raise SystemExit

print(json.dumps({
    'known': True,
    'headroom': round(max(0.0, 1.0 - best_pct / 100.0), 4),
    'resets_at': detail[worst]['resets_at'],
    'source': 'opencode.ai/zen/go/v1/usage',
    'note': '%s window is the constraint at %.0f%% used' % (worst, best_pct),
    'windows': detail,
}))
"
    exit 0
    ;;
usage)
    # opencode serves three windows — rolling, weekly, monthly — and which one
    # is full changes what to do about it: a rolling window clears in hours, a
    # monthly one does not. So all three are shown rather than only the worst,
    # which is the number the generic renderer would pick.
    python3 -c "
import json, os
b = json.loads(os.environ.get('MULTIAGENTS_BUDGET') or '{}')
if not b.get('known'):
    print(b.get('note') or 'free tier: no quota surface, spend-only'); raise SystemExit
windows = b.get('windows') or {}
for name, w in windows.items():
    used = (w or {}).get('used_percent', (w or {}).get('percent'))
    if used is None: continue
    bar = '#' * int(round(used / 10)) + '.' * (10 - int(round(used / 10)))
    resets = str((w or {}).get('resets_at') or '')[:16].replace('T', ' ')
    print(f\"{name:<8} {bar} {used:>3.0f}%  {resets}\")
if not windows:
    print(f\"{b.get('used_percent', 0):.0f}% used\")
spent = b.get('spent') or {}
if spent.get('total'):
    print(f\"{spent['total'] / 1000:.1f}k tokens spent in this project\")
" 2>/dev/null || exit 64
    ;;
prepare)
    # `opencode mcp add` only takes --url, so a stdio server has to come from a
    # config file. OPENCODE_CONFIG lets us hand it one, which means the user's
    # own opencode.jsonc is never touched and no subagent inherits the server.
    state="${MULTIAGENTS_LAUNCH_STATE:?launch state dir not provided}"
    mkdir -p "$state"
    python3 - "$state/opencode.json" <<'PYEOF'
import json, os, sys
prompt = ""
pf = os.environ.get("MULTIAGENTS_PROMPT_FILE")
if pf and os.path.isfile(pf):
    prompt = open(pf).read()
command = [os.environ.get("MULTIAGENTS_MCP_COMMAND", "uv")]
command += (os.environ.get("MULTIAGENTS_MCP_ARGS") or "").split("\x1f")
config = {
    "$schema": "https://opencode.ai/config.json",
    "mcp": {"multiagents": {"type": "local", "enabled": True,
                            "command": [c for c in command if c],
                            "cwd": os.environ.get("MULTIAGENTS_PROJECT", ".")}},
    "agent": {"orchestrator": {"mode": "primary", "prompt": prompt,
                               "description": "multiagents orchestrator"}},
}
model = os.environ.get("MULTIAGENTS_MODEL")
if model:
    config["agent"]["orchestrator"]["model"] = model
with open(sys.argv[1], "w") as fh:
    json.dump(config, fh, indent=2)
PYEOF
    echo "wrote $state/opencode.json"
    exit 0
    ;;
launch)
    state="${MULTIAGENTS_LAUNCH_STATE:?launch state dir not provided}"
    OPENCODE_CONFIG="$state/opencode.json"; export OPENCODE_CONFIG
    set -- --agent orchestrator
    [ "${MULTIAGENTS_RESUME:-0}" = "1" ] && set -- "$@" --continue
    if [ "${MULTIAGENTS_UNATTENDED:-0}" = "1" ]; then
        # `run` is opencode's non-interactive entry point; the nudge is its
        # message, and it exits when the turn is done.
        set -- run "${MULTIAGENTS_NUDGE:-continue}" "$@"
    fi
    exec "$BIN" "$@"
    ;;
*)  echo "usage: $0 check|login|budget|usage|prepare|launch" >&2; exit 64 ;;
esac
