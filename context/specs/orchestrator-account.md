# The orchestrator runs on the account that was checked, and moves on when it is spent (OA)

Observed 2026-10-08: the orchestrator ran out of quota at 01:13 on one Claude
account and stayed stopped while another Claude account had headroom. When the
user then edited agents.yaml, `multiagents run` refused `claude-b` ("no quota")
and accepted `claude`, but the session it launched had no quota either.

Researchers ag-677325 and ag-34e6b6:
- `multiagents run` launches the root role on the host. Provider `claude` uses
  the host's default profile. `claude-b` uses
  `CLAUDE_CONFIG_DIR=~/.multiagents/profiles/claude-b` (providers.yaml:34-38).
- The pre-launch quota check (`_orchestrator_hold`, driver.py:1679-1713) and
  the reset lookup (driver.py:1756-1774) go through `executor_for()`. Under the
  docker executor, that path reads the Docker vault accounts, not the host
  profile the root will run on.
- On a detected limit, the driver waits and relaunches the same provider
  (driver.py:1025-1130, 1777-1828). Quota handover covers spawned runs only.
- Building blocks that already exist:
  - `budget.read_claude(config_dir=…)` reads any profile's usage
    (budget.py:495-546).
  - The `identity` action names a profile's account without exposing secrets
    (claude.sh:51-160).
  - MCP is passed per invocation (claude.sh:695-703).
- Transcripts live under `${CLAUDE_CONFIG_DIR}/projects/<slug>`. `--resume <id>`
  works only when that profile holds `<id>.jsonl` (claude.sh:192-199,
  711-723).

Mechanisms, data layout and file operations are the developer's choice unless
named below.

"Root" means whichever role `multiagents run` launches: the orchestrator or
the initializer. Every behaviour below applies to both.

## Behaviours

**OA-R1.** The root's pre-launch quota check reads the same account the root
will run on: the host profile its provider resolves to. That is the default
profile for `claude`, and the provider's `CLAUDE_CONFIG_DIR` otherwise. This
holds whatever executor the agents use. A refusal or a wait names that
provider and that profile's reset time.
Verified by: two fake host profiles (one exhausted, one with headroom) and a
fake vault that disagrees with both. Launching on each provider checks the
matching host profile, and the vault is never read for the root.

**OA-R2.** On a detected limit, the reset time and the retry admission both
come from host profiles, as in OA-R1. A wait ends by re-selecting a usable
account (OA-R4). It never relaunches merely because a timestamp passed.
Verified by: a test where the vault and the host profile report different
resets. The wait uses the host profile, and a still-exhausted profile at
wake-up is not relaunched.

**OA-R3.** Every root launch prints one line to the terminal (stderr when
stdout is not a terminal). The line names:
- the provider;
- the profile's account identity, from the `identity` action;
- its headroom.

If the identity action fails or times out, the line says "identity unknown"
and the launch goes on. This feature never writes the identity to a file: no
state file, no event, no log under the project or under `~/.multiagents`.
Verified by: a test on the launch output, a failing identity action, and a
scan of files written during the test for the identity string.

**OA-R4.** Automatic switch. Configuration:
- **The key.** The root's agents.yaml entry may carry `switch_to:`, an ordered
  list of provider names. Each must be a provider of the same CLI as the
  root's own; a mismatch is a config error at load.
- **The default.** Absent or empty means today's behaviour: wait, then
  relaunch on the same provider.

Switching applies only when all three hold:
- **A limit episode.** The limit was detected in the root's own current
  session. The evidence must be newer than this launch: another session's
  transcript does not count, and neither does an old marker in a copied one.
- **A surviving driver.** The driver is still running. Both attached and
  unattended supervision qualify. Under `--no-supervise` the launch line says
  that automatic switching is unavailable.
- **A qualifying candidate.** The candidate's host-profile headroom is
  *known*, not stale, and at or above `quota_handover.reserve_fraction`. Its
  identity, when known on both sides, differs from the exhausted profile's.
  An unknown reading, expired credentials or a failed auth do not qualify.

Then:
- **Choice.** The driver relaunches the root on the first qualifying provider,
  looking at `switch_to` in order and then at the configured provider.
- **One try per episode.** Each provider is tried at most once per limit
  episode. A target that hits a limit again within its first exchange counts
  as tried.
- **Nobody qualifies.** The driver waits for the earliest reset among all of
  them, then re-selects (OA-R2).
- **Spend limits.** A non-resetting spend limit stops the driver, as today.

Profile-dependent driver state (watchdog, compaction, limit detection and
launch arguments) is rebuilt for the target.
Verified by: tests with fake profiles covering:
- the first qualifying candidate is chosen;
- exhausted, unknown and same-identity candidates are skipped;
- the empty list keeps today's behaviour;
- all candidates exhausted leads to a wait and a re-selection;
- an immediate re-limit on the target is not retried in the same episode;
- a stale marker in a copied transcript does not trigger a switch;
- the `--no-supervise` notice;
- a mismatched provider in `switch_to` is a config error.

**OA-R5.** Session continuity. A switch happens only after the source root
process has exited. Then:
- **Install.** The driver installs the session's transcript, and any
  companion files that `--resume` needs, into the target profile's matching
  project directory, then resumes it there.
- **Integrity.** The installation is atomic as a whole. It never overwrites a
  different existing file and never follows a symlink.
- **Concurrency.** It is serialised against any other `multiagents run` that
  is installing into the same profile.
- **Nothing else crosses.** No credential, `.claude.json` or settings data
  crosses profiles.
- **Fallback.** If installation fails, or the target refuses the session, the
  driver starts a fresh session with a new session id. Its first prompt says
  that the session moved for quota and must rebuild its state from BRIEF.md,
  the agent tree, open questions and budget.
- **Notice.** The terminal says which of the two happened.

Verified by:
- missing transcript on the target leads to install, then resume;
- install failure leads to a fresh session with a new id and the recovery
  prompt;
- a same-named different file on the target is left intact;
- a symlink is not followed;
- two concurrent installs into one profile do not interleave.

**OA-R6.** Durable switch record. Before launching the target, the driver
records:
- the source and target providers;
- the session id;
- the switch phase: installing, launched or resumed.

After a crash, a later `multiagents run` reads this record and finishes or
abandons the switch cleanly.

Spend, the context reading, and the provider that `budget_status` and the run
status report for the root move to the target only once the resume is
confirmed. Spend counts each profile's own usage, so copied history is never
counted twice.

agents.yaml is never rewritten. The next manual `multiagents run` starts from
the configured provider, checked as in OA-R1. If that provider's copy of the
session is older than the recorded one, it brings the newer transcript back
first, under the same OA-R5 rules.
Verified by:
- after a switch, the root's reported provider is the target's;
- agents.yaml is byte-identical;
- a driver killed between install and launch is recovered;
- a later run on the configured provider resumes the newer transcript.

**OA-R7.** An exit that is not a detected limit never causes a switch. The
detection itself stays the existing transcript-marker detection, now bound
to the root's own session.
Verified by: a non-limit exit relaunches or stops exactly as today.

**OA-R8.** A root limit pauses only the root's account. Spawned agents whose
runs use a different account under the same provider name keep running.
Verified by: a root limit on the host profile while a fake agent runs on a
vault account of the same provider name. The agent's admission is
unaffected.

**OA-R9.** A switch never changes the host's default login or any profile's
credentials, and never affects another Claude process. It does not log in or
out, and it does not write to any credential file or to `.claude.json` in any
profile. A `claude` started elsewhere on the host keeps its own account. The
only cross-profile write is the transcript installation of OA-R5 and OA-R6,
into this project's directory.
Verified by: a test that hashes both profiles' credential files and
`.claude.json` before and after a switch, and finds them identical.

## Out of scope
- Running the root inside docker, on vault accounts. That is a later phase,
  and nothing here may preclude it.
- Synchronising settings or hooks between host profiles.
- Switching back automatically when an account resets. The next limit episode
  re-selects from the list.
