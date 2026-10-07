"""Phase 0, group C: provider-specific prompt guidance (P0-R3.1 - P0-R3.6).

Contract: `context/specs/phase0-runtime-repairs.md`, section P0-R3.

A provider block may carry `agent_guidance:`. When it is non-empty, the prompt of
a run that EXECUTES on that provider (after routing and fallback) contains it
verbatim, after the role instructions and before `## Task`. `notes:` never
reaches a model. The contract mandates no heading for the section, so these
tests assert only the verbatim text and its position.

Black box: everything is observed through `Runner.compose_prompt`, through the
`prompt.md` a real `Runner.start` writes for the run, or through the shipped
`providers.yaml` loaded the way the CLI loads it.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c3_harness as h  # noqa: E402
from multiagents import budget as budget_mod  # noqa: E402
from multiagents import config as config_mod  # noqa: E402
from multiagents.config import AgentSpec, Config  # noqa: E402
from multiagents.paths import ProjectPaths, shipped_defaults_dir  # noqa: E402
from multiagents.runner import Runner  # noqa: E402
from multiagents.tree import Node  # noqa: E402


# Multi-line and markdown-bearing on purpose: "verbatim" has to survive both.
GUIDANCE_A = (
    "When a tool says its output was cut short, ask for the next range.\n"
    "\n"
    "- Use `offset` and `limit`.\n"
    "- Never assume you saw the whole file.  (two spaces kept)"
)
GUIDANCE_B = "Provider-B-only guidance: prefer the `grep_b` tool over reading files."
NOTES = "OPERATOR NOTES: internal remark about this CLI that no model should ever see."
ROLE = "# Role\n\nDo role things.\n"
TASK = "Write the thing.\n"
WORKDIR = Path("/work/tree")
RUNNABLE = {"bin": "sh", "spawn": {"args": ["-c", "true"]}}


def _brief(tmp_path: Path) -> Path:
    path = tmp_path / "brief.md"
    path.write_text(ROLE)
    return path


def _prompt(tmp_path: Path, providers: dict, provider: str,
            instructions: str | None = None) -> str:
    """The prompt composed for an agent running on `provider`, unrouted."""
    brief = instructions if instructions is not None else str(_brief(tmp_path))
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    config = Config(project={}, providers=providers, agents={}, models={},
                    instruction_dirs=[])
    spec = AgentSpec("worker", provider, "m1", instructions=brief)
    node = Node(id="ag-000001", agent="worker", provider=provider, model="m1",
                parent=None, depth=1, branch="agents/worker/000001", task="t")
    return Runner(paths, config).compose_prompt(spec, TASK, node, WORKDIR)


def _assert_positioned(prompt: str, guidance: str) -> None:
    """Verbatim, exactly once, after the role instructions, before `## Task`."""
    assert prompt.count(guidance) == 1, \
        f"guidance must appear verbatim exactly once:\n{prompt}"
    role_end = prompt.index("Do role things.") + len("Do role things.")
    task_at = prompt.index("## Task\n\n" + TASK.strip())
    at = prompt.index(guidance)
    assert role_end <= at, "guidance must come after the role instructions"
    assert at + len(guidance) <= task_at, "guidance must come before `## Task`"


# ---------------------------------------------------------------------------
# P0-R3.1: guidance reaches the prompt of runs executing on that provider
# ---------------------------------------------------------------------------

def test_r3_1_guidance_is_in_the_prompt_of_its_own_provider_only(tmp_path):
    providers = {"pa": {**RUNNABLE, "agent_guidance": GUIDANCE_A},
                 "pb": dict(RUNNABLE)}

    on_a = _prompt(tmp_path, providers, "pa")
    on_b = _prompt(tmp_path, providers, "pb")

    _assert_positioned(on_a, GUIDANCE_A)
    assert GUIDANCE_A not in on_b, "another provider's guidance leaked"


def test_r3_1_each_provider_gets_its_own_guidance_and_not_the_others(tmp_path):
    providers = {"pa": {**RUNNABLE, "agent_guidance": GUIDANCE_A},
                 "pb": {**RUNNABLE, "agent_guidance": GUIDANCE_B}}

    on_a = _prompt(tmp_path, providers, "pa")
    on_b = _prompt(tmp_path, providers, "pb")

    _assert_positioned(on_a, GUIDANCE_A)
    _assert_positioned(on_b, GUIDANCE_B)
    assert GUIDANCE_B not in on_a and GUIDANCE_A not in on_b


def test_r3_1_guidance_is_placed_before_the_task_even_without_role_instructions(tmp_path):
    """No brief at all: the section still exists and still precedes `## Task`."""
    providers = {"pa": {**RUNNABLE, "agent_guidance": GUIDANCE_A}}
    prompt = _prompt(tmp_path, providers, "pa", instructions="")

    assert prompt.count(GUIDANCE_A) == 1
    assert prompt.index(GUIDANCE_A) < prompt.index("## Task\n\n" + TASK.strip())


def _start_and_read_prompt(tmp_path, monkeypatch, providers, budgets, spec,
                           project=None) -> tuple[dict, str]:
    """Spawn through the real `Runner.start` and read back what the run was sent."""
    monkeypatch.setattr(budget_mod, "read_all", lambda *a, **k: dict(budgets))
    r = h.make_runner(tmp_path, monkeypatch, agents={spec.name: spec},
                      providers=providers, project=project or {})

    async def go():
        result = await r.start(spec.name, TASK)
        run = r.runs.get(result.get("agent_id"))
        if run is not None:
            await asyncio.wait_for(run.done.wait(), timeout=20)
        return result

    result = asyncio.run(go())
    assert result.get("status") != "failed" and not result.get("deferred"), result
    prompt = (Path(result["log"]) / "prompt.md").read_text()
    return result, prompt


def _two_fake_clis(tmp_path, guidance_a, guidance_b):
    done = [{"type": "result", "subtype": "success", "result": "done"}]
    pa = h.fake_cli(tmp_path, "pa", events=done)
    pb = h.fake_cli(tmp_path, "pb", events=done)
    if guidance_a is not None:
        pa["agent_guidance"] = guidance_a
    if guidance_b is not None:
        pb["agent_guidance"] = guidance_b
    return {"pa": pa, "pb": pb}


def test_r3_1_a_run_that_fell_back_gets_the_provider_it_launched_on(tmp_path, monkeypatch):
    """Pinned to `pa`, which has no headroom, so it launches on `pb`: the prompt
    carries pb's guidance and not pa's."""
    providers = _two_fake_clis(tmp_path, GUIDANCE_A, GUIDANCE_B)
    budgets = {"pa": budget_mod.Budget(provider="pa", known=True, headroom=0.0),
               "pb": budget_mod.Budget(provider="pb", known=True, headroom=0.9)}
    spec = AgentSpec("worker", "pa", "m1", instructions=str(_brief(tmp_path)),
                     models={"pb": "m2"})

    result, prompt = _start_and_read_prompt(
        tmp_path, monkeypatch, providers, budgets, spec,
        project={"budget": {"fallback_chain": ["pa", "pb"]}})

    assert result["provider"] == "pb", f"precondition: the run must have fallen back: {result}"
    _assert_positioned(prompt, GUIDANCE_B)
    assert GUIDANCE_A not in prompt, "the pinned provider's guidance followed the run"


def test_r3_1_a_fallback_onto_a_provider_without_guidance_drops_the_pinned_ones(
        tmp_path, monkeypatch):
    providers = _two_fake_clis(tmp_path, GUIDANCE_A, None)
    budgets = {"pa": budget_mod.Budget(provider="pa", known=True, headroom=0.0),
               "pb": budget_mod.Budget(provider="pb", known=True, headroom=0.9)}
    spec = AgentSpec("worker", "pa", "m1", instructions=str(_brief(tmp_path)),
                     models={"pb": "m2"})

    result, prompt = _start_and_read_prompt(
        tmp_path, monkeypatch, providers, budgets, spec,
        project={"budget": {"fallback_chain": ["pa", "pb"]}})

    assert result["provider"] == "pb", f"precondition: the run must have fallen back: {result}"
    assert GUIDANCE_A not in prompt


def test_r3_1_an_unrouted_run_through_start_carries_its_providers_guidance(
        tmp_path, monkeypatch):
    """Control for the fallback tests: the same spawn path, no fallback."""
    providers = _two_fake_clis(tmp_path, GUIDANCE_A, GUIDANCE_B)
    budgets = {"pa": budget_mod.Budget(provider="pa", known=True, headroom=0.9),
               "pb": budget_mod.Budget(provider="pb", known=True, headroom=0.9)}
    spec = AgentSpec("worker", "pa", "m1", instructions=str(_brief(tmp_path)),
                     models={"pb": "m2"})

    result, prompt = _start_and_read_prompt(
        tmp_path, monkeypatch, providers, budgets, spec,
        project={"budget": {"fallback_chain": ["pa", "pb"]}})

    assert result["provider"] == "pa", result
    _assert_positioned(prompt, GUIDANCE_A)
    assert GUIDANCE_B not in prompt


# ---------------------------------------------------------------------------
# P0-R3.2: absent or empty key -> byte-for-byte today's prompt
# ---------------------------------------------------------------------------

# Captured from `compose_prompt` at e1e605a, before P0-R3 existed, for a
# provider without the key (fixed id, branch, workdir, brief and task). If the
# PREAMBLE is changed deliberately by another contract, regenerate this and
# say so in that commit. It must never change as a side effect of P0-R3.
GOLDEN = (
    'You are an autonomous subagent in a delegated agent tree. This block is\n'
    'generated — it tells you where you stand.\n'
    '\n'
    '- Your id: ag-000001 (worker), running on plain/m1\n'
    '- Parent: you (the orchestrator)\n'
    '- Depth: 1 of a maximum 3\n'
    '- Working directory: /work/tree\n'
    '- Branch: agents/worker/000001 (yours alone; commit freely)\n'
    '- You may not spawn subagents.\n'
    '\n'
    'How this works:\n'
    '\n'
    '- Nobody is watching you interactively and you cannot ask a question mid-run.\n'
    '  Two markers are available, and choosing the right one matters:\n'
    '\n'
    '  `NEED_INFO(<topic>): <question>` — for something another agent or your parent\n'
    '  could tell you. Non-blocking: state your assumption and carry on. Prefer this.\n'
    '\n'
    '  `NEED_DECISION(<topic>): <question>` — for a choice that changes what\n'
    '  "correct" means, where guessing wrong wastes everything built on it. This\n'
    '  STOPS you immediately, so use it sparingly and only when you genuinely\n'
    '  cannot proceed sensibly either way. You must follow it with a line\n'
    '  `DEFAULT: <what you would have chosen>` — if writing that line makes the\n'
    '  answer obvious, you did not need to ask.\n'
    '- Your parent sees only your final message, never your intermediate steps. Put\n'
    '  everything that matters in it.\n'
    '- If `BRIEF.md` exists at the top of your working directory, read it first: it\n'
    '  is the agreed statement of what this project is and what done looks like.\n'
    '  `context/` holds the reference material it points at. Both are reference —\n'
    '  read them, and do not edit them unless your task explicitly says to.\n'
    '- If the multiagents tooling itself misbehaves — a tool contradicting its own\n'
    '  description, state that disagrees with itself — say so plainly in your final\n'
    '  message rather than working around it silently. Your parent decides whether it\n'
    '  gets written up.\n'
    '- Work only inside your working directory.\n'
    '- Do not merge, rebase, push, or switch branches. Your parent owns that.\n'
    '\n'
    '---\n'
    '\n'
    '# Role\n'
    '\n'
    'Do role things.\n'
    '\n'
    '---\n'
    '\n'
    '## Task\n'
    '\n'
    'Write the thing.\n'
)


@pytest.mark.parametrize("block", [
    pytest.param({}, id="key-absent"),
    pytest.param({"agent_guidance": ""}, id="key-empty"),
    pytest.param({"agent_guidance": None}, id="key-null"),
])
def test_r3_2_absent_or_empty_guidance_leaves_the_prompt_byte_identical(tmp_path, block):
    providers = {"plain": {**RUNNABLE, **block}}
    assert _prompt(tmp_path, providers, "plain") == GOLDEN


def test_r3_2_another_providers_guidance_does_not_change_this_prompt(tmp_path):
    """Declaring the key on one provider must not perturb any other's prompt."""
    providers = {"plain": dict(RUNNABLE),
                 "pa": {**RUNNABLE, "agent_guidance": GUIDANCE_A}}
    assert _prompt(tmp_path, providers, "plain") == GOLDEN


# ---------------------------------------------------------------------------
# P0-R3.3: `notes:` is never sent to a model
# ---------------------------------------------------------------------------

def test_r3_3_notes_without_guidance_produce_no_section(tmp_path):
    providers = {"plain": {**RUNNABLE, "notes": NOTES}}
    prompt = _prompt(tmp_path, providers, "plain")

    assert NOTES not in prompt
    assert prompt == GOLDEN, "notes alone must not add a guidance section"


def test_r3_3_notes_stay_out_even_when_guidance_is_present(tmp_path):
    providers = {"pa": {**RUNNABLE, "notes": NOTES, "agent_guidance": GUIDANCE_A}}
    prompt = _prompt(tmp_path, providers, "pa")

    _assert_positioned(prompt, GUIDANCE_A)
    assert NOTES not in prompt


def test_r3_3_the_shipped_providers_notes_never_reach_a_prompt(tmp_path):
    """Every shipped provider's real `notes:` text, against its own prompt."""
    config = config_mod.load(None)
    checked = 0
    for name, block in config.providers.items():
        notes = (block or {}).get("notes") or ""
        if not notes.strip():
            continue
        providers = {name: {**(block or {}), **RUNNABLE}}
        prompt = _prompt(tmp_path, providers, name)
        # Folded YAML may be long; any one sentence of it leaking is a leak.
        for line in (s.strip() for s in notes.splitlines()):
            if len(line) > 20:
                assert line not in prompt, f"{name}'s notes leaked: {line!r}"
        checked += 1
    assert checked, "precondition: at least one shipped provider has notes"


# ---------------------------------------------------------------------------
# P0-R3.4: inherited through `extends:`, overridable in the project layer
# ---------------------------------------------------------------------------

def test_r3_4_an_extends_child_inherits_the_guidance(tmp_path):
    providers = {"base": {**RUNNABLE, "agent_guidance": GUIDANCE_A},
                 "child": {"extends": "base"}}
    _assert_positioned(_prompt(tmp_path, providers, "child"), GUIDANCE_A)


def test_r3_4_an_extends_child_can_override_the_guidance(tmp_path):
    providers = {"base": {**RUNNABLE, "agent_guidance": GUIDANCE_A},
                 "child": {"extends": "base", "agent_guidance": GUIDANCE_B}}
    prompt = _prompt(tmp_path, providers, "child")

    _assert_positioned(prompt, GUIDANCE_B)
    assert GUIDANCE_A not in prompt


def test_r3_4_an_extends_child_can_clear_the_guidance_with_an_empty_string(tmp_path):
    """Overriding with "" leaves the key empty, and P0-R3.2 says an empty key
    means no section."""
    providers = {"base": {**RUNNABLE, "agent_guidance": GUIDANCE_A},
                 "child": {"extends": "base", "agent_guidance": ""}}
    assert GUIDANCE_A not in _prompt(tmp_path, providers, "child")


def test_r3_4_the_project_layer_overrides_a_lower_layers_guidance(tmp_path):
    """The shipped agy block carries guidance (P0-R3.5). A project-layer
    `providers.yaml` naming only `agent_guidance` replaces it, and an instance
    declared in the project layer that extends agy inherits the replacement."""
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    paths.config.mkdir(parents=True, exist_ok=True)
    override = "PROJECT-LAYER GUIDANCE: request ranges with the project's own words."
    (paths.config / "providers.yaml").write_text(yaml.safe_dump({"providers": {
        "agy": {"agent_guidance": override},
        "agy-2": {"extends": "agy"},
    }}))
    config = config_mod.load(paths)
    runner = Runner(paths, config)

    for provider in ("agy", "agy-2"):
        spec = AgentSpec("worker", provider, "m1")
        node = Node(id="ag-000001", agent="worker", provider=provider, model="m1",
                    parent=None, depth=1, branch="agents/worker/000001", task="t")
        prompt = runner.compose_prompt(spec, TASK, node, WORKDIR)
        assert prompt.count(override) == 1, f"{provider}: override missing\n{prompt}"
        assert prompt.index(override) < prompt.index("## Task")
        assert "view_file" not in prompt, \
            f"{provider}: the shipped guidance survived the project override"


# ---------------------------------------------------------------------------
# P0-R3.5: agy's shipped block carries view_file truncation guidance
# ---------------------------------------------------------------------------

def _shipped_providers() -> dict:
    raw = yaml.safe_load((shipped_defaults_dir() / "providers.yaml").read_text())
    return raw["providers"]


def test_r3_5_agys_shipped_guidance_names_view_file_and_reaches_agy_runs(tmp_path):
    shipped = _shipped_providers()
    guidance = (shipped["agy"] or {}).get("agent_guidance") or ""
    assert guidance.strip(), "agy's shipped block has no agent_guidance"
    assert "view_file" in guidance

    providers = {"agy": {**shipped["agy"], **RUNNABLE}}
    prompt = _prompt(tmp_path, providers, "agy")
    _assert_positioned(prompt, guidance.strip())


def test_r3_5_only_agy_ships_view_file_guidance(tmp_path):
    """The tool name is agy's. Another shipped provider's run must not be told
    about it (it would be a tool that provider does not have)."""
    shipped = _shipped_providers()
    for name, block in shipped.items():
        if name == "agy" or (block or {}).get("extends") == "agy":
            continue
        providers = {name: {**(block or {}), **RUNNABLE}}
        assert "view_file" not in _prompt(tmp_path, providers, name), name


# ---------------------------------------------------------------------------
# P0-R3.6: agy's tool name and truncation string live only in providers.yaml
# ---------------------------------------------------------------------------

# A guard rather than a red test: nothing in src/multiagents/*.py names these
# today, and this keeps it that way once the guidance exists.
@pytest.mark.parametrize("needle", ["view_file", "does NOT show the entire file"])
def test_r3_6_agys_tool_facts_are_not_in_the_python_source(needle):
    src = Path(config_mod.__file__).resolve().parent
    hits = [f"{path.name}:{n}"
            for path in sorted(src.glob("*.py"))
            for n, line in enumerate(path.read_text().splitlines(), 1)
            if re.search(re.escape(needle), line)]
    assert not hits, f"{needle!r} belongs in providers.yaml, found in {hits}"
