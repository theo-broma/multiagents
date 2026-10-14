"""C16 — a prompt transport and its spawn args must agree.

Contract: `context/specs/c16-transport-args-guard.md`, ids TG-R1..TG-R5.

Everything is driven through real, layered configuration: the shipped defaults,
a global layer (`MULTIAGENTS_CONFIG_DIR/providers.yaml`) and a project layer
(`<project>/.multiagents/providers.yaml`) are real files read by `config.load`,
then a real `Runner` launches a fake native (the one of `pf_harness`, which logs
argv, stdin and any file named in argv). `multiagents doctor` is the real
`cli.cmd_doctor`. The config key and the placeholder names are those C3 chose
and `pf_harness` spells in one block.

What the tests do NOT pin: the exact wording of the error, the exact shape of a
doctor line, whether a line number is given. They pin that the error names the
provider, the layer, the file path and the fix (drop the `args` override, or set
`prompt_transport: argv`).
"""
from __future__ import annotations

import argparse
import copy
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c3_harness as h  # noqa: E402
import pf_harness as pf  # noqa: E402

import multiagents.cli as cli  # noqa: E402
import multiagents.config as config_mod  # noqa: E402
from multiagents import budget as budget_mod  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from multiagents.runner import Runner  # noqa: E402

TRANSPORT_KEY = pf.TRANSPORT_OPTION
FIX_WORDS = ("args", pf.TRANSPORT_ARGV)         # "drop the stale args" / "argv"
STALE_ARGS = ["-p", "{prompt}", "--flag"]       # a pre-C3 list


# ---------------------------------------------------------------------------
# a layered project
# ---------------------------------------------------------------------------

class Layered:
    """A real project with real global and project config layers.

    `global_providers` / `project_providers` are the `providers:` maps written to
    each layer's providers.yaml (overlay only; the shipped layer is untouched).
    Each provider named in them gets a fake native as its `bin` unless the
    overlay already says otherwise. `agents` maps agent name -> provider (model
    `m1` unless the shipped provider allows only another one).
    """

    def __init__(self, tmp_path: Path, monkeypatch, *, global_providers=None,
                 project_providers=None, agents: dict[str, str] | None = None,
                 kinds: dict[str, str] | None = None, only: set[str] | None = None):
        self.tmp, self.mp = tmp_path, monkeypatch
        monkeypatch.setattr(budget_mod, "read_all", lambda *a, **k: {})
        self.project_dir = tmp_path / "project"
        h.as_root(monkeypatch)
        self.paths = h.make_paths(self.project_dir)
        h.make_git_repo(self.project_dir)
        monkeypatch.chdir(self.project_dir)
        self.gdir = Path(os.environ["MULTIAGENTS_CONFIG_DIR"])
        self.gdir.mkdir(parents=True, exist_ok=True)
        self.paths.config.mkdir(parents=True, exist_ok=True)
        self.gfile = self.gdir / "providers.yaml"
        self.pfile = self.paths.config / "providers.yaml"
        self.natives: dict[str, pf.Native] = {}
        kinds = kinds or {}
        for layer_file, blocks in ((self.gfile, global_providers),
                                   (self.pfile, project_providers)):
            if blocks is None:
                continue
            blocks = copy.deepcopy(blocks)
            for name, blk in blocks.items():
                if "bin" not in blk:
                    native = self.natives.get(name) or pf.Native(
                        tmp_path, kinds.get(name, name if name in pf.SHIPPED_PROVIDERS
                                            else "custom"), name)
                    self.natives[name] = native
                    blk["bin"] = str(native.path)
                blk.setdefault("enabled", True)
            layer_file.write_text(yaml.safe_dump({"providers": blocks}, sort_keys=False))
        self.agent_names = agents or {}
        self.only = only
        self.rig = self._rig()

    def _rig(self) -> "pf.Rig":
        cfg = config_mod.load(self.paths)
        agents = {}
        for name, provider in self.agent_names.items():
            model = pf.SHIPPED_PROVIDERS.get(provider, ("", "m1"))[1]
            agents[name] = AgentSpec.from_dict(
                name, {"provider": provider, "model": model, "can_spawn": False,
                       "description": "t", "instructions": ""})
        cfg.agents = agents
        cfg.project = {**cfg.project, "team": "",
                       "limits": {**(cfg.project.get("limits") or {}),
                                  "provider_failure_threshold": 100,
                                  "startup_failure_threshold": 100}}
        self.config = cfg
        rig = pf.Rig.__new__(pf.Rig)
        rig.tmp, rig.monkeypatch, rig.executor = self.tmp, self.mp, "local"
        rig.natives, rig.models = self.natives, {}
        rig.docker_log = self.tmp / "docker.log"
        rig.agents = agents
        rig.runner = Runner(self.paths, cfg)
        return rig

    def calls(self, name: str) -> list[dict]:
        return self.natives[name].calls()

    # -- doctor ----------------------------------------------------------

    def doctor(self, capsys) -> tuple[int, str]:
        names = set(self.agent_names.values()) | set(self.natives)
        real = config_mod.load

        def load(paths=None, *a, **kw):
            cfg = real(paths, *a, **kw)
            cfg.providers = {k: v for k, v in cfg.providers.items() if k in names}
            cfg.agents = {k: v for k, v in cfg.agents.items() if v.provider in names}
            return cfg
        self.mp.setattr(config_mod, "load", load)
        self.mp.setattr(cli, "load_config", load)
        rc = cli.cmd_doctor(argparse.Namespace(path=str(self.project_dir),
                                               clear=None, force=False))
        return rc, capsys.readouterr().out


def _custom(transport, args, resume=None) -> dict:
    return _custom_block(transport, args, resume)


def _custom_block(transport, args, resume) -> dict:
    block = {
        "family": "cust",
        "spawn": {"args": args, "resume": resume or ["--resume", "{session_id}"]},
        "stream": {"format": "ndjson", "session_id_paths": ["session_id"],
                   "rules": [{"match": {"type": "result"}, "as": "result",
                              "fields": {"status": "subtype", "text": "result"}},
                             {"match": {"type": "step"}, "as": "step", "fields": {}}]},
    }
    if transport is not None:
        block["spawn"][TRANSPORT_KEY] = transport
    return block


def _refusal(result: dict) -> str:
    return str(result.get("error", ""))


def _no_empty_argv(calls: list[dict], literal_empties: int = 0) -> None:
    """No empty element where `{prompt}` was. `literal_empties` is how many
    empty strings the configured args spell out on purpose (the shipped agy
    block has `-p ""`)."""
    for call in calls:
        assert call["argv"].count("") <= literal_empties, (
            f"an empty argv element reached the native: {call['argv']}")


def _says_where(message: str, *, provider: str, layer_word: str, file: Path) -> None:
    assert provider in message, f"the error does not name provider {provider!r}: {message}"
    assert str(file) in message, f"the error does not name the file {file}: {message}"
    assert layer_word in message.lower(), f"the error does not name the {layer_word} layer: {message}"


def _says_fix(message: str) -> None:
    low = message.lower()
    for word in FIX_WORDS:
        assert word in low, f"the error does not give the fix (missing {word!r}): {message}"
    assert TRANSPORT_KEY in message, f"the fix does not name {TRANSPORT_KEY!r}: {message}"


# ---------------------------------------------------------------------------
# TG-R1: a mismatch is refused at admission
# ---------------------------------------------------------------------------

def test_tg_r1_shipped_stdin_provider_with_a_global_pre_c3_args_override_is_refused(
        tmp_path, monkeypatch):
    # the real incident: shipped claude (stdin), a layer pins the old args list
    lay = Layered(tmp_path, monkeypatch,
                  global_providers={"claude": {"spawn": {"args": STALE_ARGS}}},
                  agents={"claude": "claude"})
    result = lay.rig.start("claude", "do the thing")
    message = _refusal(result)
    assert message, f"a stdin provider with {{prompt}} in args was launched: {result}"
    _says_where(message, provider="claude", layer_word="global", file=lay.gfile)
    _says_fix(message)
    assert lay.calls("claude") == [], "a process was spawned"
    agent_id = result.get("agent_id")
    if agent_id:
        assert not (lay.rig.run_dir(agent_id) / "wrapper.pid").exists()


def test_tg_r1_the_project_layer_override_is_named_as_the_project_layer_and_file(
        tmp_path, monkeypatch):
    lay = Layered(tmp_path, monkeypatch,
                  project_providers={"claude": {"spawn": {"args": STALE_ARGS}}},
                  agents={"claude": "claude"})
    message = _refusal(lay.rig.start("claude", "x"))
    assert message, "not refused"
    _says_where(message, provider="claude", layer_word="project", file=lay.pfile)
    assert str(lay.gfile) not in message, f"names the wrong layer file: {message}"
    _says_fix(message)
    assert lay.calls("claude") == []


def test_tg_r1_the_layer_named_is_the_one_that_set_the_key_not_the_topmost_layer(
        tmp_path, monkeypatch):
    # the global layer sets the stale args; the project layer touches another key
    lay = Layered(tmp_path, monkeypatch,
                  global_providers={"claude": {"spawn": {"args": STALE_ARGS}}},
                  project_providers={"claude": {"notes": "project note"}},
                  agents={"claude": "claude"})
    message = _refusal(lay.rig.start("claude", "x"))
    assert message, "not refused"
    assert str(lay.gfile) in message, message
    assert str(lay.pfile) not in message, f"blames the project layer: {message}"
    assert lay.calls("claude") == []


def test_tg_r1_a_project_layer_args_override_wins_over_a_correct_global_layer(
        tmp_path, monkeypatch):
    lay = Layered(tmp_path, monkeypatch,
                  global_providers={"claude": {"spawn": {"args": ["--ok"]}}},
                  project_providers={"claude": {"spawn": {"args": STALE_ARGS}}},
                  agents={"claude": "claude"})
    message = _refusal(lay.rig.start("claude", "x"))
    assert message, "not refused"
    assert str(lay.pfile) in message and str(lay.gfile) not in message, message
    assert lay.calls("claude") == []


def test_tg_r1_a_custom_stdin_provider_with_prompt_in_args_is_refused(tmp_path, monkeypatch):
    lay = Layered(tmp_path, monkeypatch,
                  global_providers={"cust": _custom(pf.TRANSPORT_STDIN, STALE_ARGS)},
                  agents={"cust": "cust"})
    result = lay.rig.start("cust", "x")
    message = _refusal(result)
    assert message, f"launched: {result}"
    _says_where(message, provider="cust", layer_word="global", file=lay.gfile)
    _says_fix(message)
    assert lay.calls("cust") == []


def test_tg_r1_transport_in_one_layer_and_args_in_another_names_the_args_layer(
        tmp_path, monkeypatch):
    lay = Layered(tmp_path, monkeypatch,
                  global_providers={"cust": _custom(pf.TRANSPORT_STDIN, ["--ok"])},
                  project_providers={"cust": {"spawn": {"args": STALE_ARGS}}},
                  agents={"cust": "cust"})
    message = _refusal(lay.rig.start("cust", "x"))
    assert message, "not refused"
    assert str(lay.pfile) in message, f"does not name the file that set args: {message}"
    assert lay.calls("cust") == []


def test_tg_r1_stdin_provider_whose_resume_still_carries_prompt_is_refused(
        tmp_path, monkeypatch):
    stale_resume = ["--resume", "{session_id}", "-p", "{prompt}"]
    lay = Layered(tmp_path, monkeypatch,
                  global_providers={"cust": _custom(pf.TRANSPORT_STDIN, ["--flag"], stale_resume)},
                  agents={"cust": "cust"})
    rig = lay.rig
    first = rig.start("cust", "first")
    message = _refusal(first)
    if not message:
        # admission may be per launch kind: then the resume launch must refuse
        agent_id = first["agent_id"]
        before = len(lay.calls("cust"))
        steer = rig.steer(agent_id, "second")
        message = _refusal(steer)
        assert message, f"a resume with {{prompt}} under stdin was launched: {steer}"
        assert len(lay.calls("cust")) == before, "the resume spawned a process"
    _says_where(message, provider="cust", layer_word="global", file=lay.gfile)
    _says_fix(message)
    _no_empty_argv(lay.calls("cust"))


def test_tg_r1_file_transport_without_the_file_placeholder_is_refused(tmp_path, monkeypatch):
    lay = Layered(tmp_path, monkeypatch,
                  global_providers={"cust": _custom(pf.TRANSPORT_FILE, ["--flag"])},
                  agents={"cust": "cust"})
    result = lay.rig.start("cust", "x")
    message = _refusal(result)
    assert message, f"a file-transport provider with no {pf.FILE_PLACEHOLDER} was launched: {result}"
    _says_where(message, provider="cust", layer_word="global", file=lay.gfile)
    assert pf.FILE_PLACEHOLDER in message, f"does not name the missing placeholder: {message}"
    assert lay.calls("cust") == []


def test_tg_r1_file_transport_with_prompt_in_args_is_refused_even_with_the_placeholder(
        tmp_path, monkeypatch):
    lay = Layered(tmp_path, monkeypatch,
                  global_providers={"cust": _custom(
                      pf.TRANSPORT_FILE, ["--in", pf.FILE_PLACEHOLDER, "{prompt}"])},
                  agents={"cust": "cust"})
    message = _refusal(lay.rig.start("cust", "x"))
    assert message, "not refused"
    assert lay.calls("cust") == []


def test_tg_r1_stdin_transport_does_not_require_the_file_placeholder(tmp_path, monkeypatch):
    # the second clause of the rule is for `file` only
    lay = Layered(tmp_path, monkeypatch,
                  global_providers={"cust": _custom(pf.TRANSPORT_STDIN, ["--flag"])},
                  agents={"cust": "cust"})
    result = lay.rig.start("cust", "hello")
    assert not _refusal(result), result
    (call,) = lay.calls("cust")
    assert call["stdin"].endswith(b"hello\n") or call["stdin"].endswith(b"hello")


def test_tg_r1_a_refused_launch_leaves_the_project_usable_for_a_correct_provider(
        tmp_path, monkeypatch):
    lay = Layered(tmp_path, monkeypatch,
                  global_providers={"bad": _custom(pf.TRANSPORT_STDIN, STALE_ARGS),
                                    "good": _custom(pf.TRANSPORT_STDIN, ["--flag"])},
                  agents={"bad": "bad", "good": "good"})
    assert _refusal(lay.rig.start("bad", "x"))
    result = lay.rig.start("good", "fine")
    assert not _refusal(result), result
    assert b"fine" in lay.calls("good")[0]["stdin"]
    assert lay.calls("bad") == []


def test_tg_r1_refusing_twice_is_the_same_refusal_and_still_spawns_nothing(
        tmp_path, monkeypatch):
    lay = Layered(tmp_path, monkeypatch,
                  global_providers={"cust": _custom(pf.TRANSPORT_STDIN, STALE_ARGS)},
                  agents={"cust": "cust"})
    a = _refusal(lay.rig.start("cust", "x"))
    b = _refusal(lay.rig.start("cust", "x"))
    assert a and b
    assert lay.calls("cust") == []


# ---------------------------------------------------------------------------
# TG-R2: doctor reports it
# ---------------------------------------------------------------------------

def _problem_count(output: str) -> int:
    last = [ln for ln in output.splitlines() if ln.strip()][-1].strip()
    if last == "ok":
        return 0
    assert last.endswith("problem(s)"), f"unexpected last doctor line: {last!r}"
    return int(last.split()[0])


def _doctor_of(tmp_path, monkeypatch, capsys, sub, **kw):
    lay = Layered(tmp_path / sub, monkeypatch, **kw)
    rc, out = lay.doctor(capsys)
    return lay, rc, out


@pytest.mark.parametrize("case", ["stale-args", "file-without-placeholder", "stale-resume"])
def test_tg_r2_doctor_lists_the_provider_with_layer_file_and_fix_and_launches_nothing(
        tmp_path, monkeypatch, capsys, case):
    bad = {
        "stale-args": _custom(pf.TRANSPORT_STDIN, STALE_ARGS),
        "file-without-placeholder": _custom(pf.TRANSPORT_FILE, ["--flag"]),
        "stale-resume": _custom(pf.TRANSPORT_STDIN, ["--flag"],
                                ["--resume", "{session_id}", "{prompt}"]),
    }[case]
    good = {
        "stale-args": _custom(pf.TRANSPORT_STDIN, ["--flag"]),
        "file-without-placeholder": _custom(pf.TRANSPORT_FILE, ["--in", pf.FILE_PLACEHOLDER]),
        "stale-resume": _custom(pf.TRANSPORT_STDIN, ["--flag"]),
    }[case]
    _g, _rc0, out0 = _doctor_of(tmp_path, monkeypatch, capsys, "good",
                                global_providers={"cust": good}, agents={"cust": "cust"})
    lay, rc, out = _doctor_of(tmp_path, monkeypatch, capsys, "bad",
                              global_providers={"cust": bad}, agents={"cust": "cust"})
    assert lay.calls("cust") == [], "doctor launched the provider"
    # the fake provider has an unrelated baseline problem (no auth script); the
    # mismatch must add exactly one more
    assert _problem_count(out) == _problem_count(out0) + 1, f"not counted:\n{out}"
    assert rc == 1
    assert str(lay.gfile) in out, f"doctor does not name the file:\n{out}"
    assert "global" in out.lower()
    assert TRANSPORT_KEY in out, f"doctor does not give the fix:\n{out}"
    assert any("cust" in ln and str(lay.gfile) in ln or ("cust" in ln and "args" in ln.lower())
               or ("cust" in ln and "prompt" in ln.lower()) for ln in out.splitlines()
               if "native" not in ln), f"no problem line names the provider:\n{out}"


def test_tg_r2_doctor_names_the_project_layer_file_when_the_project_layer_sets_it(
        tmp_path, monkeypatch, capsys):
    lay = Layered(tmp_path, monkeypatch,
                  global_providers={"cust": _custom(pf.TRANSPORT_STDIN, ["--ok"])},
                  project_providers={"cust": {"spawn": {"args": STALE_ARGS}}},
                  agents={"cust": "cust"})
    rc, out = lay.doctor(capsys)
    assert str(lay.pfile) in out, out
    assert str(lay.gfile) not in out, out
    assert rc == 1


def test_tg_r2_a_correct_config_produces_no_such_problem(tmp_path, monkeypatch, capsys):
    blocks = {"cust": _custom(pf.TRANSPORT_STDIN, ["--flag"]),
              "fcust": _custom(pf.TRANSPORT_FILE, ["--in", pf.FILE_PLACEHOLDER]),
              "acust": _custom(pf.TRANSPORT_ARGV, STALE_ARGS)}
    agents = {"cust": "cust", "fcust": "fcust", "acust": "acust"}
    good, _rc, out = _doctor_of(tmp_path, monkeypatch, capsys, "g",
                                global_providers=blocks, agents=agents)
    assert "{prompt" not in out, f"doctor flags a correct config:\n{out}"
    assert str(good.gfile) not in out, out
    baseline = _problem_count(out)
    broken = {**blocks, "cust": _custom(pf.TRANSPORT_STDIN, STALE_ARGS)}
    _bad, _rc2, out2 = _doctor_of(tmp_path, monkeypatch, capsys, "b",
                                  global_providers=broken, agents=agents)
    # exactly the one broken provider adds exactly one problem
    assert _problem_count(out2) == baseline + 1, out2


def test_tg_r2_doctor_reports_the_shipped_override_incident(tmp_path, monkeypatch, capsys):
    _g, _rc0, out0 = _doctor_of(tmp_path, monkeypatch, capsys, "good",
                                global_providers={"claude": {"spawn": {"args": ["--ok"]}}},
                                agents={"claude": "claude"})
    lay, rc, out = _doctor_of(tmp_path, monkeypatch, capsys, "bad",
                              global_providers={"claude": {"spawn": {"args": STALE_ARGS}}},
                              agents={"claude": "claude"})
    assert str(lay.gfile) in out and "claude" in out, out
    assert _problem_count(out) == _problem_count(out0) + 1, out


# ---------------------------------------------------------------------------
# TG-R3: {prompt} is never silently emptied
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("transport", [pf.TRANSPORT_STDIN, pf.TRANSPORT_FILE])
def test_tg_r3_no_launch_reaches_a_spawn_with_an_empty_argv_element(
        tmp_path, monkeypatch, transport):
    args = ["--in", pf.FILE_PLACEHOLDER, "-p", "{prompt}"] if transport == pf.TRANSPORT_FILE \
        else ["-p", "{prompt}"]
    lay = Layered(tmp_path, monkeypatch,
                  global_providers={"cust": _custom(transport, args)},
                  agents={"cust": "cust"})
    result = lay.rig.start("cust", "a real prompt")
    # whether it was refused is TG-R1's business; what reached the native is ours
    _no_empty_argv(lay.calls("cust"))
    assert _refusal(result) or all("" not in c["argv"] for c in lay.calls("cust"))


def test_tg_r3_the_shipped_override_incident_never_spawns_with_an_empty_prompt_arg(
        tmp_path, monkeypatch):
    lay = Layered(tmp_path, monkeypatch,
                  global_providers={"claude": {"spawn": {"args": STALE_ARGS}}},
                  agents={"claude": "claude"})
    lay.rig.start("claude", "real")
    _no_empty_argv(lay.calls("claude"))


def test_tg_r3_a_steer_or_retry_after_a_stale_resume_never_has_an_empty_argv_element(
        tmp_path, monkeypatch):
    lay = Layered(tmp_path, monkeypatch,
                  global_providers={"cust": _custom(
                      pf.TRANSPORT_STDIN, ["--flag"], ["--resume", "{session_id}", "{prompt}"])},
                  agents={"cust": "cust"})
    first = lay.rig.start("cust", "first")
    if first.get("agent_id") and not _refusal(first):
        lay.rig.steer(first["agent_id"], "second")
    _no_empty_argv(lay.calls("cust"))


# ---------------------------------------------------------------------------
# TG-R4: an explicit argv opt-in still works
# ---------------------------------------------------------------------------

def test_tg_r4_explicit_argv_with_prompt_in_args_delivers_a_short_prompt_in_argv(
        tmp_path, monkeypatch):
    lay = Layered(tmp_path, monkeypatch,
                  global_providers={"cust": _custom(pf.TRANSPORT_ARGV, STALE_ARGS)},
                  agents={"cust": "cust"})
    result = lay.rig.start("cust", "short prompt é")
    assert not _refusal(result), result
    (call,) = lay.calls("cust")
    element = call["argv"][call["argv"].index("-p") + 1]
    assert "short prompt é" in element and element.endswith("short prompt é\n") \
        or element.endswith("short prompt é")
    assert b"short prompt" not in call["stdin"]


def test_tg_r4_explicit_argv_override_of_a_shipped_stdin_provider_delivers_in_argv(
        tmp_path, monkeypatch):
    lay = Layered(tmp_path, monkeypatch,
                  global_providers={"claude": {"spawn": {
                      TRANSPORT_KEY: pf.TRANSPORT_ARGV, "args": STALE_ARGS}}},
                  agents={"claude": "claude"})
    result = lay.rig.start("claude", "hello argv")
    assert not _refusal(result), result
    (call,) = lay.calls("claude")
    assert any("hello argv" in a for a in call["argv"]), call["argv"]
    assert call["argv"].count("") == 0


def test_tg_r4_explicit_argv_over_the_limit_is_refused_with_pf_r4s_message_and_no_spawn(
        tmp_path, monkeypatch):
    lay = Layered(tmp_path, monkeypatch,
                  global_providers={"cust": _custom(pf.TRANSPORT_ARGV, STALE_ARGS)},
                  agents={"cust": "cust"})
    result = lay.rig.start("cust", pf.big_text())
    message = _refusal(result)
    assert message, "a 200 KiB argv prompt was not refused"
    assert TRANSPORT_KEY in message
    assert pf.TRANSPORT_STDIN in message or pf.TRANSPORT_FILE in message, message
    assert lay.calls("cust") == []


def test_tg_r4_the_argv_opt_in_is_not_mistaken_for_a_mismatch_by_doctor(
        tmp_path, monkeypatch, capsys):
    lay = Layered(tmp_path, monkeypatch,
                  global_providers={"cust": _custom(pf.TRANSPORT_ARGV, STALE_ARGS)},
                  agents={"cust": "cust"})
    _rc, out = lay.doctor(capsys)
    assert str(lay.gfile) not in out, out


# ---------------------------------------------------------------------------
# TG-R5: no regression — shipped providers launch as today
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("provider", ["claude", "opencode-go", "agy", "codex"])
def test_tg_r5_shipped_providers_with_no_override_still_launch_and_doctor_is_quiet(
        tmp_path, monkeypatch, capsys, provider):
    # a layer that only repoints `bin` at the fake: transport and args stay shipped
    lay = Layered(tmp_path, monkeypatch,
                  global_providers={provider: {"enabled": True}},
                  kinds={provider: pf.SHIPPED_PROVIDERS[provider][0]},
                  agents={provider: provider})
    if provider == "codex":
        profile = Path(os.path.expanduser("~")) / ".multiagents" / "profiles" / "codex"
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "auth.json").write_text("{}")
    result = lay.rig.start(provider, "hello shipped")
    assert not _refusal(result), result
    calls = lay.calls(provider)
    assert calls, "nothing was spawned"
    blocks = pf.shipped_blocks()
    block = blocks[provider]
    spawn = block.get("spawn") or blocks[block["extends"]]["spawn"]     # a route extends its CLI base
    _no_empty_argv(calls, spawn["args"].count(""))
    assert any(b"hello shipped" in pf.delivered_anywhere(c, provider) for c in calls)
    _rc, out = lay.doctor(capsys)
    assert "{prompt" not in out, out
