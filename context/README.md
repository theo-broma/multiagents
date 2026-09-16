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

Findings are `F<n>` ids. They go into the ledger through `record_findings`, and
the ledger — not these files — is what you read to decide what to work on. Use
`list_findings` for the index and `read_finding(F12)` for one. **Do not open a
findings file to get an overview**: each holds every finding for a whole context,
and reading it to answer a question about one of them loads all of them.

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
