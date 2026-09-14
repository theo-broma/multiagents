# The rewrite plan

Written 2026-09-12, with an advisor (agy, conversation `1de09b8c`) arguing the
other side. It opened by recommending a ground-up rewrite and withdrew two of
its three recommendations once it had read the code rather than the shape of
the code. Both the recommendations and the withdrawals are recorded below,
because the withdrawn ones are the ones somebody will propose again.

## The conclusion

**There is no rewrite.** The complexity in this project is not in its
architecture, it is in the domain, and `docs/open-questions.md` is the receipt:
a bind-mounted credential file fails by inode; a symlinked one is destroyed by
the rename in a token refresh; agy reports one tool call twice and silently
halved a doom-loop threshold; three providers describe token usage in words
that do not overlap and nine million claude tokens were counted as zero. None
of that is derivable from first principles. A rewrite re-derives it at the
worst possible moment, and the advisor — asked to defend its own position —
agreed without reservation.

What follows is a staged restructuring. Every step is a pure refactor with the
397-test suite as its gate, and no step requires a flag day.

## What the code actually is, measured

| | |
|---|---|
| source | 13,535 lines across 28 modules |
| tests | 397, one file, 7,448 lines, 159 seconds, green at `409844b` |
| history | 107 commits |
| import graph | acyclic; `runner.py` is the hub, `server.py` and `cli.py` the two entry points |

The import graph is clean. That matters: the problem is the size of three
files, not tangled dependencies between them, which is why this is sequencing
work rather than architecture work.

## 1 · Split `runner.py` — the only real structural crisis

2,199 lines. `Runner` has 45 methods spanning spawn, budget routing, circuit
breaking, stream consumption, merging, steering, consulting and deferral. A
change to how a budget is read can break how a git branch is merged, and
nothing in the file's shape says otherwise.

The advisor proposed an `AgentLifecycle` / `AgentMonitor` cut. That is the
right instinct and the wrong line: measured against the actual methods, the
seams are elsewhere.

### 1a · `_consume` is two functions wearing one name — do this first

374 lines, the largest method in the project, and it changes subject halfway:

```
 973–1082   the pump        read lines, parse, log, sample, flush
1083–1347   the post-mortem cancellation semantics, exit classification,
                            provider-limit verdict, circuit breaker, commit,
                            drop-if-empty, merge-into-parent, cleanup
```

Extract the second half as `_finalize(run, code, text, stderr)`. The pump then
fits on a screen and the post-mortem becomes reachable from a test without
standing up a subprocess — which is most of why the outcome-classification
logic is under-tested today relative to how much reasoning lives in it.

Cheapest step in the plan, largest single reduction. Start here.

### 1b · Routing and provider health are a separate object

`_auth_ok`, `_sample_headroom`, `_wind_down`, `_half_open`, `_maybe_cool_family`,
`_instance_load`, `_orchestrator_provider`, `can_spawn`, `_preflight` — roughly
300 lines that touch budgets, the tree's health/cooldown/claim records, and the
providers config, and touch no subprocess and no git. They are already a
coherent thing; they are simply not named.

Extract as a `Routing` collaborator constructed with the same `paths` and
`config`. `Runner` keeps one attribute and the call sites barely move.

### 1c · Parked conversations are not spawned agents

`consult` (128 lines), `_find_conversation`, `answer_question`, `steer` — about
250 lines implementing a different lifecycle: a conversation persists, is
resumed by id, holds context across calls, and is *parked* rather than running.
Commit `ea0e7c1` ("Stop calling a parked conversation a running agent") is the
history of confusing the two. Give it its own module so the distinction is
structural rather than remembered.

### What would change this

Nothing about 1a. If 1b or 1c turn out to need more than a handful of
attributes from `Runner` to be passed back and forth, stop — a collaborator
that needs its parent is worse than a long file, and the long file is not on
fire.

## 2 · Let a provider script be any executable

The extension contract is the project's stated promise: *adding an integration
means adding a block in `providers.yaml` — no Python — provided the CLI can
stream line-delimited JSON.* The contract works. `budget` genuinely falls back
(`read_provider` calls the script first and only reads `_BUILTIN` on exit 64 or
127), and `claude.sh` declines the action in exactly those terms.

What does not work is the *execution constraint*. Three places assume shell:

```
scripts.py:resolve()        name = script_name or f"{provider_name}.sh"
scripts.py:run_action()     ["sh", str(script), action]
scripts.py:exec_action()    ["sh", str(script), action]
```

Because the only supported language is `sh`, every script that needs to read
JSON reaches for an inline `python3 -c` heredoc — one in `claude.sh`, three in
`opencode.sh`. That is the real smell, and it is not "bash cannot do JSON", it
is "the contract permits only bash".

**The change:** resolve any `{provider_name}.*` (and the extensionless case),
honour the executable bit and the shebang, and fall back to `sh` for a
non-executable `.sh` so every existing install keeps working. Order must be
defined rather than discovered: an explicit `script_name` in `providers.yaml`
wins, then an executable file, then `.sh`.

**What it buys:** somebody integrating a new CLI writes `myprovider.py` against
the same env-var-and-exit-code contract and ships no Python into this package.
The promise is kept and the heredocs lose their reason to exist.

**Rejected alternative:** the advisor's first proposal was to delete the
`budget` and `usage` actions from the scripts and handle usage natively in
Python. That breaks the promise — a new provider would need a merged pull
request to report its own quota — and the advisor withdrew it when that was
pointed out. Do not re-propose it.

Strictly additive, three call sites, no migration.

## 3 · `cli.py` is two things, and only one of them is a CLI

3,000 lines, 23 subcommands — but the first ~750 lines, before any `cmd_`
function, are not command plumbing. `_run_attached`, `_run_supervised`,
`_supervise`, `_launch_agent`, `_start_supervisor`, `_other_driver_running`,
the terminal save/restore pair, the pid files and the session-id rotation are
the **driver runtime**: how this project takes over a terminal, execs a CLI,
watches it and survives its exit.

- **3a.** Extract that as `driver.py`. It is the part with real behaviour in it,
  and it is currently hidden behind a file named after argument parsing. This
  is also where `open-questions.md` §3b's known gap lands — the drivers not
  appearing in the tree — so having it in its own module is a prerequisite for
  that work rather than a detour from it.
- **3b.** Then split the `cmd_*` functions into `commands/` by topic. Genuine
  plumbing; do it last, or on a slow afternoon.

## 4 · The tests follow the code, not the other way round

7,448 lines in one file is a symptom. Do not open it as a task of its own:
split `tests/test_core.py` in the same commit that splits the module it covers,
so each extraction arrives with its tests already housed.

The one independent reason to care: 159 seconds is slow enough to discourage
running it, and a single file cannot be parallelised by module. That argument
gets stronger with every extraction above, which is another reason not to lead
with it.

## Explicitly not doing, and why

Each of these was proposed, considered, and declined with a falsifier — the
same discipline as `open-questions.md` §4.

- **A ground-up rewrite.** Declined: the empirical fixes are not recoverable
  from reasoning. *Revisit if* the provider CLIs ever converge on a protocol
  that makes stream-scraping unnecessary, which would retire most of what the
  rewrite would have had to rediscover.
- **Migrating state to SQLite.** Proposed on the grounds that a growing agent
  tree makes JSON traversal and file locks a bottleneck. Declined, and the
  advisor withdrew it: `tree.py` already gives atomic `os.replace` writes with
  `fsync` on the file *and* its parent directory, a `.bak` fallback, and a
  corrupt read that recovers and heals rather than silently emptying. The cost
  is a schema, migrations, and rewriting every test that touches a JSON file.
  *Revisit if* lock contention is ever **measured** — it never has been, and
  agents are spawned in tens.
- **Splitting `tree.json` into per-domain files with independent locks.**
  This is the genuinely unsafe middle option and it must be named so nobody
  reaches for it as a compromise. `note_run_outcome` incrementing a failure
  count, a breaker tripping, a cooldown being written and a node going to
  `failed` are **one transaction today**. Across three files they are three,
  and a crash between them leaves a provider cooled with no failure record, or
  a failure count that nothing can clear — which is precisely the bug fixed in
  `4b039dd`. The only safe alternatives are one document and one lock (what
  exists) or a transactional store (declined above). *Revisit if* contention is
  measured, in which case the answer is SQLite, not two-phase commit over JSON.
- **Treating subagents as MCP servers instead of scraping their streams.**
  Proposed, then withdrawn by the advisor on its own reasoning: the CLI stream
  is the only place in-band rate-limit messages, per-step usage and provider
  verdicts actually appear, and no CLI is contracted to expose them over MCP.
  The scraping is not a workaround, it is the data source. *Revisit if* a
  provider publishes a supported programmatic interface carrying the same
  fields.

## Order

1. `_consume` → `_finalize` (1a) — largest reduction, smallest risk
2. Provider scripts: any executable (2) — independent, three call sites
3. Routing collaborator (1b)
4. `driver.py` out of `cli.py` (3a)
5. Conversations module (1c)
6. `commands/` (3b)

Every step: `pytest tests/` green before and after, plus `multiagents doctor`
and `multiagents probe <provider>` on a real project, because neither the
script-resolution change nor the driver extraction is covered end to end by the
suite.

Steps 1 and 2 are independent of each other and of everything below them. If
only one thing gets done, do step 1.

## What this plan does not touch

`docs/open-questions.md` §3 — the watchdog thresholds — is blocked on
measurement, not on code, and none of the work above unblocks it. The
restructuring must leave `tree.json`'s node fields (`started_at`,
`last_event_at`, `ended_at`, `status`, `agent`) exactly as they are, or the
sampling script in that section stops working against historical trees and the
re-measurement starts from zero.
