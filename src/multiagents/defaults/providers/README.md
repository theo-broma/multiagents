# Provider scripts

One script per provider, implementing a single contract so that every CLI is
checked, repaired and measured the same way. Adding a provider means adding a block to
`providers.yaml` and a script here — no Python in the package.

## One CLI, several billing surfaces: the opencode family

`opencode` here is the **CLI base and nothing else**: which binary, how to build
its command line, how to parse the stream it prints, how it is handed the MCP
server, and which home paths to link. It is not a route. No agent may name it,
routing never selects it, `models.yaml` lists no models for it, and `budget`
reports no capacity for it.

The billing surfaces that CLI serves are providers of their own, each a few lines
because they all `extends: opencode`:

| provider | what it is | ships |
|---|---|---|
| `opencode-go` | the Go subscription — `family: opencode-go`, `models_include: [opencode-go/*]` | enabled |
| `opencode-zen` | the free tier — `models_include: [opencode/*]` | disabled |
| `opencode-zai` | the Z.AI GLM Coding Plan — `models_include: [zai-coding-plan/*]` | disabled |
| `opencode-deepinfra` | DeepInfra — `models_include: [deepinfra/*]` | disabled |

They share ONE script, `opencode.sh`, because one script serves the whole CLI:
which billing surface a run is on is decided by `MULTIAGENTS_OPENCODE_PLAN`
(zen / zai-coding-plan / deepinfra), and the Go subscription is the default,
which is what leaving it unset means. The script's file name stays `opencode.sh`
for the same reason — it is the CLI's script, not one route's.

Their families are separate on purpose: one outage took the Go subscription down
while the free tier of the very same model kept answering, and because both
lived in one provider the single breaker tripped by Go then refused Zen too —
the one thing that could still serve the request.

A config written before the split named `opencode` where it meant the Go
subscription. It still works: the name is read as `opencode-go`, with one
deprecation warning per occurrence naming the file and line to change. That
includes a `providers:` override block named `opencode` — its route-level keys
(`enabled`, `models_include`, `env`, the budget keys) apply to `opencode-go`,
while a CLI-level key (`bin`, `bin_search`, `spawn`, `mcp`, `stream`, …) still
applies to the base and therefore to every opencode provider.

The rename is declared, not implemented: `opencode-go` carries
`renamed_from: [opencode]`, and `multiagents.renames` reads that — for any
provider — into the alias, the warning, and the one-time move of durable state
keyed by the old name. Two consequences worth knowing before you add a block of
your own:

- a name that has been renamed away is **not a route**, so `models.yaml`,
  `budget` and `doctor` report nothing for it and a roster pinning it is
  refused. Declare `routable: false` on a block that was never a route at all.
- neither `routable` nor `renamed_from` is inherited through `extends:` — a
  base's answers would otherwise become every dependent's.

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

    <provider>.sh usage     non-interactive, fast. Print extras only: credits,
                            vault account, notes or project spend (up to 12
                            plain lines). Never print window bars or percentages:
                            the monitor renders all structured budget windows.
                            Receives the parsed budget as MULTIAGENTS_BUDGET.
                            Runs on the host; do not re-probe a CLI. If an extra
                            needs a binary absent on the host, exit 64 quietly.
                            Extras are cached and fetched in the background;
                            they may lag one refresh, but never delay windows.
                            exit 0  = extra lines are usable
                            exit 64 = no extras (quiet)
                            other non-zero = one short diagnostic below windows

    <provider>.sh identity  non-interactive, host-side, read-only
                            prints ONE JSON object on stdout:
                              {"identity": "...", "kind": "email|account|org"}
                            exit 0 = a non-secret scalar claim is available
                            exit 64 = unknown (quiet)
                            Never print credentials or fragments, including on
                            stderr. Select the same profile/account as usage.
                            The monitor caches claims in memory for 30 seconds;
                            polls carry only ***** and identity_available.

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

                            With MULTIAGENTS_COMPACT_CHECK=1 it is a probe: it
                            compacts nothing and does not start the CLI.
                            exit 0  = a real compact could succeed now
                            exit 64 = this provider cannot compact from outside
                            exit *  = not now (e.g. no session id, no transcript)
                            The interactive driver asks this before it stops a
                            live session to compact it. Any other value of the
                            variable is a real compaction.

                            MULTIAGENTS_COMPACT_FOCUS (empty or unset = none):
                            what the compaction should keep, at most 1000
                            characters. Forwarded to the CLI's own compaction
                            where it takes instructions, ignored otherwise;
                            the probe ignores it.

    <provider>.sh models    non-interactive; the provider's models, one per
                            line or as its `models_parse` expects
                            exit 64 = not implemented; nothing is listed

Identity readers trust the selected profile root (HOME or a configured local
profile, the vault, or the Docker private backing). That root is resolved once
with `realpath`, allowing symlinks in the root and its ancestors. Below it,
directory descriptors and `O_NOFOLLOW` refuse symlinks in files and directories,
including `accounts/<label>` inside a vault.
A symlinked `~/.claude.json` managed by a dotfile tool therefore shows unknown.
Identity candidates are compared only with credential-bearing fields: `token`,
`secret`, `password`, `key`, or names ending in `_token`, `_key` or `_secret`,
case-insensitively, at any nesting depth. Native Claude spellings such as
`accessToken`, `refreshToken` and `primaryApiKey` count as their underscored
equivalents. Both sides have whitespace removed, are casefolded and URL-decoded
at most three times, stopping early when stable. Credential values over 8 KiB
are ignored before normalization; secrets shorter than 16 normalized characters
are also ignored. Candidate identities over 254 characters give unknown. A
substring match in either direction refuses the identity. Claude also checks
the selected account's `.credentials.json`.
Identity jobs have their own two-worker pool. Reveals have a total 12-second
capacity/completion bound and return `status: "busy"` when it expires. Polls keep
the last known availability while an expired reading is refreshed.

`models` runs only for a provider with neither a static `models:` list nor a
`models_cmd`. Before CX-C4 such a provider was skipped by `refresh-models`
with "no models_cmd and no static models: list"; now its action script is
asked. A script that answers `models` with exit 64 (claude's) therefore no
longer produces that line: the provider is left out of models.yaml without a
problem recorded. Deliberate, and the one place CX-C4 is not byte-for-byte.

## Adapters

A provider may name an `adapter:` (CX-C1), found where its action script
would be. Agent runs exec the adapter, which drives `bin`; without a
`script:`, the adapter is also the action script.

In docker the adapter runs at its host path. When nothing already mounted
covers it, it is bind-mounted **as a single file**, and docker binds a file
by its inode: replacing the adapter atomically on the host (a rename, as
editors and installers do) leaves the container running the OLD copy until
the container is recreated (`multiagents docker rm && multiagents docker up`).
Editing it in place is seen at once. Keep a project adapter in a directory
that is mounted whole if you edit it often.

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
