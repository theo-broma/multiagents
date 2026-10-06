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
| `tailnet-host` | a `*.example.ts.net` host name that is not a placeholder (one containing `<`) |
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
