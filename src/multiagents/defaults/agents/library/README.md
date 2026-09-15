# The agent library

Predefined agents that are **not** in the default roster. Each one has a brief
in this directory and a ready-made `agents.yaml` block below.

The initializer reads this during `multiagents init-agent` and proposes the ones
a project actually needs, into `.multiagents/proposals/agents.yaml`. You accept
the proposal by copying the blocks into `.multiagents/config/agents.yaml`.
Nothing here is active until you do.

The point of the library is that adding a specialist should be a paste, not a
writing exercise. A brief written in a hurry during initialisation is worse than
one that has been used and revised, and a project that needs a pentester needs
roughly the same pentester as the last one did.

**Adding one yourself** is the same paste. The blocks are complete: a provider,
a model, a brief path and the permissions the role needs. Retune the model to
what your subscription actually has — `multiagents refresh-models` lists it.

**Three rules worth keeping when you edit these.** Every agent names a fallback
model on the other provider under `models:`, or it waits instead of failing over
when its provider is exhausted. Where two agents check each other's work, they
stay on different model families — a checker that shares the author's blind
spots agrees with it, which is the one thing it must not do. And every
`opencode-go/*` pin is on a model with a **$60 monthly limit**; the $15 and $30
models drain too fast to run a team on. The allowed list, and why the strongest
opencode models are deliberately not used, is at the bottom of `agents.yaml`.

`reviewer` and `researcher` used to live here and are now in the default team:
review covers a question neither the tester nor the adversary asks, and a cheap
read-only researcher exists to spend its own context instead of the
orchestrator's. Both earned a permanent slot.

---

## `specifier` — numbered requirements before code

Turns an intention into numbered, individually testable requirements in
`context/specs/<feature>.md`. Writes no code.

**Add it when** the domain carries the difficulty — money, regulation, real
invariants, anything where the interesting requirements are never in the
request. Then you want them written by something other than whoever designs the
solution.

Without it the orchestrator writes the contract itself, which is faster and is
the default for good reason: it is the only agent holding the brief, the earlier
phases and your conversation, while a specifier starts cold. Two checks already
stand between that and marking its own homework — the advisor reviews the
contract before anyone builds to it, and the tester reports what it could not
express as a test. This is for when those are not enough.

```yaml
  specifier:
    provider: agy
    model: gemini-3.1-pro-high
    instructions: library/specifier.md
    models:
      opencode: opencode-go/qwen3.6-plus
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
    model: opencode-go/minimax-m3
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
      opencode: opencode-go/qwen3.7-plus
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
    model: opencode-go/glm-5.2
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
