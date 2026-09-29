# D3: per-provider CLI dependency manifest, the contract

**Status:** contract, written by the orchestrator on 2026-09-29.
- Revision 2 folds in the advisor's review (ag-8e7d87, turn 2). That
  review cut `extends` and version ordering, pinned the reference forms
  and selectors, made "verified" context-aware, and added a probe seam for
  containers that are already running.
- **User's request:** each provider's "plugin directory" gets a file
  describing in detail everything the plugin depends on in its CLI, so
  that hunting for bugs caused by CLI updates can be automated.
- **Inputs:**
  - `context/specs/d3-cli-inventory.md` (ag-a609b9);
  - the advisor's points, in BRIEF handoff #2 and the entry at ~22:45 on
    2026-09-29.
- **Ids:** `DM-R*`. They are never renumbered. A behaviour is retired by
  marking it withdrawn.

## What this is, and what it is not

**What it is.** A machine-readable declaration of the *native* CLI surface
each provider relies on. It comes with two checks:
- a static lint that ties each declaration to the code or config that uses
  it;
- a `doctor` section that probes the real binary and compares its version
  with the versions recorded as verified.

**What it is not:**
- **A proof of compatibility.** The lint only gives syntactic evidence that
  declared dependencies point at real code. A constant that is present but
  unused can still satisfy it. The lint does not claim to resist deliberate
  gaming.
- **A proof of completeness.**
- **A version pin.** A version that is not verified produces a warning. It
  is never refused.

## Behaviours

**DM-R1: location and layering.**
- A manifest lives in a `providers/` directory beside the adapters and
  scripts, and is named `<provider>.dependencies.yaml`.
- **Runtime selection**, used by `doctor` and `probe`, follows the same
  precedence as adapters and scripts (`scripts.py` ~64–83): the project's
  `.multiagents/config/providers/`, then the global
  `~/.config/multiagents/providers/`, then the shipped
  `src/multiagents/defaults/providers/`. The first file found wins, and
  wins whole. Layers are never merged.
- **Shipped manifests** exist for claude, codex, opencode and agy.
- **A provider with no manifest in any layer** is valid.
  - `doctor` reports `no manifest`. This is information only: it does not
    count as a problem.
  - The lint skips that provider.
- **`lint()`** reads **only the shipped manifests**, against the shipped
  code and config (DM-R5). User overrides never affect it.
- Verified by:
  - a test of the layer order, using temporary global and project
    directories;
  - a test of whole-document replacement;
  - a test of a custom provider with no manifest.

**DM-R2: the document schema.**
- `schema: 1` is required. Any other value makes the manifest malformed.
- `provider` must equal the file-name prefix.
- `binary` is `{name, version_command, version_regex, version_stream}`:
  - `version_command` is the argv that follows the binary, for example
    `["--version"]`;
  - `version_regex` is a Python regular expression whose group 1 is the
    version;
  - `version_stream` is `stdout`, `stderr` or `either`, and defaults to
    `stdout`.
- `dependencies` is a list of entries of the form
  `{id, kind, value, match, used_by, note?}`:
  - **`id`** has the form `<kind>.<slug>` and is unique within the
    manifest.
  - **`kind`** is one of `subcommand`, `flag`, `env`, `state_path`,
    `stream_field`, `text_match`, `exit_code`, `endpoint`, `layout`.
  - **`value`** is the literal the CLI must keep honouring.
  - **`match`** is `exact` (the default) or `fragment`. Under `fragment`,
    `value` may occur *inside* a larger literal, for example
    `sandbox_mode=` inside `sandbox_mode="workspace-write"`.
  - **`used_by`** is a non-empty list of references (DM-R4).
- `not_dependencies` is optional. It is a list of `{selector, reason}`
  for leaves that DM-R5 would otherwise demand.
- `verified` is a list, possibly empty (DM-R3).
- **Out of scope for `dependencies`**, because it is not the CLI's
  surface:
  - multiagents' own `MULTIAGENTS_*` variables;
  - the script exit-code contract (0/10/20/64);
  - the adapter interface.

  An entry whose `value` names a `MULTIAGENTS_*` variable is the lint
  finding `internal_protocol`.
- Verified by: schema tests that accept a minimal document and reject each
  missing or ill-typed required key.

**DM-R3: what "verified" means, and when it applies.**
- Each `verified` entry records:
  - `version`, exact;
  - `date`;
  - `platform`, for example `linux-x86_64`;
  - `executor`, which is `local` or `docker`;
  - `evidence`;
  - `scope`, which is `all` or a list of dependency ids;
  - `integration_digest` (DM-R3a);
  - `dependencies_digest` (DM-R3a).
- **What does not count as verification:**
  - `--version` output alone, which is discovery;
  - fake-CLI tests, which prove the adapter, not the upstream CLI.
- **An entry applies** to a probe result only when all of these hold:
  - its version equals the probed version;
  - its platform equals the host platform;
  - its executor equals the probed context;
  - both digests equal the current digests.
- **An applicable entry with a list `scope`** yields `verified (partial:
  N of M dependencies)`. It never yields plain `verified`.
- **Shipped manifests start with `verified: []`.** Entries are added later
  by the orchestrator, from real runs. The implementer does not invent
  any.

**DM-R3a: the digests.**
- **`integration_digest(provider, paths)`** is the sha256 of a framed
  sequence of `(role, identifier, sha256(content))` records, in a fixed
  and documented order.
  - The roles are `adapter`, each script action file, and
    `providers_entry`.
  - `providers_entry` is the provider's entry in the effective
    `providers.yaml` **after `extends` is resolved**, serialized
    canonically: sorted keys, JSON, and nothing that depends on comments
    or formatting.
  - Each file is resolved as at runtime. A file listed twice is framed
    once.
  - A missing file is framed as a `missing` record, never skipped.
  - The digest depends on **content**, not on the layer, so a
    byte-identical override keeps the digest. Provenance, meaning which
    layer and which path each part came from, is reported separately.
- **`dependencies_digest`** is the sha256 of the canonical JSON of the
  manifest's `binary` and `dependencies`. Editing a dependency therefore
  voids the `verified` entries recorded before the edit.
- **Boundary, documented.** Shared integration code outside the
  provider's own files is **not** in either digest. For example,
  `docker.py` sets `ANTHROPIC_BASE_URL`.
- **`doctor`** prints both digests, so that a person recording a
  verification can copy them.
- Verified by:
  - the digest is stable across calls;
  - it changes with a content-changing project override;
  - it is unchanged by a byte-identical override;
  - it is unchanged by a comment-only `providers.yaml` edit, and changed
    by a value edit;
  - a `dependencies` edit changes `dependencies_digest`.

**DM-R4: references resolve to executable code or config.** Each
`used_by` reference has one of these forms:
- **YAML:** `providers.yaml#<JSON-Pointer>`. The pointer runs inside the
  shipped `providers.yaml`, starting at the provider's entry, for example
  `providers.yaml#/opencode/spawn/args/5`. Keys containing `/` or `~` use
  JSON-Pointer escaping.
- **Python:** `<repo-relative .py>::<qualified.name>`. It names a
  function, method or class, found through `ast`.
- **Shell, one case arm:** `<repo-relative .sh>::case:<label>`. It names
  a top-level `case` arm, for example `opencode.sh::case:check`.
- **Shell, one function:** `<repo-relative .sh>::fn:<name>`.
- **Shell, whole file:** `<repo-relative .sh>::file`. This form is allowed
  only for a script with no `case` arms and no functions.

A reference **resolves** when the target exists **and** the entry's
`value` occurs in the target's executable literals, according to `match`.
What counts as an executable literal depends on the target:
- **YAML:** the scalar at the pointer, or any scalar beneath it. A pointer
  to a mapping also counts its **keys**, so that native dependencies
  expressed as mapping keys can be covered, such as `part.reason`,
  `content-filter` and `OPENCODE_CONFIG`.
- **Python:** string constants in the definition's body. Docstrings do not
  count. A `fragment` also matches within f-string constant parts.
- **Shell:** the target's text with comments removed.
  - The comment stripper respects single and double quotes, `${…#…}`
    parameter expansions, and here-documents. A here-document body counts
    as a literal.
  - A dotted field path such as `part.state.input` may be declared as
    `fragment` against the source that navigates it. The lint does not try
    to reassemble dotted paths from separate keys.

A value that occurs only in a comment or a docstring does not resolve.

Verified by fixture tests for each reference form, covering:
- a reference that resolves;
- a missing target;
- a value that occurs only in a comment;
- a value that occurs only in a docstring;
- a `#` inside quotes and inside `${x#y}`, which is not treated as a
  comment;
- a here-document literal;
- a `fragment` match.

**DM-R5: the lint.**
- **`lint() -> list[LintFinding]`** runs over the shipped files only.
  Each finding carries `provider`, `dependency_id` (or null), `code` and
  `message`.
- **The codes** are `malformed`, `unresolved_ref`, `internal_protocol`,
  `duplicate_id`, `uncovered_leaf` and `stale_not_dependency`.
- **Forward check.** Every `used_by` reference must resolve (DM-R4).
- **Reverse coverage.** It is checked at the **leaf level** of the
  provider's shipped `providers.yaml` entry, over exactly these
  selectors. `[*]` means every index, and mapping keys count as leaves
  where noted:
  - `/spawn/args`, `/spawn/resume`, `/spawn/permission/*`,
    `/spawn/optional/*`;
  - `/models_cmd`;
  - `/mcp/args`, where present;
  - `/stream/session_id_paths`;
  - `/stream/rules[*]/match`, keys and values;
  - `/stream/rules[*]/fields`;
  - `/stream/status_map`, keys;
  - `/refusal_markers`;
  - `/truncation_markers`;
  - `/transcript/limit_markers[*]/match`;
  - `/home_links`;
  - `/bin_versions_depth`.

  The implementer confirms each selector against the real file and
  documents the final list in the lint module.
  - A leaf is covered only by a `used_by` pointer that designates **that
    leaf**, or its immediate parent when the parent is a list of plain
    scalars. A pointer to a larger subtree covers nothing.
  - **Placeholders** such as `{prompt}`, `{model}` and `{workdir}` are
    multiagents' own template syntax. They are not native dependencies and
    are exempt.
  - **Codex's YAML** `spawn` and `stream` surfaces are internal, because
    the adapter does the native work. They are listed under
    `not_dependencies` with that reason.
  - A `not_dependencies` selector that no longer matches anything is
    reported as `stale_not_dependency`.
- **Not claimed:** reverse coverage of Python or shell code.
- **The suite test.** `tests/test_d3_manifest_lint.py` asserts zero
  findings on the shipped tree. The shipped manifests are populated from
  `d3-cli-inventory.md`, and pass the lint.
- **Lint output** says "declared dependencies resolve". It never says
  "all dependencies are covered".

**DM-R6: `doctor` probes the real binary in each execution context.**
- **Section.** A new section, `cli dependencies`, is printed after
  `providers`.
- **Contexts.** They are the set of executors that agents actually use:
  the project executor plus every per-agent executor override. They are
  enumerated from the config itself, not from `executor_for()`'s first
  match (`executor/__init__.py` ~38).
- **Host context.** The binary is resolved as H7's `resolve_bin()`
  resolves it.
- **Docker context.** The probe runs only inside an **already running**
  container, through a new executor seam,
  `exec_in_running(argv, timeout) -> (rc, stdout, stderr) | NotRunning`.
  - The seam never starts, creates or seeds a container, unlike
    `start()` and `ensure_running()` (`docker.py` ~1912, ~2226).
  - On timeout, it kills the process **inside the container** as well as
    the host-side `docker exec`.
  - The overall deadline, cleanup included, is the probe timeout plus
    5 s.
  - A container that is not running yields `container not running`.
    This is information only.
- **Probe execution.**
  - The probe is non-interactive: stdin is `/dev/null` and there is no
    TTY.
  - It uses the same environment an agent run would have.
  - Its timeout defaults to 10 s.
  - The version is read from the stream that `version_stream` names.
- **State precedence.** For each provider and context, the first state
  that holds, in this order, is reported:

  | Order | State |
  |---|---|
  | 1 | `disabled` |
  | 2 | `malformed` |
  | 3 | `no manifest` |
  | 4 | `missing` |
  | 5 | `timeout` |
  | 6 | `probe_failed` |
  | 7 | `overridden` |
  | 8 | `verified` or `verified (partial)` |
  | 9 | `unverified` |

  `overridden` means the integration differs from the shipped one, and no
  applicable entry matches the current digests. Each state shows the
  manifest and integration provenance, the version found, and the
  verified versions for that context.
- **Exit status.**
  - `malformed` counts as a problem.
  - `missing` counts as a problem only in a context that the existing
    `providers` section does not already cover, such as a binary missing
    only in the container.
  - Every other D3 state is information or a warning. None of them
    changes the exit status or ever refuses a launch.
- **Python surface.** `probe(provider_name, paths=None, context="host")`
  returns `ProbeResult{state, version, detail, manifest_source,
  integration_sources, digests}`.
- Verified by:
  - a fake CLI per state, and a state-precedence test;
  - a timeout test that returns within the deadline;
  - a docker seam test with a stubbed executor: not running, and a
    timeout that kills on the container side;
  - an exit-status test.

**DM-R7: nothing else changes.**
- Launch, routing and auth never read the manifest.
- The existing `doctor` sections are unchanged.
- The existing suite stays green, apart from the known reds.

## Known limitations, recorded

- The lint gives syntactic evidence only. It proves neither completeness
  nor behaviour.
- Shared integration code sits outside the digests, as DM-R3a documents.
- The container probe needs a running container.
- The shipped `used_by` references are repo-relative. In an installed
  package they are resolved relative to the package root. The lint is a
  repository test and is not run against an installed package.
