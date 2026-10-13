---
status: ready
---
# Review findings, C1 and C2, part 1: the high-severity ones still open

The initializer triaged these with the user on 2026-10-13, after the
stabilisation batch. The six findings `REPORT.md` leads with were already
fixed. The seven `open` high-severity findings were each checked against
main at fe47dc15, because ST1, ST2 and TF had changed that code.

**Already recorded in the ledger** by the initializer, at the user's
validation:
- **`fixed`:**
  - F60: `stop()` includes the auth container, `executor/docker.py:3014`;
  - F61: ST2 checks every network connect, `docker.py:2569-2571`;
  - F170: TF-R5, `tests/test_tf_consult.py`, 8 passed.
- **`scheduled`,** pointing to this plan: F24, F30, F62, F121 and F135.

## Apply now

Each item goes through the standing pipeline: tester, implementer, then a
reviewer on every implementer run. Items 1 to 3 touch the sandbox (C1), so
a single adversary pass covers them before merge. The orchestrator writes
the contract, citing these ids, and may group items that touch the same
files: items 2 and 3 are both in `executor/docker.py`.

**1. F24 (and its duplicate F71): `_scrub` passes a non-JSON error body
through untouched.** Still present at `authproxy.py:488-489`. A secret in an
HTML error page reaches the agent's container.
- **The fix.** When the body is not JSON, apply the text redaction from
  `redact.py` instead of returning the bytes as they are.
- **What it must not do.** It must not break a body that is not UTF-8.
- **Note.** The characterization test
  `test_scrub_falls_back_to_original_bytes_on_non_json_error_body` pins the
  defect. A tester reverses it deliberately, the way F150 was handled.

**2. F30: a `read_only` mount on the same path as the project root is
silently made writable.** The deduplication in `DockerExecutor.mounts()`
keeps the first entry, which is the writable root.
- **The fix.** A collision is never resolved by dropping `read_only` without
  a word. The contract chooses one of two behaviours:
  - read-only wins;
  - the config is refused with a message.
- **Tests.** The characterization test
  `test_extra_mounts_read_only_is_silently_dropped_when_path_collides_with_root`
  is reversed the same way.
- **Part 2.** Phase 7 part 2 will rewrite the mounts. The rule decided here
  carries over.

**3. F62: `ensure_auth_proxy` does not check `docker network connect
bridge`** (`docker.py:2718`) and still answers `ok`. The fix is the same as
the one ST2 made for F61: check the code, remove the half-created
container, and return the error.

**4. F121: `choose_provider` treats an unread provider differently by
position.**
- **The user's decision (2026-10-13).** A provider whose quota was never
  read is **usable in both positions**, preferred and fallback. This is
  consistent with the module's rule, "unknown headroom is not no headroom".
- **The reason must be honest.** It says there is no reading, never "has
  headroom".
- **Ordering.** A provider with a real reading and headroom is still
  preferred to an unread one in the same tier.
- **Tests.** The three characterization tests named in F121 are brought in
  line with the decision.

**5. F135: `opencode.sh check` misses a credential count at the start of
the output.** Example: `1 credentials`. The sed at `opencode.sh:278` needs a
non-digit before the number.
- **The user's decision (2026-10-13).** A count at line start parses
  normally. Only output that contains no count at all stays `unknown`
  (exit 20).
- **This supersedes** the older AU reading in
  `context/specs/auth-checks-uncertain.md:33`. A tester reconciles
  `tests/test_c2_auth_characterization.py:560` with it.
- **Done when** `tests/test_zai_provider.py` and `tests/test_au_*.py` are
  green.

**6. Clean up the nodes left over.**
- `nd-74628b22` (the old F135 attempt, held on the AU conflict) and its
  reviewer `nd-65004b6a`, under `nd-b6b90957`: cancel or dispose them.
  Item 5 replaces them.
- `nd-7866cea2` (the SL-R10 revision race, held): it is obsolete. SL is
  merged and ST7 fixed the `nc_m1_adv3` flake. Dispose it.
- Also the unmerged leftovers that the stabilisation handoff lists:
  - `nd-0950c9d2`, which must never be merged;
  - the tester and adversary nodes;
  - `/var/tmp/main-host`.

**7. When everything has merged,** set each finding to `fixed` in the
ledger, citing the merge commit.

## Next phase

- **The remaining `open` findings** (41, medium and low). The initializer
  proposes decisions to the user in groups, in the next session.
- **Phase 7 part 2: one container per run.** It is specified with the user
  once this plan and the triage are done. The scope is as recorded in the
  plan of 2026-10-12.

## Config changes

- **None applied by this plan.**
- **Already applied at the user's request (2026-10-13),** in the untracked
  `.multiagents/config/project.yaml`: the fingerprint `0ff6772a7ab4960b` was
  added to `git.guard.allow_fingerprints`. It is `*.c.ts.net`, Tailscale's
  public rule in `public_suffix_list.dat`, not a tailnet.

## Notes considered

`context/notes/` contains only README.md: there are no user notes.
