# Agent briefs

Every agent in `agents.yaml` names a brief in here with its `instructions:` key.
The path is relative to this directory and resolves across all three config
layers — shipped defaults, then `~/.config/multiagents/`, then the project's
`.multiagents/config/` — so overriding a shipped brief means dropping a file
with the same relative path into a later layer. Nothing is copied over: the
first layer that has the file wins, project-first.

Three buckets:

## `team/` — the default roster

The six roles that ship active, and the pipeline they run.

| Brief | Agent | Mandate |
| --- | --- | --- |
| `_initializer.md` | `initializer` | Converses with the user, writes `BRIEF.md`, proposes the team and the model for each role. Launched by `multiagents init-agent`. |
| `_orchestrator.md` | `orchestrator` | Drives the project to completion. Makes every architectural and delegation decision; writes the interface contracts; never writes implementation code. Launched by `multiagents run`. |
| `advisor.md` | `advisor` | Second opinion for the drivers. Reads the code, analyses proposals and prompts, offers alternatives. Decides nothing, changes nothing. |
| `tester.md` | `tester` | Writes the black-box behavioural test suite from the orchestrator's contract, before any implementation exists. Defines what done means. |
| `implementer.md` | `implementer-quick`, `implementer`, `implementer-deep` | Writes the code that makes the suite green. Three tiers on one brief, routed by how much judgement the task needs. The tests are read-only to them. |
| `adversary.md` | `adversary` | Attacks the green code — mutation, fuzzing, untested inputs, interleaving, the attacker's position. Breaks it; fixes nothing. |

The two leading-underscore briefs are mandatory: the drivers will not run
without them, and the underscore is there to say they are not yours to delete.

## `library/` — predefined, not active

Specialists a project can add: `specifier`, `spec-adversary`, `reviewer`,
`researcher`, `security-advisor`, `pentester`. Each has a brief here and a
paste-ready `agents.yaml` block in `library/README.md`.

The initializer reads that README during `multiagents init-agent` and proposes
the ones this project needs. Nothing here runs until an entry for it exists in
`agents.yaml`.

## Here, at the root — infrastructure

`bug-reporter.md` is neither. It writes up defects in **multiagents itself**,
not in your project, and it ships active because a system that cannot report its
own bugs never gets fixed. It is not part of the six-phase pipeline.

It cannot read the multiagents source, and its brief says so rather than
pretending otherwise: it runs in a worktree of *your* project, its file tools are
confined to that worktree, and under the docker executor the source is not
mounted in the container at all. It used to be told to read the source and cite a
function and a line, which bought nothing but a paragraph of apology in every
ticket. The generated environment block carries the commit hash instead, which is
what actually lets a maintainer open the code. *What would change this:* mounting
the source read-only and granting it with `--add-dir` — possible for agy and
claude, not for opencode, which takes a single `--dir`.
