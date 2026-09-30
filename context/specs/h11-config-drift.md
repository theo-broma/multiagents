# H11: the config-drift warning, the contract

**Status:** contract, written by the orchestrator on 2026-09-30.
- **Source:** phase6-hardening.md H11, and BRIEF around line 2176.
  - A project `providers.yaml` that was a full copy of the shipped file
    froze the shipped turn rules for 13 days. List values replace
    wholesale when layers merge, and nothing said so.
  - The global `~/.config/multiagents/providers.yaml` is such a copy
    today.
- **Ids:** `CD-R*`. They are never renumbered. A behaviour is retired by
  marking it withdrawn.

## Behaviours

**CD-R1: detect list values that shadow a different shipped value.**
- **The function.** `drift.find_shadowing(paths=None) ->
  list[Shadow{file, key_path, kind, detail}]` compares each non-shipped
  layer against the shipped defaults. The layers are the global config
  directory and the project's `.multiagents/config/`.
- **Files compared:** `providers.yaml`, `agents.yaml` and `project.yaml`.
- **What is reported.** A key path is reported, with `kind:
  "list_shadow"`, when all of these hold:
  - it exists in the layer file and in the shipped file;
  - both values are lists;
  - the two lists differ.
  - Example key paths: `providers.opencode.spawn.args` and
    `providers.claude.stream.rules`.
- **Scalars and mappings are not reported.** Scalars are deliberate
  overrides by nature, and mappings merge.
- **A key that exists only in the layer** is not drift.
- Verified by:
  - with a temporary shipped file and a temporary global layer, a
    differing list is reported with its exact dotted key path;
  - an identical list is not reported;
  - a differing scalar is not reported;
  - a key absent from the shipped file is not reported.

**CD-R2: detect copied instruction files.**
- A project or global instruction file that has the same relative path as
  a shipped one (for example `agents/team/tester.md`) and a different
  content is reported with `kind: "file_shadow"`.
- Where the layer file's modification time is **older** than the shipped
  file's, the detail says the shipped version is newer.
- Verified by:
  - a differing copy is reported;
  - an identical copy is not reported;
  - an older copy says the shipped version is newer.

**CD-R3: deliberate overrides can be acknowledged.**
- A layer file may carry a top-level list
  `drift_acknowledged: [<dotted key path or relative file path>, …]`.
- Each entry silences exactly that item. Entries that no longer match
  anything are themselves reported, as `kind: "stale_acknowledgement"`.
- For a `.md` file, the acknowledgement is given in the layer's
  `agents.yaml`.
- Verified by:
  - an acknowledged shadow is silent;
  - a stale acknowledgement is reported.

**CD-R4: surfaces.**
- **`doctor`** prints a section, `config drift`, listing each item with
  its file, key path and one line of detail, followed by the fix: remove
  the key so the shipped value applies, or add it to
  `drift_acknowledged`.
  - Drift is a **warning**. It never changes `doctor`'s exit status.
  - With no drift, the section prints `none`.
- **The MCP server** logs the count once at startup, as a single line
  naming `multiagents doctor`.
- Verified by:
  - `doctor`'s output with drift present;
  - `doctor`'s exit status being unchanged by drift.

**CD-R5: nothing else changes.**
- Config loading and layer merging are unchanged. This is detection only.
- The existing suite stays green, apart from the known reds.
