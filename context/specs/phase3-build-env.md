# Phase 3 contract — two `build_env`s that do not enforce their own boundary

The interface contract for `BRIEF.md` phase 3 item 3: **F112** and **F33**.
Written by the orchestrator.

The findings are files, not tools. `context/review/C2-seam.md` for F112,
`context/review/C1-sandbox-executor.md` for F33. You have neither
`read_finding` nor `list_tickets`; do not try them.

**Treat F33 at medium/security, not its filed low/correctness.** The review
recorded that disagreement deliberately and `BRIEF.md` resolves it: forwarding
a variable the configuration explicitly blocked is the security reading, and
that is the one to act on.

---

## Two functions, opposite failures, one boundary

There are two `build_env`s and each breaks the promise it makes.

**`executor/base.py:build_env`** says in its own docstring:

> *Deny-by-default: the child starts with nothing and receives only what is
> named.*

It then forwards `BASE_ENV_KEYS` — `PATH`, `LANG`, `LC_ALL`, `LC_CTYPE`,
`TERM`, `TZ`, `TMPDIR`, `SHELL`, `USER` — in a loop that never consults
`blocked`. Only the `passthrough` loop checks it. So naming one of those nine
in `security.env_block` does nothing at all, silently. That is **F33**.

Its impact today is nil: nothing in the shipped or live `env_block` is one of
the nine. Say so in your result rather than overclaiming. What is being fixed
is that the function does not do what it says, and the next person to add
`SHELL` to a block list will believe it worked.

**`scripts.py:build_env`** does the exact opposite:

```python
env = dict(os.environ)
```

The calling process's entire environment, unfiltered, handed to every provider
script invocation — and a **project-local** script wins the precedence in
`resolve()`, so this is the orchestrated project's own file receiving the
orchestrator's live environment. Measured during the review: it included
`CLAUDE_CODE_MESSAGING_TOKEN`, `ANTHROPIC_BASE_URL` and the sandbox's internal
proxy URLs. That is **F112**, and it is the high-severity half.

---

## R19 — `blocked` applies to everything, or it is not a block list

**Observable behaviour required.** A variable named in `blocked` does not
appear in the environment `executor/base.py:build_env` returns, whatever route
it would otherwise have arrived by.

**What must be true afterwards:**

- A `BASE_ENV_KEYS` member named in `blocked` is absent from the result.
  `PATH` is the one to test with, because it is the one whose absence would
  actually be noticed.
- Everything `blocked` already achieved still holds: the passthrough loop, and
  the registration of blocked values as redaction literals via
  `register_environment`.
- Nothing else changes about what a child receives when `blocked` is empty,
  which is the normal case.

**A judgement call I am making rather than leaving open:** blocking `PATH`
should produce an environment with no `PATH`, not a substituted default. A
configuration that blocks `PATH` is asking for something strange and should
get exactly what it asked for, loudly, rather than a quiet fallback that makes
the block look ineffective again.

---

## R20 — a provider script receives a named environment, not an ambient one

**Observable behaviour required.** The environment `scripts.py:build_env`
returns contains only: a documented base, the `MULTIAGENTS_*` keys the function
computes, the provider's own `env:` block, and the `extra` argument. A variable
present in `os.environ` and in none of those does not reach a script.

**What must be true afterwards:**

- A credential-shaped variable in the calling process's environment —
  `ANTHROPIC_API_KEY`, `CLAUDE_CODE_MESSAGING_TOKEN`, `GITHUB_TOKEN` — is
  absent from the returned environment.
- Everything the function already computes is unchanged: the `MULTIAGENTS_*`
  block, the docker branch's private-home and vault keys, the provider `env:`
  expansion, and `extra` last.
- **All three shipped provider scripts still work, for every action they
  implement.** This is the requirement that decides whether the change is any
  good, and it is stated before the allowlist on purpose.

### The allowlist is yours to derive, and to prove

I am **not** dictating the list, because I cannot establish it from here with
confidence and a guessed one fails silently — a provider script that loses a
variable does not crash, it returns `known: false`, and this project has
already spent a session routing on budget numbers that were wrong.

What I have established, and you should start from rather than rediscover:

- The three shipped scripts read exactly four ambient variables between them:
  `HOME` (all three), `PATH` and `TERM` (`agy.sh`), `XDG_DATA_HOME`
  (`opencode.sh`). They also invoke `python3` by name, so `PATH` is load-bearing
  beyond the one script that names it.
- `CLAUDE_CONFIG_DIR` is **not** an ambient dependency. It arrives through the
  provider's `env:` block (`providers.yaml:415-447`) and `claude.sh` exports it
  itself at lines 258 and 348. An allowlist does not threaten it.
- `base.build_env` already computes a defensible base for the same job:
  `BASE_ENV_KEYS` plus `HOME` and the three `XDG_*` paths. **The two functions
  agreeing about what the base is would be a good outcome**, and a better one
  than a second hand-written list.
- `tests/support/c2_harness.py:227`'s `_SCRIPT_ENV_KEYS` is **not** the
  allowlist, despite what F112's reasoning implies. It is a list of keys the
  harness *strips* so a developer's real state does not leak into a test. Do
  not mistake it for prior art on what to keep.

### The one that will catch you

`ANTHROPIC_BASE_URL` is set at **container creation** (`docker.py:936`) when
the auth proxy is on. Under the docker executor a script reached by
`docker exec` inherits it from the container, so an allowlist here is harmless.
Under the **local** executor there is no container, and `scripts.build_env`'s
output is the process environment — so the same allowlist would remove it.

**Establish which of those is true before you decide**, and say what you found.
If the local executor genuinely needs it, the answer is a passthrough entry,
not abandoning the allowlist.

### An escape hatch is required, not optional

A fixed allowlist with no way to extend it means the next provider that needs
one variable has to patch multiagents. `base.build_env` already has the right
shape for this — `passthrough`, where a bare `NAME` forwards this process's
value and `NAME=value` sets one. Give `scripts.build_env` access to the same
idea, driven by configuration rather than by editing the list.

Where that configuration lives is your choice; `security.env_passthrough`
already exists and already means this.

---

## What is NOT in scope

- **F73**, whose severity F33 is being lifted to. Referenced for the severity
  argument only; it is not this change.
- **`security.env_block`'s contents.** The live and shipped lists are correct;
  this is about the mechanism honouring them.
- **The `MULTIAGENTS_*` protocol itself.** What those keys are and mean is
  settled; only what accompanies them is in question.
- **`register_environment` and the redaction machinery.** It works. Do not
  widen it here.

---

## What "done" looks like

Baseline: **989 passed, 3 skipped, 0 failed** in a container as of the R13
merge. Run the suite as `uv run --frozen python -m pytest`, never
`uv run --frozen pytest`.

**Expect a characterization test to go red.**
`tests/test_c2_seam_characterization.py::test_build_env_copies_the_full_ambient_process_environment`
pins F112's behaviour, and
`tests/test_c1_executor_characterization.py::test_build_env_base_keys_are_forwarded_even_if_named_in_blocked`
pins F33's. Both are correct records of what the code does and both invert
under this change.

**The implementer does not touch them.** They are read-only to that tier and
the merge gate reverts the change silently. A test you believe must change is
`NEED_INFO(<test name>)` back to me, and a test engineer inverts it afterwards
— renamed so the name states what is now true, with a comment naming the
finding and saying the inversion is deliberate. That has now been done four
times in this project.

R19 and R20 are done when a blocked variable is blocked by every route, a
provider script receives only a named environment, every shipped script still
works for every action, the escape hatch exists, and the baseline is otherwise
unchanged.
