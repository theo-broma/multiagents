# Authentication scripts

One script per provider, implementing a single contract so that every CLI is
checked and repaired the same way. Adding a provider means adding a block to
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
