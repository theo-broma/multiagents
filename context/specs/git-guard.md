# Nothing sensitive leaves through a push (GG)

Source: user, 2026-10-06.
- After personal data reached the public history: "il faut absolument retirer
  toute trace de données personnelles du git public".
- "Je voudrais donc un agent 'git' qui soit appelé pour toutes les tâches git,
  et qui vérifiera entre autres qu'aucun élément sensible ne passe un push".
- "Il faudra aussi une mention dans la config pour activer/désactiver le fait
  que le modèle de l'orchestrator soit mentionné comme co-auteur, et cet agent
  devra vérifier si cette règle est respectée".

Two layers:
- a deterministic guard (this spec), which no model can talk its way past;
- the `git` agent (`agents/library/git.md`), the judgement on top of it.

Examples in this spec, its tests and shipped files use placeholders only.

## Configuration

```yaml
git:
  coauthor_orchestrator: true   # name the orchestrator's model as co-author
  guard:
    patterns_file: ~/.config/multiagents/sensitive-patterns   # private, never in the repo
    allowed_emails: []          # besides the repo's own user.email and agent identities
    allow: []                   # literal strings that never count as a finding
```

## Behaviours

**GG-R1. Config.**
- `git.coauthor_orchestrator` is a boolean and defaults to `true`.
- `git.guard.patterns_file` defaults to
  `~/.config/multiagents/sensitive-patterns`.
- `git.guard.allowed_emails` and `git.guard.allow` are lists of strings,
  defaulting to empty.

Other types are refused at load, the way existing invalid settings are.
Verified by: config tests.

**GG-R2. The orchestrator is told the rule.** The orchestrator's composed
instructions state the co-author rule from the config:
- when true, end every commit message with one `Co-Authored-By:` trailer naming
  its model;
- when false, never add one.

Changing the value changes the composed text at the next launch.
Verified by: a test composing the brief under both values.

**GG-R3. `multiagents git-guard scan [RANGE]`.**
`RANGE` defaults to `<remote>/<base>..<base>`. The scan covers every commit in
the range:
- every added line of every changed file, binary files included as raw bytes;
- the commit message;
- the author and committer name and email;
- added or renamed paths.

A finding is any of:

| category | what it catches |
|---|---|
| `email` | an email address that is none of: the repo's `user.email`, an entry of `allowed_emails`, an agent identity (`*@multiagents.local`, `*@multiagents.invalid`), a `noreply` address, or an address at `example.com/.org/.net/.invalid` |
| `tailnet-ip` | an IPv4 address in `100.64.0.0/10` |
| `tailnet-host` | a `<name>.ts.net` host name that is not a placeholder (one containing `<`; see GG-R7) |
| `private-key` | a `-----BEGIN … PRIVATE KEY-----` block |
| `token` | a string shaped like a known credential: GitHub `ghp_`/`gho_`/`github_pat_`, Anthropic `sk-ant-`, OpenAI-style `sk-` followed by 20+ characters, Slack `xox[abpr]-`, AWS `AKIA` + 16 characters |
| `private` | any line of `patterns_file`, matched case-insensitively. A line is a literal string, unless it starts with `re:`, in which case the rest is a regular expression; blank lines and `#` lines are ignored |
| `coauthor` | when `coauthor_orchestrator` is false, a `Co-Authored-By:` trailer naming an AI model (Claude, Opus, Sonnet, Haiku, Fable, GPT, Codex, Gemini) |

A match equal to an `allow` entry is not a finding.

Output:
- one line per finding: category, short commit SHA, and where it was found
  (`path:line`, `message`, `author` or `path-name`);
- the match is masked, showing its first 2 characters followed by `…`;
- the unmasked match never appears in any output or log.

Exit codes: 0 for no findings, 1 for findings, 2 for a usage or git error.
- A missing `patterns_file` is not an error: the scan says the private patterns
  were not checked and runs the built-in checks.
- A `patterns_file` readable by group or others is refused (exit 2) with a
  message.

Verified by: tests on throwaway repos built in `tmp_path`, one per category,
plus allow, masking, exit codes and the patterns-file cases.

**GG-R4. `multiagents git-guard install`** writes the repository's
`.git/hooks/pre-push` hook:
- The hook reads git's pre-push input and runs the GG-R3 scan on exactly the
  commits each pushed ref would add to the remote. For a new remote branch,
  that is the commits not reachable from any remote-tracking ref.
- The push is refused when the scan finds anything or fails.
- `install` is idempotent.
- It refuses to replace a pre-push hook that it did not write, unless given
  `--force`, which keeps the old hook as `pre-push.local` and chains to it.
- `git-guard uninstall` removes only a hook it wrote.

Verified by: tests that push to a local bare remote; a clean push succeeds, and
a push carrying each category is refused with the remote unchanged.

**GG-R5. `push_branch` runs the guard.** Before pushing, the `push_branch` MCP
tool runs the GG-R3 scan on the commits the push would add.
- On findings it refuses with `{"ok": false, "reason": "guard", "findings":
  [...masked...]}`, and the remote is untouched.
- No parameter skips the scan.

Verified by: a `push_branch` test against a local bare remote.

**GG-R6. The shipped `git` library agent.**
`src/multiagents/defaults/agents/library/git.md` holds the generic brief:
- the audit procedure;
- the co-author rule check;
- never pushing, and never moving a project ref;
- masked reporting.

It names no user, host or account.
Verified by: a test that the library lists `git` and that the file contains
no address outside the placeholders above.

## Out of scope
- Rewriting existing history. That is a one-off operation done with the `git`
  agent, outside this spec.
- Scanning branches that are never pushed.
- A server-side GitHub check.

## Clarifications (2026-10-06, answering tester ag-da44e5)
- **Remote.** `git.remote` may be a remote name or a path/URL; both are supported.
  - With a name, the default range and the "already published" set come from its remote-tracking refs.
  - With a path or URL, they come from the remote's refs as `git ls-remote` reports them.
- **Committer.** A finding in the committer identity is labelled `committer`.
- **Edge cases.**
  - An invalid `re:` line in the patterns file makes the scan exit 2 with a message naming the line number, without printing the pattern.
  - The `Co-Authored-By` keyword matches case-insensitively.
  - A finding in a binary file is located as `path:bin`.
- **Hook.**
  - `install` writes a hook that invokes the same multiagents installation by absolute path, and works under the environment git passes it.
  - A refused `install` exits 1.
  - `uninstall` with no hook of ours exits 0 with "nothing to remove". After `install --force`, `uninstall` restores `pre-push.local` as `pre-push`.
- **`push_branch`.** A refusal returns `{"ok": false, "reason": "guard", "findings": [...]}`, and `pushed` is never true. A success keeps today's fields and adds `"ok": true`.
- **Library tests.** `tests/test_core.py::test_every_library_agent_ships_a_brief_and_a_pasteable_block` and `test_every_library_brief_is_listed_in_the_readme` pin four library agents. GG-R6 makes it five, so they are updated deliberately by a tester.

## Decisions after review ag-7957dd (2026-10-06)
- **Email exemptions stay exactly as GG-R3 lists them.** The implementation exempted the whole `.invalid` TLD; that is withdrawn, because an address like `<first>.<last>@<company>.invalid` would leak a name. Existing fixtures that commit as a one-letter address at `e.invalid` (the h1 and h3 tests) are changed deliberately by a tester to an exempt address at `example.invalid`.
- **Line-wrapped secrets are out of scope.** Detection is per line by design. A private key is caught by its `BEGIN` line, and a token split across lines is not usable as written. Recorded as a known limit.

## Extension (2026-10-06): placeholders, fingerprints, reporting

Source: user, 2026-10-06, after the first real scan stopped a push on 55
fictional fixtures. The answer was that placeholders must be exempted by the
guard, there must be a local allow list, and the reasons for a block must
reach the orchestrator, who reports them in the chat so the user can add them
to the allow list if they want to.

**GG-R7. Placeholders the guard recognises.** None of these is a finding:
- `tailnet-host`: a `*.example.ts.net` name that contains `<`, as before, or whose
  label just before `ts.net` is `example`, as in `phone.example.ts.net`.
- `tailnet-ip`: the CIDR text `100.64.0.0/10`. A bare address in the range is
  still a finding.
- `private-key`: a `BEGIN … PRIVATE KEY` line whose key type is a placeholder,
  meaning it contains `…` or `<`. A real armour line never contains either.

Existing fixtures that use other fictional names are changed by a tester to
these forms: MT test hosts become `*.example.ts.net`, and the MT IP literal
moves to a documentation range (`192.0.2.0/24`). The current spec texts are
changed to placeholders. History that still carries the old forms is handled
by the local allow list (GG-R8), not by widening the exemptions.
Verified by: one test per exemption, plus one showing that the near miss is
still a finding (`phone.example.ts.net`, `192.0.2.2`, a real armour line).

**GG-R8. Fingerprints and the local allow list.**
- Every finding carries a `fingerprint`: an HMAC-SHA256 of the exact match,
  keyed by a per-user secret and truncated to 16 hex characters. The key is
  created on first use at `$XDG_STATE_HOME/multiagents/guard-key`, mode 0600 in
  a 0700 directory. The fingerprint never reveals the match to someone without
  the key, and it is stable for the same match on the same machine.
- `git.guard.allow_fingerprints: []` is a new list of strings, validated like
  `allow`. A finding whose fingerprint is listed is not a finding.
- The scan output gives one line per finding: category, short SHA, location,
  masked match and fingerprint. When there are findings, it ends with a block
  the user can paste into `git.guard.allow_fingerprints`. The scan never writes
  the config itself.
Verified by: tests that the same match gives the same fingerprint and a
different key gives a different one; that a listed fingerprint is not
reported; that the key file and directory modes are as stated; and that the
unmasked match appears in no output.

**GG-R9. A block is reported to the user.**
- A `push_branch` refusal returns each finding with category, commit,
  location, masked match and fingerprint.
- The orchestrator's composed instructions (GG-R2) tell it, on a guard
  refusal, to report in the chat a table of the findings (category, commit,
  location, masked match, fingerprint), saying which ones look fictional and
  which look real. The orchestrator never adds an entry to `allow` or
  `allow_fingerprints` itself: the user decides.
- The `git` library agent's brief says the same for its audits.
Verified by: a `push_branch` test asserting the fingerprint field; a brief
composition test asserting the reporting rule; and a library test asserting the
rule in `git.md`.
