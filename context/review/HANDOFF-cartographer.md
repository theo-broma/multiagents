# Handoff — cartographer, interrupted 2026-09-16 (claude quota)

## Done
`context/review/MAP.md` is complete and committed (af03bb8). Seven contexts,
ranked, with budgets. It is the deliverable and it does not need redoing.

All measurements behind it are real, not estimated:
- fan-in: AST walk over every import in the package
- coverage: a real instrumented run (`--with pytest-cov`), per module
- churn: `git log --name-only` over the full branch history
- size: `wc -l`

## Left / unverified — one thing only
Running `-p no:randomly --cov=multiagents` gave **17 failed, 528 passed, 3
skipped**, not the documented 545/3. Mostly `PermissionError`. I was cut off
while diagnosing it. MAP.md's last section records it as an open question with
the exact two commands needed to settle it.

My read, unconfirmed: it is **test-order dependence, not a code defect**.
`pytest-randomly` is active by default; I disabled it for reproducible coverage,
pinning execution to file order. Several failing test names suggest they chmod a
directory read-only and leak it into the next test. If confirmed, that is a real
finding about the suite, in the same family as the isolation finding the BRIEF
already documents.

To settle it in two runs:
```
uv run --frozen python -m pytest -q                    # expect 545 passed, 3 skipped
uv run --frozen python -m pytest -q -p no:randomly     # no --cov: isolates ordering
```
If the first passes and the second fails, it is ordering. Do not file it before
running both — the BRIEF warns twice that failures here are usually environmental.

## Worked out along the way, not obvious from the diff
- **Churn is nearly useless as a ranking signal on this repo.** Whole history is
  150 commits, 1 author, 11 days. Nothing is cold. I ranked on fan-in +
  irreversibility + coverage instead, and said so in the map. Do not "fix" the
  ranking by reweighting toward churn.
- **Coverage does the job churn normally does** — it separates cleanly, 96% down
  to 36%, and it is the strongest signal in the map.
- **`pytest-cov` is NOT a project dependency.** I added it transiently with
  `uv run --with pytest-cov`. Nothing was committed to add it. Anyone
  re-measuring needs the same flag.
- **`sys` at the repo root is a tracked 64 MB ImageMagick PostScript file**
  (created 2026-09-14), 99.6% of the repo's bytes, almost certainly a shell
  redirect typo. Worth a finding; writing findings was not my job.
  **Warning:** it showed up as modified (`M sys`) during my session without my
  writing to it — I restored it with `git checkout -- sys` before committing.
  Check `git status` for it before you commit anything, and do not commit a
  64 MB binary diff by accident. Why it changed is itself unexplained and may
  be worth a ticket.
- **The BRIEF's four suggested groupings survive measurement, mostly.** Its
  docker+authproxy, budget+providers, and runner+driver groupings are all
  supported. Its `tree.py` as a standalone risk is the one that does not:
  `tree.py` is 91% covered, the well-tested half of C3, so concurrent state is
  better defended than the brief assumes. The untested risk in that context is
  `runner.py` (61%, 435 uncovered statements) and `gitops.py` (77%).
- **`cli.py` ranking sixth is deliberate and measured**, not an oversight — it
  is the largest and highest-churn file in `src/` with fan-in 1.
- **If budget allows exactly one change to the map, split C2.** A reviewer given
  C2 will spend everything on `budget.py` and never reach `auth.py` (77%,
  credential handling). Same failure mode for `gitops.py` inside C3.

## Environment note
The container hit `fork: Resource temporarily unavailable` repeatedly near the
end — it cost me one commit attempt (empty message, recovered). If the next run
sees odd shell failures, it is process exhaustion, not the code.
