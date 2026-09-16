# Skill: provisioning the container

Agents on the docker executor run inside one container per project, and that
container is described entirely by `executor.docker` in
`.multiagents/config/project.yaml`. **You may change it.** A project whose
agents cannot run its own test suite is the normal starting state, not a
constraint you have to work around, and working around it is expensive: agents
hand back work they could not verify, and you re-verify it by hand with the
one context in this system that cannot be replaced.

## The symptom you are looking for

An agent reports that it could not run the tests — `flutter: not found`,
`cargo: not found`, a toolchain that is on your PATH and not on its own. Take
that at face value and fix the container; do not route the work to a different
agent, and do not accept "tests not run" as a result twice for the same reason.

An agent that says so plainly is doing the right thing. The failure you are
guarding against is the other one — an agent that reports a count it never
measured — and an agent that cannot run the suite is under exactly that
pressure every turn.

## The levers

**`extra_mounts`** — host paths made visible at the identical path inside.

```yaml
extra_mounts:
  - path: ~/flutter              # the SDK itself
  - path: ~/.pub-cache           # and its package cache
  - path: /opt/toolchain
    read_only: true
```

Two things that are learned rather than obvious:

- **Read-only breaks more toolchains than you would expect.** Flutter writes
  `bin/cache/engine.stamp` on every single invocation and dies on a read-only
  mount before it runs anything. Prefer `read_only: true`, try it first, and
  when the failure is a write to the toolchain's own directory, drop it rather
  than fighting it.
- **Mount the package cache too.** An SDK without its cache re-resolves and
  re-downloads every dependency on every run, which is slow, needs egress it
  probably does not have, and fails differently each time. With the cache
  mounted, most suites run with no network at all.

**`security.env_passthrough`** — some toolchains have to be TOLD where the
cache is, or they look in the agent's private HOME, find nothing, and go to the
network for all of it. An entry there may be a bare `NAME`, which forwards the
host's value, or `NAME=value`, which sets one:

```yaml
security:
  env_passthrough:
    - PUB_CACHE=/home/you/.pub-cache     # matching the mount above
```

A mounted cache the tool cannot see is a mount that does nothing.

**`egress_allowlist`** — what the proxy will let agents reach. Read the next
section before touching this one.

## Egress is not yours to widen

**Ask the user before adding any host to `egress_allowlist`. Every time.**

This is absolute. Not "when it seems risky", not "unless it is obviously a
package registry". The allowlist is the control that makes the rest of the
arrangement worth anything: agents hold real credentials, and what stops a
credential an agent can read from becoming a credential an agent can post
somewhere is this list and nothing else. It has no mechanical backstop. You
are it.

Say what you want to add, what breaks without it, and let them answer.

**Never add a wildcard or a bare public suffix.** Matching is suffix-anchored,
so `example.com` already covers every subdomain of it. `com` covers the
internet.

**Never set `mount_docker_socket`.** It is refused, and the refusal is not the
point — with rootful Docker that socket is host root, so an agent holding it
is no longer in a container at all.

**Never mount a path outside the project's toolchain.** Not `/`, not `/etc`,
not a home directory root, and never `~/.ssh`, `~/.aws`, `~/.gnupg` or any
other credential store. A mount is readable by every agent in the container,
all of which run with approvals turned off.

## Restarting, which is the part that bites

A mount list is fixed when the container is CREATED, not when it starts. So
`docker down && docker up` does nothing at all: `down` stops the container and
`up` starts the same one again, with the mounts it was born with. The sequence
that works is:

```
multiagents docker rm && multiagents docker up
```

Learned the hard way on 2026-09-16, by adding two mounts, restarting, and
finding the toolchain still missing — the container was a day old and nothing
said so. `multiagents docker up` now refuses a container whose mounts no longer
match the config and tells you to recreate it, so this should announce itself
rather than being silently ignored.

**Recreating kills every agent running inside.**

So: check first, and restart only when nothing is running. If agents are in
flight, either wait for them or tell the user what is queued and let them pick
the moment. Killing three agents mid-turn to install a toolchain for the
fourth is a bad trade you will not be able to undo — their worktrees survive,
their sessions and their reasoning do not.

`multiagents run` does this same dance for a stale credential, and reports
rather than restarts while agents are active. Copy that instinct.

## Write it down

A mount is a change to how this project is built, and the next person to read
`project.yaml` will not know why `~/.pub-cache` is in the list. Say so in a
comment beside it, in one line: what needs it and what fails without it.

## For subagents: you do not do this

If you are not the orchestrator or the initializer, this section is the whole
skill for you.

**Do not edit `project.yaml`.** You can reach it — the project root is mounted
— and that is not permission. Changing your own sandbox from inside it is the
one move that makes every other boundary here meaningless, and a change you
make will not take effect in this run anyway, only in somebody else's later.

Report it instead. `NEED_INFO(toolchain): the suite needs `flutter`, which is
not on PATH in this container` reaches your parent without stopping you, and
your parent can provision it. Say what you tried, what was missing, and what
you could not verify as a result — and never report a test result you did not
observe.
