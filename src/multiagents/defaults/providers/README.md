# Provider scripts

One script per provider, implementing a single contract so that every CLI is
checked, repaired and measured the same way. Adding a provider means adding a block to
`providers.yaml` and a script here — no Python.

## Contract

    <provider>.sh check     non-interactive, fast
                            exit 0  = authenticated
                            exit 10 = NOT authenticated
                            exit *  = unknown
                            stdout  = one line of human-readable status

    <provider>.sh login     may be interactive and take over the terminal
                            print what the user must do BEFORE doing it
                            exit 0 on success

    <provider>.sh budget    non-interactive, fast
                            prints ONE JSON object on stdout:
                              {"known": bool, "headroom": 0..1, "severity": str,
                               "resets_at": str, "note": str}
                            exit 0  = the JSON is usable
                            exit 64 = not implemented; multiagents falls back to
                                      a built-in reader if it has one

    <provider>.sh prepare   idempotently register the MCP server for this CLI,
                            so it can act as an orchestrator
                            exit 0  = ready (or nothing needed)
                            exit 64 = nothing to do

    <provider>.sh launch    exec this CLI INTERACTIVELY as the orchestrator or
                            initializer, with the MCP server attached and the
                            prompt applied. Takes over the terminal; does not
                            return.

## Environment provided

    MULTIAGENTS_PROVIDER          provider name
    MULTIAGENTS_BIN               absolute path to the CLI binary
    MULTIAGENTS_EXECUTOR          local | docker
    MULTIAGENTS_CONTAINER         container name        (docker only)
    MULTIAGENTS_PRIVATE_HOME      path the CLI sees     (docker, if private)
    MULTIAGENTS_PRIVATE_BACKING   host dir behind it    (docker, if private)
    MULTIAGENTS_UID / _GID        uid:gid to run as

`MULTIAGENTS_RESUME` is **advisory, not a promise.** It is set from a marker
written before the CLI is launched, so it records that a session was started
here once — not that one ever produced a resumable conversation. A user who
quits the first session without saying anything leaves the marker behind, and a
resume flag passed blindly then fails on every later run. Each `launch` must
therefore check that a conversation actually exists before adding its resume
flag, and start fresh (saying so on stderr) when none does. `claude.sh` does
this by looking for a transcript under `~/.claude/projects/<cwd slug>/`.

Additionally for `prepare` and `launch`:

    MULTIAGENTS_MODEL             model the roster entry asks for
    MULTIAGENTS_PROMPT_FILE       the agent's brief, freshly written
    MULTIAGENTS_MCP_CONFIG        a claude-shaped mcpServers JSON file
    MULTIAGENTS_MCP_COMMAND       the server command, for CLIs that want parts
    MULTIAGENTS_MCP_ARGS          its arguments, \x1f-separated
    MULTIAGENTS_LAUNCH_STATE      a scratch directory the script may write to
    MULTIAGENTS_RESUME            "1" to continue the last session, "0" for new
    MULTIAGENTS_ROLE              orchestrator | initializer
    MULTIAGENTS_PROJECT           the project root

`check` and `budget` must not cost money or require a network round trip where a
local signal will do — both run on paths that are hit often. Prefer inspecting
stored credentials over probing the API.

Captured actions (`check`, `budget`) have their output read. Handed-over actions
(`login`, `launch`) are exec'd by the caller, because they need the terminal.

Scripts resolve **project → global → shipped**, so a project can override one
provider's behaviour without touching the machine. The legacy `auth/` directory
is still searched for installs predating the rename, but always loses to
`providers/` in the same layer.
