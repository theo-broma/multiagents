# Map

Measured 2026-09-16 against `refactor/split-consume` @ HEAD (the de-facto trunk).

## How the numbers were taken

- **Size:** `wc -l` over `src/multiagents/`, plus tracked non-Python assets where
  they carry logic (`defaults/providers/*.sh`, `defaults/*.yaml`).
- **Blast radius:** fan-in from an AST walk of every `import` / `from .x import y`
  in the package, counting *distinct importing modules*, not import statements.
- **Churn:** `git log --name-only` over the whole branch history.
- **Coverage:** `uv run --frozen --with pytest-cov python -m pytest --cov=multiagents`,
  statement coverage, per module. `pytest-cov` is not a project dependency; it was
  added transiently for this measurement and nothing was committed to add it.

### Two caveats that change how the numbers should be read

**Churn is a much weaker signal here than usual.** The entire history is 150
commits by 1 author over 11 days (2026-09-05 → 2026-09-16). There is no "code
that has been stable for two years" anywhere in this tree, and no authorship
diversity to measure. Every file is hot. I have therefore ranked on fan-in,
irreversibility and coverage, and used churn only to break ties. Where I quote a
churn number, treat it as "share of 150 commits", not as an annual rate.

**Coverage is the strongest available half of the churn/coverage signal**, and it
separates the modules cleanly — from 96% (`config.py`) to 36% (`server.py`). It
does the work churn normally does.

## Contexts

**C1 — Sandbox and egress boundary**
*Paths:* `src/multiagents/executor/docker.py`, `executor/base.py`,
`executor/local.py`, `executor/__init__.py`, `authproxy.py`,
`src/multiagents/defaults/docker/Dockerfile`, `Dockerfile.proxy`
*Entry points:* `executor.get_executor()` from `runner`, `driver`, `cli`,
`monitor.snapshot`; the proxy process itself, which is the only route out of the
agent network; container spawn and `prepare_home`.
*Depends on:* C2 (`scripts`), C4 (`paths`, `redact`)
*Depended on by:* C3 (runner/driver), C6 (cli), C7 (monitor.snapshot)
*Size:* 1,962 Python lines across 5 modules + 52 lines of Dockerfile; `executor`
package fan-in 4, `executor.base` fan-in 3, `authproxy` fan-in 1 (reached only
through `executor.docker`)
*Churn:* `docker.py` 20 commits (4th-highest in `src/`), `authproxy.py` 2
*Coverage:* `docker.py` 66%, `base.py` 76%, `authproxy.py` **50% — the lowest of
any module that is not read-only UI**, and 118 uncovered statements sit in the
one component whose job is to refuse traffic
*Why it ranks here:* This is the only context where being wrong costs something
that cannot be taken back: a credential leaving the network. It is the sole
egress path, it is reachable by untrusted agent output by construction, and the
half of it that enforces the allowlist is the least-tested code in the tree. The
3 skipped tests are all here (they shell out to `docker`), so the real coverage
of the container path is lower than 66% reads. **Correction, 2026-09-16:** the
C1 harness grepped every `pytest.skip`/`skipif` site and found only **one** of
the three is docker-related and therefore C1's; the other two concern node and
`page.html` rendering, which is C7. C1's real coverage is a little better than
this paragraph claims, and C7's a little worse. `docker.py:774 AUTH_PROVIDER =
"claude"` also makes this the second home of the provider-plugin invariant.
*Suggested budget:* **180k**

**C2 — Provider seam, quota and failover**
*Paths:* `src/multiagents/budget.py`, `providers.py`, `scripts.py`, `auth.py`,
`catalog.py`, `models.py`, `supervisor.py`,
`src/multiagents/defaults/providers.yaml`, `defaults/providers/*.sh`
*Entry points:* `scripts.run_action()` / `exec_action()` — the plugin seam
itself; `budget.read_provider()` / `read_all()`; `providers.load()`; the three
provider shell scripts, invoked by name.
*Depends on:* C4 (`config`, `paths`, `redact`)
*Depended on by:* C1, C3, C6, C7 — everything
*Size:* 3,299 Python lines + 980 lines of shell + 459 lines of
`providers.yaml`. Fan-in: `scripts` 7, `providers` 7, `budget` 6, `auth` 5,
`catalog` 3 — four of the six highest-fan-in modules in the package that are not
pure utilities
*Churn:* `budget.py` 13, `providers.yaml` 16, `claude.sh` 18, `providers.py` 11,
`agy.sh` 9, `scripts.py` 8 — 75 commits touching the seam
*Coverage:* `budget.py` 81%, `providers.py` 93%, `scripts.py` 87%, `auth.py`
**77%**, `catalog.py` 84%, `supervisor.py` 93%
*Why it ranks here:* Highest aggregate fan-in in the codebase, and the errors are
irreversible in the money sense — a failover that moves load onto the one pool
nobody can measure spends real budget that cannot be audited afterwards. It also
holds the invariant the user named: 4 of the 5 baseline provider literals are in
`budget.py`, including `budget.py:663`, which the BRIEF itself marks unargued.
`auth.py` at 77% is the weak spot — it is credential-handling code with the
lowest coverage in the context.
*Suggested budget:* **180k**

**C3 — Agent lifecycle and concurrent tree state**
*Paths:* `src/multiagents/runner.py`, `driver.py`, `tree.py`, `watchdog.py`,
`gitops.py`, `procs.py`
*Entry points:* `runner.start_agent()` and the MCP tools that call it;
`driver`'s spawn/poll loop; `tree` state mutation from 6 distinct modules;
`watchdog`'s timers; every git operation in `gitops`.
*Depends on:* C1, C2, C4
*Depended on by:* C5 (server), C6 (cli), C7 (monitor)
*Size:* 5,447 Python lines — the largest context. `runner.py` alone is 2,601
lines and imports 13 internal modules, the widest dependency cone in the package.
Fan-in: `tree` 6, `procs` 6, `gitops` 4, `runner` 3, `watchdog` 3
*Churn:* `runner.py` 50 commits (2nd), `tree.py` 29 (4th), `driver.py` 9,
`watchdog.py` 7, `gitops.py` 5 — 100 commits, the hottest context in the tree
*Coverage:* `runner.py` **61% — 435 uncovered statements, the largest absolute
block of untested logic in the codebase**; `driver.py` 70%; `tree.py` 91%;
`watchdog.py` 84%; `gitops.py` 77%
*Why it ranks here:* The combination that predicts defects best — highest churn,
largest size, and the lowest coverage of the three top contexts, concentrated in
`runner.py`. `tree.py` is well covered at 91%, which is why this ranks below C2
rather than above it: the concurrent-state half the user worried about is the
tested half. The untested half is `runner.py`'s lifecycle and `gitops.py`'s merge
path, where being wrong costs a branch — work already done.
*Suggested budget:* **150k**

**C4 — Configuration, paths and redaction (the foundation)**
*Paths:* `src/multiagents/config.py`, `paths.py`, `redact.py`,
`src/multiagents/defaults/agents.yaml`, `defaults/project.yaml`,
`defaults/models.yaml`, `defaults/agents/**`
*Entry points:* `config.load()` at every process start; `paths` resolution used
by 11 modules; `redact.redact()` on every stream and every transcript written.
*Depends on:* nothing internal — `paths` and `redact` import no sibling
*Depended on by:* everything. `paths` fan-in **11**, `config` fan-in 9,
`redact` fan-in 8 — the three highest fan-in figures in the package
*Size:* 913 Python lines + 1,094 lines of shipped YAML + ~2,900 lines of agent
briefs
*Churn:* `config.py` 16, `project.yaml` 26, `agents.yaml` 22, `paths.py` 3,
`redact.py` 2
*Why it ranks here:* Strictly the highest blast radius in the codebase by fan-in,
and it ranks fourth anyway — because it is also the best-tested code here
(`config.py` 96%, `paths.py` 93%, `redact.py` 89%), small, and largely
declarative. That is the honest trade: blast radius is the probability
multiplier, not the probability. The live risk is in the shipped YAML rather than
the Python — `agents.yaml` carries the four non-compliant `agy:` pins the BRIEF
already names, and those propagate into every `multiagents init`. A reviewer
here should spend its budget on the data, not the loaders. `redact.py` at 89% is
worth the exception: it is the last line before a secret reaches a transcript.
*Suggested budget:* **90k**

**C5 — MCP server and tool surface**
*Paths:* `src/multiagents/server.py`, `findings.py`, `bugs.py`, `transcripts.py`
*Entry points:* every MCP tool an agent can call — this is the API surface the
agent tree drives itself through.
*Depends on:* C2, C3, C4
*Depended on by:* nothing internal (fan-in 0 — it is a top-level entry point)
*Size:* 1,776 Python lines
*Churn:* `server.py` 23 commits (5th-highest in `src/`)
*Coverage:* `server.py` **36% — the lowest in the entire codebase, 242 uncovered
statements**; `bugs.py` 53%; `transcripts.py` 85%; `findings.py` 94%
*Why it ranks here:* Exposure is real — this is the surface untrusted agent
output reaches directly, and it is the least-covered module in the tree against
the 5th-highest churn. It ranks below C4 only because a defect here is contained:
fan-in 0 means nothing else breaks when it does, and the BRIEF's own ticket
channel exists because failures here are *observed at runtime* rather than
needing to be read out of the source. Much of the 36% is likely tool-wiring
boilerplate; a reviewer should confirm that before treating the number as alarm.
*Suggested budget:* **90k**

**C6 — Command-line interface**
*Paths:* `src/multiagents/cli.py`
*Entry points:* the `multiagents` console script; every subcommand.
*Depends on:* C1, C2, C3, C4, C7 — imports 19 internal modules, the largest
import list in the package
*Depended on by:* `monitor.snapshot` only, and only through two
**function-local deferred imports** (`snapshot.py:517` and `:525`, both
`from ..cli import ...`)
*Size:* 2,524 lines — the second-largest module
*Churn:* 66 commits — **the highest-churn file in `src/`**
*Coverage:* **43%**, 866 uncovered statements
*Why it ranks here:* This is the explicit test of the BRIEF's "rank by blast
radius, not line count" instruction, and the measurements uphold it. `cli.py` is
the biggest, hottest, and among the least-covered files in the tree — and it
still ranks sixth, because fan-in is 1 and that 1 is a deferred import used for
error formatting. Being wrong here produces a bad message or a failed command in
front of a human who can see it, not a leaked credential or a lost branch. Worth
noting for C7's reviewer rather than this one: that `snapshot → cli` deferred
import is the only import cycle in the package, and it exists to dodge one.
*Suggested budget:* **60k**

**C7 — Monitor, TUI and snapshot**
*Paths:* `src/multiagents/monitor/**` (`snapshot.py`, `settings.py`, `tui.py`,
`actions.py`, `server.py`, `page.html`)
*Entry points:* `multiagents monitor` (TUI) and the monitor web server;
`monitor.actions` is the one write path — it can pause, resume and kill agents.
*Depends on:* C1, C2, C3, C4, C6
*Depended on by:* C6 (`cli` imports `monitor` to launch it)
*Size:* 2,014 Python lines + 814 lines of `page.html`
*Churn:* `snapshot.py` 12, `page.html` 5, `tui.py` 3, `actions.py` 2
*Coverage:* `actions.py` **41%**, `tui.py` 45%, `server.py` 52%, `snapshot.py`
65%, `settings.py` 66%
*Why it ranks here:* Almost all of it is read-only presentation, where being
wrong means a human sees a stale number — recoverable, and visible while it
happens. It ranks last despite poor coverage for that reason. The one part that
does not fit that description is `monitor/actions.py`, which mutates tree state
and kills processes at 41% coverage; if this context is reviewed at all, that
file is the reason and the rest can be skimmed.
*Suggested budget:* **50k**

## Not covered

- **`tests/test_core.py` (10,945 lines) is not a context.** It is the subject of
  phase 4 rather than of review, and characterizers are assigned per-context by
  the contexts above. A reviewer reading it should read the slice for its own
  context, not the file.
- **`README.md` (153 KB) and `docs/`** are excluded from every context. The BRIEF
  directs `researcher` at the README, and that stands.
- **`uv.lock`** (156 KB, generated) — not reviewed. No context claims it.
- **`sys` — a 64 MB PostScript file, tracked in git at the repository root.**
  Written 2026-09-14 by ImageMagick (`%%Creator: (ImageMagick)`), almost
  certainly a shell redirection that was meant to be an argument. It is 99.6% of
  the repository's bytes. I have left it out of every context because nothing
  imports it and it has no logic, but it is not covered by anyone and should not
  be silently ignored — **it is worth filing as a finding on its own** (repo
  hygiene: every clone pays 64 MB for a typo). Flagging rather than filing,
  because writing findings is not my job.

  **Established 2026-09-16 by the orchestrator, and it is worse than "nobody
  noticed":** the file is 64,713,591 bytes. It was introduced by `ba25c7c`
  ("Move the driver runtime out of the file named after argument parsing"), a
  refactor moving 924 lines out of `cli.py` into a new `driver.py` — 820,435
  insertions in total, of which `sys` is 819,386. It was swept in by a large
  commit nobody could read line by line. Two days later, `a5b7a47` added
  `/advisor` and `/sys` to `.gitignore` with the comment *"A shell redirection
  that lands on `advisor` or `sys` instead of a flag is easy to make and easy to
  commit without noticing — one 62 MB PostScript file got in that way already."*
  So the accident **was** noticed and a guard rail **was** added. But
  `git check-ignore -v sys` returns nothing: an ignore rule has no effect on an
  already-tracked file. The guard rail prevents the next accident and does
  nothing about this one, while reading as though the matter were closed. That
  is the finding — a fix that looks like a fix.

  Two corrections to the figures above: nothing references the path (only the
  `.gitignore` comment names it), so removing it breaks nothing; and the packed
  repository is 24.16 MiB, not 64 MB — PostScript is text and compresses well.
  "Every clone pays 64 MB" overstates it. The real cost is ~24 MB and a working
  tree that is 99.6% one stray file.
- **`.multiagents/`** is gitignored, so the live roster and config are invisible
  from a worktree. The BRIEF's finding about the live `agents.yaml` being
  asserted by nothing **cannot be verified by any agent working in a worktree** —
  only the shipped defaults under `src/multiagents/defaults/` can. Whoever is
  assigned C4 needs to know this before it concludes the roster is fine.
- **What aggregation to seven hides:** C2 folds together three things that could
  each be their own context — the plugin seam (`scripts.py`), quota and failover
  (`budget.py`), and credential handling (`auth.py`). They are grouped because
  they share the provider abstraction and are read together, but a reviewer given
  C2 and a small budget will likely spend all of it on `budget.py` and never
  reach `auth.py` at 77%. If budget allows only one split anywhere in this map,
  split C2. Likewise C3 folds `gitops.py` (merge correctness, 77%) in with
  lifecycle, and it will lose the same way against `runner.py`'s 2,601 lines.

## A measurement I could not complete — now settled

**SETTLED 2026-09-16 by the orchestrator, and the hypothesis below was wrong.**
Both commands were run on the host:

| command | result |
|---|---|
| `uv run --frozen python -m pytest -q` | **1 failed, 547 passed** in 299s |
| `uv run --frozen python -m pytest -q -p no:randomly` | **1 failed, 547 passed** in 297s |

Disabling `pytest-randomly` changes nothing, so **execution order is not the
cause** and the test-order hypothesis below is falsified.

**Nor is it the coverage instrumentation** — that was this section's first
conclusion, and the C1 harness disproved it within the hour. Running the suite
plain, with neither `-p no:randomly` nor `--cov`, *from inside an agent*, it got
**17 failed / 537 passed / 3 skipped**, and every one of the 17 is
`PermissionError: ... can_spawn is false`, in `test_core.py`'s C3/runner tests.

So the real variable is **who runs the suite**. Seventeen tests exercise agent
spawning, and they fail for any process whose `can_spawn` is false — which is
every agent in the tree except the orchestrator. The cartographer saw 17 because
it was an agent, not because of `--cov`.

**The consequence is larger than the question that uncovered it: the suite is
green nowhere.**

| who runs it | result |
|---|---|
| the orchestrator, on the host | 1 failed, 547 passed, 0 skipped |
| any agent, inside the container | 17 failed, 537 passed, 3 skipped |

Two disjoint failure sets. On the host the 17 spawn tests pass and
`claude.sh check` fails; inside an agent `claude.sh check` passes and the 17
spawn tests fail. Nobody has seen this suite green, and the documented 545/3
corresponds to neither environment.

That matters for every remaining phase. **An agent cannot tell its own breakage
from the environment's**: a characterizer running the suite sees 17 red tests
that have nothing to do with its work, and the only way it can know that is to
be told. Whoever writes a task for an agent that runs this suite must say so.
The coverage percentages in this map are unaffected — coverage is collected per
statement regardless of which tests fail.

Two further things these runs establish, which matter more than the question they
answered:

- **The documented baseline of 545 passed / 3 skipped is not what the suite does.**
  On a host where `docker` is available, nothing skips and 548 tests run. The 3
  "skipped" tests are only skipped where they cannot run, which is inside the
  container every agent works in.
- **Trunk is red, by one test**, and it is not a flake:
  `tests/test_core.py::test_the_claude_script_uses_the_container_profile_only_where_it_should`
  fails at `tests/test_core.py:8892` with `assert (0 == 10)`. `claude.sh check`,
  pointed at a container profile directory that has just been created and is
  **empty**, returns 0 and prints `container profile is logged in`. It concludes a
  profile is authenticated from the directory existing. That belongs to **C2**, and
  it raises C2's value: the seam is not merely under-tested, it has a failing test
  on trunk that nobody is reading.

The original note follows, kept for the record.

## A measurement I could not complete (original note, hypothesis now falsified)

Running the suite with coverage instrumentation and fixed ordering
(`-p no:randomly --cov=multiagents`) produced **17 failures, 528 passed, 3
skipped** — not the documented green of 545/3. Most failures reported
`PermissionError`. I was interrupted before I could isolate the cause, so this is
recorded as an open question, not as a finding.

The likely explanation is **test-order dependence, not a code defect**:
`pytest-randomly` is active by default in this project, I disabled it to make
coverage reproducible, and that pinned execution to file order. Several of the
failing tests have names suggesting they chmod a directory read-only
(`test_a_spent_budget_refuses_the_next_spawn`,
`test_an_agent_whose_brief_is_missing_refuses_to_run`), which would leak a
non-writable directory into whatever runs next. If so, it belongs to the same
family as the test-isolation finding the BRIEF already documents, and the
coverage percentages above are still sound — coverage is collected per statement
regardless of which tests fail.

**It must be confirmed before anything is built on it.** Whoever picks this up:
run `uv run --frozen python -m pytest -q` plain (expect 545/3), then
`uv run --frozen python -m pytest -q -p no:randomly` without `--cov` to separate
the ordering variable from the instrumentation variable. If the second one fails
and the first passes, it is ordering, and it is a real finding about the suite.

## Suggested budgets against the real ceiling

Total across all seven: **800k**. The first pass is expected to cover C1–C3 —
**510k**, which is the number that actually has to be affordable.

The split is deliberately uneven, weighted by rank and by how much untested code
a reviewer has to read rather than by line count. C6 is the second-largest module
in the codebase and gets the second-smallest budget; C4 is the highest-fan-in
context and gets half of C1's. Both are consequences of ranking by cost-of-being-
wrong, and both are the kind of thing worth overriding deliberately rather than
by accident.

Given opencode at 85% of its window with 19 days to run, and claude at 60% with
no overage credits, I would rather see C1 and C2 fully covered at 180k each and
C3 recorded as half-covered than see all three run short. If something has to
give, cut C3 to 100k before cutting either of the first two.
