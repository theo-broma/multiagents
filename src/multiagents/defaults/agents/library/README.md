# The agent library

Predefined agents that are **not** in the default roster. Each one has a brief
in this directory and a ready-made `agents.yaml` block below.

The initializer reads this during `multiagents init-agent` and proposes the ones
a project actually needs, into `.multiagents/proposals/agents.yaml`. You accept
the proposal by copying the blocks into `.multiagents/config/agents.yaml`.
Nothing here is active until you do.

The point of the library is that adding a specialist should be a paste, not a
writing exercise. A brief written in a hurry during initialisation is worse than
one that has been used and revised, and a project that needs a reviewer needs
roughly the same reviewer as the last one did.

**Adding one yourself** is the same paste. The blocks are complete: a provider,
a model, a brief path and the permissions the role needs. Retune the model to
what your subscription actually has — `multiagents refresh-models` lists it.

**Two rules worth keeping when you edit these.** Every agent names a fallback
model on the other provider under `models:`, or it waits instead of failing over
when its provider is exhausted. And where two agents check each other's work,
they stay on different model families — a checker that shares the author's blind
spots agrees with it, which is the one thing it must not do.

---

## `specifier` — numbered requirements before code

Turns an intention into numbered, individually testable requirements in
`context/specs/<feature>.md`. Writes no code.

**Add it when** the project is one where features are specified before they are
built, and you want the requirements written by something other than whoever
designs the solution. Without it the orchestrator writes the contract itself,
which is faster and grades its own homework.

```yaml
  specifier:
    provider: agy
    model: gemini-3.1-pro-high
    instructions: library/specifier.md
    models:
      opencode: opencode-go/deepseek-v4-pro
    description: >-
      Turns an intention into numbered, individually testable requirements in
      context/specs/<feature>.md. Writes no code.
    writes: true
    permission: sandbox
    effort: high
    can_spawn: false
    timeout: 1200
    silence_timeout: 240
```

## `spec-adversary` — attacks the spec, before anything exists

Appends concrete failure scenarios (`A1`, `A2`, …) to a spec file and proposes
no fixes. The team's `adversary` attacks written code; this one attacks the
words, one stage earlier, where a missing requirement is still free to add.

**Add it with `specifier`** — it is the other half of that pair and it is on a
different model family on purpose.

```yaml
  spec-adversary:
    provider: opencode
    model: opencode-go/gpt-5.6-luna
    instructions: library/spec-adversary.md
    models:
      agy: claude-sonnet-4-6
    description: >-
      Attacks a spec with concrete failure scenarios before implementation.
      Appends them to the spec file; proposes no fixes.
    writes: true
    permission: sandbox
    effort: high
    can_spawn: false
    timeout: 1200
    silence_timeout: 240
```

## `reviewer` — reads a diff, reports defects

Correctness review with a location, a concrete failure and a machine-read
verdict. No writes, no fixes.

**Add it when** you want a correctness pass the orchestrator is not doing
itself — a large team, a long session, or a project where the orchestrator's
context is the bottleneck. The default team folds this into Phase 6, which is
cheaper and less thorough.

```yaml
  reviewer:
    provider: agy
    model: gemini-3.1-pro-high
    instructions: library/reviewer.md
    models:
      opencode: opencode-go/gpt-5.6-luna
    description: Reviews a diff for correctness and reports findings. No writes.
    writes: false
    permission: readonly
    effort: high
    can_spawn: false
    timeout: 900
    silence_timeout: 180
```

## `researcher` — reads widely, reports narrowly

Answers questions about a codebase, burning its own context instead of its
parent's.

**Add it when** the codebase is large or unfamiliar enough that "how does X
work here?" is a real question. It is cheap, read-only, blocks nothing and can
run alongside anything.

```yaml
  researcher:
    provider: opencode
    model: opencode-go/glm-5.3-flash
    instructions: library/researcher.md
    models:
      agy: gemini-3.8-flash-medium
    description: Reads the codebase and answers questions. No writes.
    writes: false
    permission: readonly
    can_spawn: false
    timeout: 600
    silence_timeout: 120
```

## `security-advisor` — consulted while the design can still move

Design-time security advice, phrased as candidate requirements so a concern
becomes a numbered requirement and then a test. Conversational: a threat model
is built through follow-ups, not one-shot questions.

**Add it when** the project handles credentials, personal data, money, tenancy,
or input from outside the system — and add it at the start, since its value is
that a boundary can still be moved for free.

```yaml
  security-advisor:
    provider: agy
    model: gemini-3.1-pro-high
    instructions: library/security-advisor.md
    models:
      opencode: opencode-go/gpt-5.6-luna
    description: >-
      Design-time security advice, phrased as candidate requirements the
      specifier can absorb. Advises; decides nothing.
    conversational: true
    writes: false
    permission: readonly
    effort: high
    can_spawn: false
    timeout: 420
    silence_timeout: 180
```

## `pentester` — attacks existing code from an attacker's position

Reports the attacker's position, the path and what they get. May commit a test
that fails now and passes once fixed.

**Add it when** security is a first-class concern rather than one of several —
the team's `adversary` already attacks code from an attacker's position as part
of its remit, so this is for projects that want a dedicated, deeper pass. Keep
it on a different provider from `security-advisor`: the audit should not be done
by whoever approved the design.

```yaml
  pentester:
    provider: opencode
    model: opencode-go/deepseek-v4-pro
    instructions: library/pentester.md
    models:
      # effort: high does not survive the move to agy for this model.
      agy:
        model: claude-opus-4-6-thinking
        effort: ""
    description: >-
      Attacks existing code for exploitable paths and reports position, path and
      outcome. May commit a failing test that proves a finding.
    writes: true
    permission: sandbox
    effort: high
    can_spawn: false
    timeout: 1500
    silence_timeout: 240
```
