#!/bin/sh
# opencode. Credentials live on the HOST even when agents run in a container,
# because the container mounts opencode's data directory rather than masking it.
set -u
BIN="${MULTIAGENTS_BIN:-}"
require_bin() {
    if [ -z "$BIN" ]; then
        printf '%s\n' "${MULTIAGENTS_BIN_ERROR:-MULTIAGENTS_BIN is not set}" >&2
        exit 20
    fi
}
case "${1:-check}" in
    check|login|launch) require_bin ;;
esac

# MULTIAGENTS_OPENCODE_PLAN=zai-coding-plan selects the Z.AI Coding Plan (the
# `opencode-zai` instance). MULTIAGENTS_OPENCODE_PLAN=deepinfra selects Deep
# Infra (the `opencode-deepinfra` instance), which is metered — real dollars
# per token, no quota windows anywhere — so its budget is honestly unknown.
# MULTIAGENTS_OPENCODE_PLAN=zen selects the free Zen tier (the `opencode-zen`
# instance). Unset, empty or any other value is the Go behaviour below,
# unchanged. The quota origin can be overridden with MULTIAGENTS_ZAI_ORIGIN,
# which is for tests only.
zai_plan() { [ "${MULTIAGENTS_OPENCODE_PLAN:-}" = "zai-coding-plan" ]; }
di_plan() { [ "${MULTIAGENTS_OPENCODE_PLAN:-}" = "deepinfra" ]; }
zen_plan() { [ "${MULTIAGENTS_OPENCODE_PLAN:-}" = "zen" ]; }

# zai_py check|budget|usage. The key is read from opencode's auth store inside
# python and sent with urllib, so it is never on a command line; every message
# is a fixed string, so a server that echoes the key back cannot leak it.
zai_py() {
    python3 - "$1" "${XDG_DATA_HOME:-$HOME/.local/share}/opencode/auth.json" <<'PYEOF'
import json, os, sys, urllib.error, urllib.request
from datetime import datetime, timezone

mode, auth = sys.argv[1], sys.argv[2]
ENTRY = "zai-coding-plan"
PATH = "/api/monitor/usage/quota/limit"

def load_key():
    """Returns (key, why) where why is set when there is no store or no parse."""
    if not os.path.isfile(auth):
        return None, "no opencode auth store at " + auth
    try:
        data = json.load(open(auth))
    except Exception:
        return None, "the opencode auth store is not valid JSON"
    entry = data.get(ENTRY) if isinstance(data, dict) else None
    key = entry.get("key") if isinstance(entry, dict) else None
    if isinstance(key, str) and key:
        return key, None
    return None, None

if mode == "check":
    key, why = load_key()
    if key:
        print("z.ai coding plan credential present")
        raise SystemExit(0)
    print(why or "no '%s' entry with a key in the opencode auth store; "
          "run `multiagents auth login opencode-zai` and choose Z.AI Coding Plan" % ENTRY)
    raise SystemExit(10)

def unknown(note):
    if mode == "budget":
        print(json.dumps({"known": False, "note": note}))
    else:
        print(note)
    raise SystemExit(0)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None

def fetch(key):
    origin = (os.environ.get("MULTIAGENTS_ZAI_ORIGIN") or "https://api.z.ai").rstrip("/")
    req = urllib.request.Request(origin + PATH, headers={"Authorization": key})
    opener = urllib.request.build_opener(NoRedirect)
    opener.handlers = [h for h in opener.handlers
                       if not isinstance(h, urllib.request.ProxyHandler)]
    try:
        with opener.open(req, timeout=12) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.read()
        except Exception:
            return b""
    except Exception:
        unknown("z.ai quota endpoint is unreachable")

def ms_iso(ms):
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()
    except Exception:
        return None

def number(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

key, _ = load_key()
if not key:
    unknown("no key: no '%s' entry with a key in the opencode auth store" % ENTRY)

raw = fetch(key)
try:
    body = json.loads(raw)
except Exception:
    unknown("z.ai quota endpoint body is not JSON")
if not isinstance(body, dict):
    unknown("z.ai quota endpoint body is not JSON")
if body.get("success") is False or body.get("code") not in (None, 0, 200):
    unknown("z.ai quota request failed")
data = body.get("data") if "data" in body else body
limits = data.get("limits") if isinstance(data, dict) else None

def find(pred):
    for l in limits if isinstance(limits, list) else []:
        if (isinstance(l, dict) and l.get("type") in ("TOKENS_LIMIT", "CREDIT_LIMIT")
                and pred(l) and number(l.get("percentage")) is not None):
            return l

found = {}
five = find(lambda l: l.get("unit") == 3 and l.get("number") == 5)
week = find(lambda l: l.get("unit") == 6)
if five: found["five_hour"] = five
if week: found["weekly"] = week
if not found:
    unknown("z.ai quota endpoint reported no window")

if mode == "usage":
    lines = []
    for l in found.values():
        cur, cap = number(l.get("currentValue")), number(l.get("usage"))
        if cur is not None and cap is not None:
            lines.append("%g/%g credits" % (cur, cap))
    if not lines:
        raise SystemExit(64)
    print("\n".join(lines))
    raise SystemExit(0)

detail = {n: {"percent": l["percentage"], "resets_at": ms_iso(l.get("nextResetTime"))}
          for n, l in found.items()}
worst = max(detail, key=lambda n: detail[n]["percent"])
pct = detail[worst]["percent"]
print(json.dumps({
    "known": True,
    "headroom": round(max(0.0, 1.0 - pct / 100.0), 4),
    "resets_at": detail[worst]["resets_at"],
    "source": "api.z.ai" + PATH,
    "note": "%s window is the constraint at %.0f%% used" % (worst, pct),
    "windows": detail,
}))
PYEOF
}

# di_py check|budget. DeepInfra has no quota endpoint: it bills per token
# against its own key, so there is nothing to fetch and headroom is honestly
# unknown. `check` reads opencode's auth store for the `deepinfra` entry; the
# key is never printed, and no message is built from store content.
di_py() {
    python3 - "$1" "${XDG_DATA_HOME:-$HOME/.local/share}/opencode/auth.json" <<'PYEOF'
import json, os, sys

mode, auth = sys.argv[1], sys.argv[2]
ENTRY = "deepinfra"

def load_key():
    """Returns (key, why) where why is set when there is no store or no parse."""
    if not os.path.isfile(auth):
        return None, "no opencode auth store at " + auth
    try:
        data = json.load(open(auth))
    except Exception:
        return None, "the opencode auth store is not valid JSON"
    entry = data.get(ENTRY) if isinstance(data, dict) else None
    key = entry.get("key") if isinstance(entry, dict) else None
    if isinstance(key, str) and key:
        return key, None
    return None, None

if mode == "check":
    key, why = load_key()
    if key:
        print("Deep Infra credential present")
        raise SystemExit(0)
    print(why or "no '%s' entry with a key in the opencode auth store; "
          "run `multiagents auth login opencode-deepinfra` and choose Deep Infra" % ENTRY)
    raise SystemExit(10)

# budget. No windows, headroom unknown — never zero, and a missing window is
# not "no headroom". No network call: the branch is taken before the Go probe
# and no key is read, let alone sent anywhere.
print(json.dumps({
    "known": False,
    "headroom": None,
    "windows": {},
    "note": "DeepInfra is metered billing with no quota surface; headroom "
            "is unknown and spend is tracked per run by this project",
}))
PYEOF
}

# zen_py check|budget. Zen is the FREE tier of the same opencode CLI, and it is
# not the Go subscription: its credential is the `opencode` entry of opencode's
# auth store (Go's is `opencode-go`) and it has no documented quota endpoint at
# all. So `budget` reports unknown headroom and probes nothing — reading the Go
# usage URL here would answer Zen's capacity with Go's windows, and it is Go's
# key that URL answers to, so the fall-through also sent the wrong credential to
# the wrong subscription's endpoint. The branch is taken before the Go probe, so
# no key is read and no request is made.
#
# `check` reads that same store for the `opencode` entry. A missing entry answers
# UNKNOWN (20), never "not logged in" (10): the free Zen models run without a
# credential, so an absent one is not a failed login, and a go-only store leaves
# Zen exactly as usable as it was. Every message is a fixed string — nothing
# here is built from store content, so no key can leak.
zen_py() {
    python3 - "$1" "${XDG_DATA_HOME:-$HOME/.local/share}/opencode/auth.json" <<'PYEOF'
import json, os, sys

mode, auth = sys.argv[1], sys.argv[2]
ENTRY = "opencode"

def load_key():
    """Returns (key, why) where why is set when there is no store or no parse."""
    if not os.path.isfile(auth):
        return None, "no opencode auth store at " + auth
    try:
        data = json.load(open(auth))
    except Exception:
        return None, "the opencode auth store is not valid JSON"
    entry = data.get(ENTRY) if isinstance(data, dict) else None
    key = entry.get("key") if isinstance(entry, dict) else None
    if isinstance(key, str) and key:
        return key, None
    return None, None

if mode == "check":
    key, why = load_key()
    if key:
        print("OpenCode Zen credential present in the opencode auth store")
        raise SystemExit(0)
    # The free models need no credential, so this is unknown rather than a
    # failed authentication: nothing is known that would stop a run.
    print(why or "no '%s' entry with a key in the opencode auth store; the free "
          "OpenCode Zen models do not need one" % ENTRY)
    raise SystemExit(20)

# budget. No windows and no headroom number: Zen publishes no quota endpoint to
# read, and a missing window is not an exhausted one.
print(json.dumps({
    "known": False,
    "headroom": None,
    "windows": {},
    "note": "OpenCode Zen has no documented quota endpoint; headroom is unknown "
            "and its free models are unmetered",
}))
PYEOF
}

case "${1:-check}" in
identity)
    # API keys and their fragments are credentials, never account identities.
    exit 64
    ;;
check)
    if zai_plan; then zai_py check; exit $?; fi
    if di_plan; then di_py check; exit $?; fi
    if zen_plan; then zen_py check; exit $?; fi
    out=$("$BIN" providers list 2>/dev/null) || {
        echo "could not run '$BIN providers list'"; exit 20; }
    # "0 credentials" means no stored login. An API key in the environment is
    # reported separately and is not a subscription credential.
    case "$out" in
        *"0 credentials"*)
            echo "no stored credentials (free tier / env keys only)"; exit 10 ;;
        *)
            n=$(printf '%s' "$out" | sed -n 's/.*[^0-9]\([0-9][0-9]*\) credential.*/\1/p' | head -1)
            if [ -n "$n" ]; then
                echo "$n stored credential(s)"; exit 0
            fi
            echo "unknown: could not parse the stored credential count"; exit 20 ;;
    esac
    ;;
login)
    if zai_plan; then
        echo "Choose the Z.AI Coding Plan provider, then paste its API key."
        exec "$BIN" providers login
    fi
    if di_plan; then
        echo "Choose the Deep Infra provider, then paste its API key."
        exec "$BIN" providers login
    fi
    if zen_plan; then
        echo "Choose the OpenCode Zen provider, then sign in if you want its"
        echo "free models (they also work with no credential at all)."
        echo
        echo "Credentials are stored on the host (~/.local/share/opencode/auth.json)"
        echo "under the 'opencode' entry, separately from the Go subscription's."
        echo
        exec "$BIN" providers login
    fi
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
    # DeepInfra is metered: the branch is taken BEFORE the Go probe, so no
    # network call is made and no key is read. Zen is the same for a different
    # reason: its quota surface does not exist, and the endpoint below answers
    # to the Go key with Go's windows.
    if di_plan; then di_py budget; exit 0; fi
    if zai_plan; then zai_py budget; exit 0; fi
    if zen_plan; then zen_py budget; exit 0; fi
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
    # Quota windows are rendered by the monitor; this action supplies extras.
    if zai_plan; then zai_py usage; exit $?; fi
    # Zen has no capacity figures of any kind to add below the windows, so
    # there are no extras: exit 64 quietly rather than repeat the unknown
    # budget's own note, which the monitor already shows.
    if zen_plan; then exit 64; fi
    python3 - <<'PYEOF'
import json, os, sys
b = json.loads(os.environ.get('MULTIAGENTS_BUDGET') or '{}')
lines = []
spent = b.get('spent') or {}
if spent.get('total'):
    lines.append(f"{spent['total'] / 1000:.1f}k tokens spent in this project")
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
compact)
    # Deferred. The route is `POST /session/{id}/summarize` on opencode's own
    # HTTP server (`opencode serve`) — the literal string "/session/{id}/
    # summarize" is in the binary, alongside session.compact, session.summarize
    # and session.compacting, so the operation exists. It needs a server
    # process this project does not run, and opencode's monthly quota was
    # exhausted (until 2026-10-05) when this was measured, so it could not be
    # verified end to end. The documented CLI route
    # (`opencode run --command compact --session <sid>`) is not usable as it
    # stands: it is recognised but returns an UnknownError against a healthy
    # session, a third-party defect rather than ours. Exits 64 without
    # starting the CLI; the day the HTTP route is wired, nothing in Python
    # changes.
    # The same answer in check mode (MULTIAGENTS_COMPACT_CHECK=1).
    exit 64
    ;;
*)  echo "usage: $0 check|login|budget|usage|prepare|launch|compact" >&2; exit 64 ;;
esac
