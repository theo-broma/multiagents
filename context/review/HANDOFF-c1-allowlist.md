# Handoff — C1 allowlist characterization

Written by the orchestrator from `ag-188fa7`'s handoff, which was cut off by a
quota interruption before it could commit anything. Its findings file was never
written, and this is the only record of what it established.

## State

`tests/test_c1_allowlist_characterization.py` is merged: 322 lines, **28 tests,
all green**. `context/review/C1-sandbox-allowlist.md` **does not exist yet** and
must be written from scratch, F10 onward.

## Established against the real code

`write_proxy_config` (`src/multiagents/executor/docker.py:696-708`) escapes only
`.`. Every other character passes through raw into the generated ERE line
`(^|\.)<escaped>$`. That single fact is behind every item below.

The 28 merged tests already pin four classes of consequence — the tests exist,
the findings do not:

- **F10** — bare or short public-suffix entries act as a wildcard.
- **F11** — dead or malformed entries (leading/trailing dot, whitespace, a port,
  a scheme with a path, embedded `^ $ \`) silently match nothing. An allowlist
  entry that admits no host at all, with no error.
- **F12** — unescaped quantifiers `* + ?` change match semantics instead of
  matching literally.
- **F13** — an unbalanced `(` or `[` makes the match *crash* rather than decide.

## Probed and verified, but not yet committed as tests

Run against the real `write_proxy_config` / `allowlist_admits`, not reasoned
about:

| entry | what happens |
|---|---|
| lone `)` | raises `re.PatternError: unbalanced parenthesis` — F13's crash class, reached via `)` rather than `(` or `[` |
| lone `]` | does **not** raise; matches as a literal `]`. A real asymmetry with `[`, worth pinning |
| `a(b)c` (balanced) | valid regex; matches the literal string `abc`, not `a(b)c` |
| `a[bc]d` (balanced) | valid character class; matches `abd` and `acd`, not `a[bc]d` |
| `a{2}b` | matches `aab`. Same family as F12, via the interval quantifier `{n}` |

## What the next run should do

1. **Append** these five cases to `tests/test_c1_allowlist_characterization.py`.
   Do not touch the existing 28.
2. Write `context/review/C1-sandbox-allowlist.md` from scratch, F10 onward,
   covering F10–F13 above plus:
   - **A new finding for the balanced case.** `a(b)c` and `a[bc]d` are neither a
     crash (F13) nor a dead entry (F11): they are valid regexes that silently
     match something other than what the entry says. No error, no crash, no
     empty match — an allowlist rule that quietly means something else. This is
     the most consequential thing left to file, and it is genuinely distinct
     from F1's `|` injection, which widens the allowlist; this one *changes* it.
3. Confirm with `uv run --frozen python -m pytest -q tests/test_c1_allowlist_characterization.py`.

F1 (ERE alternation via `|`) and F2 (uncaught `AttributeError` on a non-string
entry) are already filed. Do not re-file them.

## Harness

No gaps. `c1_harness.py` was sufficient for everything attempted.
