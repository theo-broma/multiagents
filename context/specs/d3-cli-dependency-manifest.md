# D3: per-provider CLI dependency manifest, the contract

**Status:** contract, written by the orchestrator on 2026-09-29.
- **User's request:** each provider's "plugin directory" gets a file that
  describes in detail everything the plugin depends on in its CLI, so that
  hunting for bugs caused by CLI updates can be automated.
- **Inputs:**
  - the inventory in `context/specs/d3-cli-inventory.md` (ag-a609b9);
  - the advisor's points (ag-8e7d87, recorded in BRIEF handoff #2).
- **Ids:** `DM-R*`. They are never renumbered. A behaviour is retired by
  marking it withdrawn.

## What this is, and what it is not

- **It is** a machine-readable declaration of each provider's
  *native* CLI surface that multiagents relies on. It comes with:
  - a static lint that ties each declaration to the code or config that
    uses it;
  - a `doctor` section that probes the real binary and compares its
    version with the versions recorded as verified.
- **It is not:**
  - a compatibility proof;
  - a completeness proof;
  - a version pin that refuses to run.

  A CLI version that is newer or older than a verified one is reported
  as *unverified*. It is never refused.

## Behaviours

**DM-R1: location and layering.**
- **Where it lives.** A provider's manifest is
  `<provider>.dependencies.yaml` in a `providers/` directory, beside the
  adapter or scripts.
- **Which layer wins.** It is resolved with the same precedence as the
  adapters and scripts (`scripts.py` ~64–83): project
  `.multiagents/config/providers/`, then global
  `~/.config/multiagents/providers/`, then shipped
  `src/multiagents/defaults/providers/`. The first layer that has the
  file wins.
- **The winning document replaces the others whole.** The layers are not
  merged key by key, with one exception: a document with
  `extends: <layer>`, where the layer is `shipped` or `global` and is
  lower than its own.
  - Such a document starts from that layer's manifest.
  - Its own `dependencies` entries then replace the entries with the
    same `id`, or add new ones.
  - An entry `{id: X, withdrawn: true}` removes X.
  - Its `verified` list, when present, replaces the inherited one.
  - `extends` naming its own layer or a higher one is a malformed
    manifest (DM-R6).
- **Shipped manifests.** There is one for each provider in the shipped
  `providers.yaml`: claude, codex, opencode and agy.
- **A custom provider with no manifest in any layer** is valid.
  - `doctor` reports it as `no manifest` for information: it is not a
    problem and does not change the exit status.
  - The lint skips it.
- **Verified by:**
  - one test per layer order, using temporary global and project config
    dirs;
  - a test of `extends` replacing, adding and withdrawing entries;
  - a test for a custom provider with no manifest.

**DM-R2: the document schema.** A manifest is YAML with these keys:
- `schema: 1`. Required. Any other value is malformed.
- `provider`: the provider name. It must equal the filename's prefix.
- `binary`:
  - `name`: the executable name;
  - `version_command`: an argv list run *after* the binary, e.g.
    `["--version"]`;
  - `version_regex`: a Python regex whose first group is the version
    string.
- `dependencies`: a list of entries. Each entry has:
  - `id`: stable, unique within the manifest, of the form
    `<kind>.<slug>`, e.g. `flag.output-format`,
    `stream.result-usage`, `env.claude-config-dir`.
  - `kind`: one of `subcommand`, `flag`, `env`, `state_path`,
    `stream_field`, `text_match`, `exit_code`, `endpoint`, `layout`.
  - `value`: the literal the CLI must keep honouring. That can be a
    flag, a variable name, a path relative to the CLI's home, an event
    type or field path, a matched string or regex, an exit code, a URL,
    or a directory depth.
  - `used_by`: a non-empty list of references (DM-R4).
  - `note`: optional free text.
- `not_dependencies`: optional. A list of `{ref, reason}` for leaves
  that reverse coverage (DM-R5) would otherwise demand.
- `verified`: a list, possibly empty (DM-R3).
- `extends`: optional (DM-R1).

**Only native CLI dependencies go in `dependencies`.** These are not
the CLI's surface; they are multiagents' own protocol, so they are
excluded:
- the `MULTIAGENTS_*` variables passed between the runner and an
  adapter, including `MULTIAGENTS_CODEX_PROFILE`;
- the script exit-code contract of `scripts.py` (0/10/20/64);
- the adapter interface.

An entry whose `value` names a `MULTIAGENTS_*` variable is a lint
finding.

Verified by: schema tests that accept a minimal valid document and
reject each missing or ill-typed required key as malformed.

**DM-R3: what "verified" means.** Each `verified` entry records:
- `version`: the exact version string, as `version_regex` extracts it;
- `date`: in ISO form;
- `platform`: e.g. `linux-x86_64`;
- `executor`: `local` or `docker`;
- `evidence`: free text naming what was run, e.g. a run id or a
  command;
- `scope`: `all`, or a list of dependency ids exercised;
- `integration_digest`: the digest of the integration code the version
  was exercised against (DM-R3a).

Two things never count as verification:
- `--version` output alone, which is only discovery;
- tests against a fake CLI, which prove the adapter's behaviour, not
  upstream compatibility.

The shipped manifests start with `verified: []`. Entries are added
later, by the orchestrator, from real runs. The implementer does not
invent them.

**DM-R3a: the integration digest.**
- `integration_digest(provider, paths) -> str` is a sha256 over the
  following, in a fixed and documented order:
  - the *resolved* adapter and scripts of that provider, whichever
    layer each comes from (DM-R1);
  - the provider's entry in the effective `providers.yaml`, serialized
    canonically: sorted keys, and no dependence on comments or
    formatting.
- A `verified` entry applies only when its `integration_digest` equals
  the current digest.
- **Overrides.** When the adapter, a script or the `providers.yaml`
  entry is overridden by a project or global layer, the digest changes,
  so the shipped `verified` entries stop applying. The status is then
  `overridden`, not `verified`.
- `doctor` shows provenance, meaning the layer and path:
  - of the manifest;
  - of each piece of integration code.
- `doctor` prints the current digest, so that a person recording a
  verification can copy it.

Verified by:
- the digest is stable across two calls;
- it changes when a project-layer adapter override is added;
- it is unchanged by a comment-only edit to `providers.yaml`, and
  changed by a value edit.

**DM-R4: references resolve to executable code or config.**

Each `used_by` reference takes one of these forms:
- `providers.yaml:<provider>.<dotted.key>`: a key path inside that
  provider's entry in the *shipped* `providers.yaml`.
- `<repo-relative .py path>::<qualified.name>`: a function, method or
  class, found through the `ast` module.
- `<repo-relative .sh path>::<function>`: a shell function defined in
  that file as `name()` or `function name`.

A reference **resolves** when two conditions hold:
- the target exists;
- the entry's `value` occurs inside the target's executable content,
  that is:
  - for YAML, the value at that key path, or anywhere beneath it;
  - for Python, a string constant in that definition's body, docstrings
    excluded;
  - for shell, the function body with comment lines and trailing `#`
    comments removed.

A reference that resolves only through a comment, or only to a
substring of some unrelated identifier, does not resolve. The lint's
own tests prove this.

Verified by lint tests using fixture files:
- a reference that resolves;
- one whose target is missing;
- one whose value occurs only in a comment;
- one whose value occurs only in a docstring.

**DM-R5: the lint.**
- **The function.** `lint(paths=None) -> list[LintFinding]` checks
  every provider that has a manifest.
  - A finding has `provider`, `dependency_id` (or null), `code` and
    `message`.
  - `code` is one of `malformed`, `unresolved_ref`, `internal_protocol`,
    `duplicate_id`, `uncovered_leaf` and `stale_not_dependency`.
- **Forward.** Each `used_by` reference must resolve (DM-R4).
- **Reverse coverage, where it can be extracted mechanically.** Every
  scalar leaf under the following keys of the provider's shipped
  `providers.yaml` entry must be covered, either by some dependency's
  `used_by` or by a `not_dependencies` entry:
  - the command or argv templates;
  - the stream or event field maps;
  - `status_map`;
  - `refusal_markers`;
  - limit or quota message lists;
  - `home_links`;
  - `bin_versions_depth`.

  The implementer lists the exact key set in the lint module's
  docstring. That list must match what the shipped file has.
  - A `not_dependencies` entry whose ref no longer exists is
    `stale_not_dependency`.
  - Reverse coverage of Python and shell code is **not** required, and
    the lint does not claim it.
- **The suite test.** `tests/test_d3_manifest_lint.py` runs `lint()` on
  the shipped tree and asserts that there are zero findings. This is
  the test that ties declarations to code. A CLI-surface change that
  forgets its manifest now fails the suite.
- **What the lint may not print.** Its output never claims completeness
  or compatibility. It says "declared dependencies resolve", not "all
  dependencies are covered".
- **The shipped manifests** are populated from
  `d3-cli-inventory.md`, and pass the lint.

**DM-R6: `doctor` probes the real binary in each execution context.**
- **Where.** A new section, `cli dependencies`, is printed after
  `providers`. For each provider, it probes each execution context
  agents actually use:
  - the host;
  - and, when `executor.kind` is `docker` and the container is running,
    inside the container through the existing executor.

  A container that is not running is reported as `container not
  running`, for information only.
- **How the binary is found.** It is resolved as launch resolves it
  today. When H7's binary resolution lands, it will use that instead.
- **How the probe runs.** It runs `binary + version_command`:
  - non-interactively, with stdin from `/dev/null` and no TTY;
  - bounded by a timeout of 10 s by default;
  - the child process group is killed on timeout.
- **States.** One per provider and context:

  | State | Meaning |
  |---|---|
  | `verified` | The version matches a `verified` entry whose digest applies. |
  | `unverified` | A version was found and no applicable entry matches it. The report says whether it is newer, older or incomparable than the closest verified version, and which versions are verified. |
  | `overridden` | Integration code is overridden and no entry has the current digest. |
  | `missing` | The binary was not found. |
  | `timeout` | The probe did not finish in time. |
  | `probe_failed` | Non-zero exit, or `version_regex` did not match. The first line of output is shown. |
  | `malformed` | The manifest failed to parse or failed the schema. |
  | `disabled` | The provider is disabled in `providers.yaml`. It is not probed. |
  | `no manifest` | See DM-R1. |

- **Exit status.** `doctor` already counts problems.
  - `malformed` counts as a problem.
  - `missing` for an enabled provider is already counted by the
    existing `providers` section. It is not counted twice.
  - Every other D3 state is a warning or information, and does not
    change the exit status. In particular, `unverified`, `overridden`
    and `timeout` never fail `doctor` and never refuse a launch.
- **The Python surface for tests.**
  `probe(provider_name, paths=None, context="host") ->
  ProbeResult{state, version, detail, manifest_source,
  integration_sources, digest}`. The printer only formats these
  results.

Verified by, with a fake CLI script on PATH per test:
- one test for each state;
- a timeout test that returns within the bound;
- a test that `unverified`, `overridden` and `timeout` leave the exit
  status unchanged, and that `malformed` raises it.

**DM-R7: nothing else changes.**
- Launch, routing and auth never read the manifest. The manifest has no
  runtime effect outside `doctor`, `lint` and their tests.
- The existing `doctor` sections and their output are unchanged.
- The existing suite stays green, apart from the known reds.

## Known limitations, recorded

- The lint proves that declared dependencies point at real code. It
  does not prove that every dependency is declared, nor that the CLI
  still behaves the same.
- The container probe needs a running container. It never starts or
  recreates one.
