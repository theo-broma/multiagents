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
    # opencode exposes no headroom surface even with an active Go subscription:
    # no subcommand, no local state, and `stats` reports $0.00 because
    # subscription models are not billed per token. Spend is tracked from the
    # event stream instead.
    printf '{"known": false, "note": "no quota surface; spend tracked from the stream"}\n'
    exit 0
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
    exec "$BIN" "$@"
    ;;
*)  echo "usage: $0 check|login|budget|prepare|launch" >&2; exit 64 ;;
esac
