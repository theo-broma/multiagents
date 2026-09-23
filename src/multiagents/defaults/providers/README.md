# Provider scripts

One script per provider, implementing a single contract so that every CLI is
checked, repaired and measured the same way. Adding a provider means adding a block to
`providers.yaml` and a script here — no Python in the package.

## It does not have to be a shell script

The contract is a filename, an argument, an exit code and some environment
variables, none of which are shell. Name yours in `providers.yaml` and it runs:

    providers:
      myprov:
        bin: myprov
        script: myprov.py        # or myprov.js, or a compiled myprov

A `.sh` runs under `sh` whatever its mode, which is what every shipped script
and every existing install is. **Anything else runs itself**, so it needs a
shebang (or an ELF header, if you compiled it) and `chmod +x`. Both of the
realistic slips are reported with the fix named rather than as whatever the
kernel said.

This exists because the alternative was worse. The three scripts here all reach
for inline `python3 -c` heredocs to parse JSON — `claude.sh` and `agy.sh` have
one each, and `opencode.sh` has three — not because shell was the right language for reading
a billing API, but because it was the only one the contract accepted. A
provider whose quota lives behind JSON should be written in something that can
read JSON.

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

    <provider>.sh usage     non-interactive, fast. Render THIS provider's quota
                            for the monitor, as up to 12 plain lines on stdout.
                            Receives the already-parsed budget as
                            MULTIAGENTS_BUDGET and formats it; it must not
                            re-probe the CLI, because a slow action here stalls
                            every monitor refresh behind a 30s line cache.
                            The FIRST FOUR lines must each stand alone — the
                            curses monitor shows only four per provider, while
                            the web view shows all of them.
                            exit 0  = the lines are usable
                            exit 64 = not implemented; multiagents falls back to
                                      a generic rendering of the same budget

    <provider>.sh prepare   idempotently register the MCP server for this CLI,
                            so it can act as an orchestrator
                            exit 0  = ready (or nothing needed)
                            exit 64 = nothing to do

    <provider>.sh launch    exec this CLI INTERACTIVELY as the orchestrator or
                            initializer, with the MCP server attached and the
                            prompt applied. Takes over the terminal; does not
                            return.

    <provider>.sh compact   non-interactive; MULTIAGENTS_SESSION_ID names the session
                            exit 0  = compacted, and verified; stdout line 1 = figures
                            exit 64 = this provider cannot compact from outside
                            exit *  = attempted and failed; reason on stderr

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
