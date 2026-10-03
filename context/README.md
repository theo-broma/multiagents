# context

Reference material agents need that is not code. This directory and `BRIEF.md`
are **committed**: agents work in git worktrees, so anything uncommitted or
gitignored does not exist for them.

Read `../BRIEF.md` first. It carries the decisions; this is the index.

## What is here now

Nothing but this file. The review writes the rest.

## What the review will write here

| path | written by | when |
|---|---|---|
| `review/MAP.md` | `cartographer` | phase 1, once for the whole project |
| `review/<context>.md` | `auditor`, `adversary`, `characterizer`, `harness` | as each context is worked |
| `review/REPORT.md` | `reporter` | phase 6, last |
| `review/C4-runtime-observed.md` | the orchestrator | 2026-09-22, outside the review |

Findings are `F<n>` ids. They go into the ledger through `record_findings`, and
the ledger — not these files — is what you read to decide what to work on. Use
`list_findings` for the index and `read_finding(F12)` for one. **Do not open a
findings file to get an overview**: each holds every finding for a whole context,
and reading it to answer a question about one of them loads all of them.

## The provider plugin seam

The review has a standing invariant to guard — **a provider's logic lives in its
config and its script, never hardcoded in the main program**. `BRIEF.md` carries
the grep, the baseline of five already-argued sites, and the finding worth
making. Both halves of the seam are tracked and readable from a worktree:

- `src/multiagents/defaults/providers.yaml`
- `src/multiagents/defaults/providers/{claude,agy,opencode}.sh`

The live copies under `.multiagents/config/` are gitignored and you will not see
them. Review the tracked defaults; that is where a new provider starts from.

## The Codex provider (Phase 5, from 2026-09-28)

- `specs/codex-provider.md` — requirements and decisions (`CX-*`). Read it
  first.
- `codex-proposal/` — the user's original integration proposal (adapter,
  provider block, examples, 21 offline tests). It is kept verbatim as input.
  It predates sandbox-git, and several of its choices are superseded by the
  spec (profile, quota, launch).

## Phase 6 (from 2026-09-28)

- `specs/phase6-hardening.md` — H1–H13 and D2, in the order to work them.
- `specs/limit-notices.md` — D1, the limit-hit notices the user asked for.

## Phase 6 closing and Phase 7 (from 2026-10-03)

- `specs/phase6-closing.md` — C1–C10, the last items of phase 6.
- `specs/phase7-nodes-and-containers.md` — the seed for phase 7: plan nodes,
  the scheduler script, and one container per run (PAC-R1..R8). To be
  specified with the user before any code is written.

## What is NOT here, and where it lives instead

- **Requirements and specs.** This project does not specify before it builds.
  Requirements are expressed as tests in `tests/test_core.py` and as the entries
  in `docs/open-questions.md`.
- **What this project believes and why** — `docs/open-questions.md`. Each entry
  carries its evidence, its date, and how to check it. Several of those beliefs
  were wrong the first time and the corrections are recorded in place. Read the
  entries for your context before filing a finding against it.
- **Prior design decisions** — `docs/rewrite-plan.md` (what was declined, and the
  measurement that declined it), `docs/superpowers-review.md`.
- **The manual** — `README.md`, 153 KB. It is a reference, not an introduction.
  Send `researcher` at it rather than reading it yourself.
