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

## Environment provided

    MULTIAGENTS_PROVIDER          provider name
    MULTIAGENTS_BIN               absolute path to the CLI binary
    MULTIAGENTS_EXECUTOR          local | docker
    MULTIAGENTS_CONTAINER         container name        (docker only)
    MULTIAGENTS_PRIVATE_HOME      path the CLI sees     (docker, if private)
    MULTIAGENTS_PRIVATE_BACKING   host dir behind it    (docker, if private)
    MULTIAGENTS_UID / _GID        uid:gid to run as

`check` must not cost money or require a network round trip where a local
signal will do. Prefer inspecting stored credentials over probing the API.
