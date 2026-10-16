---
status: applied
applied_in: fa3a5928
---
# User decisions queue, and git-guard reserved domains

Agreed with the user on 2026-10-15, in an init-agent session. The
requirements are in `context/specs/user-decisions-queue.md`: decisions UD-D1
to UD-D4 and requirements UD-R1 to UD-R12. The advisor reviewed the design
(ag-aa1387, turn 26).

## Apply now

**Order.** Phase 7 part 2 is in progress. This plan does not touch
`executor/docker.py`. It touches `tree.py`, `server.py`, `bg.py`, `cli.py`
and the monitor. The orchestrator decides from the locks whether it runs
alongside part 2 or after it. Part 2's RPC transport milestone (IC-R11)
shares `server.py` and the scoped RPC that UD-R4 relies on, so UD-R4's
refusal over the scoped RPC follows IC-R11.

**1. User decisions queue (UD-R1..R12).**
- **The contract** is the orchestrator's: it writes it from the spec,
  citing the UD ids, and puts it to the advisor.
- **Suggested slices:**
  1. the schema, the store and the transitions (UD-R1..R3, R11);
  2. asking, authority and refs (UD-R4, R5, R8);
  3. delivery and wake (UD-R6, R7);
  4. the readers: CLI, MCP, monitor, ntfy (UD-R9, R10);
  5. the instructions (UD-R12).
- **The adversary is mandatory** on slices 2 and 3, the answer authority and
  the delivery. The spec lists the scenarios.
- **The user's standing rules apply:**
  - a reviewer after every implementer;
  - escalation to opus at round 3;
  - waits for the user's validation before committing a spec or contract.

  From the moment `ask_user` merges, those waits go through the queue
  themselves.

**2. Git-guard: exempt reserved example domains (user, 2026-10-14).**
- **The problem.** GG-R3 exempts only `example.invalid`. Every new masking
  test written with an RFC 2606 domain blocks the user's push. On
  2026-10-14 there were 22 findings, all fictional, which the user had to
  allow by fingerprint.
- **The change.** Also exempt the reserved names:
  - `example.com`, `example.net`, `example.org` and their subdomains;
  - the `.example`, `.test` and `.invalid` TLDs (RFC 2606, RFC 6761).
- **The orchestrator writes it** as an amendment to the git-guard spec. The
  initializer did not read that spec this session, so the precise wording
  is the orchestrator's.
- **Unchanged.** Tokens shaped like real credentials (`sk-ant-…`) are not
  exempted by domain: this item is about emails and URLs only.
- **The adversary checks** that no real-looking address slips through. For
  example, `example.com.evil.io`, `corp-example.com` and `example.co` must
  still be reported.

## Next phase

- **The GUI the user imagines** reads and answers the queue through UD-R9's
  API. It is not specified yet.
- **A channel that proves a human answered** (UD-R4 residual). This is only
  if the user wants it.

## Config changes

- **None applied by this plan.**
- **Proposed, to be applied by the user,** in the untracked
  `.multiagents/config/project.yaml`: add 19 fingerprints for fictional test
  data to `git.guard.allow_fingerprints`. They come from commits ad5eaf7,
  cc1617c, fbd5d27 and 167a131, and block the push today. The classifier
  refused the edit to the initializer, so the user runs it. Item 2 avoids
  this next time, but these fingerprints stay needed: the commits are
  already in history.

## Notes considered

`context/notes/` contains only README.md: there are no user notes.
