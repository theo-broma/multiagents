"""Codex provider contract, ENGINE half — models and budget ids.

`context/specs/codex-provider.md`, amendments after the contract review:

- CX-C4  (replaced) with no `models_cmd`, `refresh-models` runs the provider's
         `models` action through `scripts.run_action` (env and resolution
         applied); exit 64 means "not implemented" and the provider is skipped
         quietly; a provider that declares `models_cmd` behaves as today.
- CX-C15 a budget script's optional numeric `stale_seconds` is kept in the
         `Budget` and reported by `budget_status`; a malformed value is ignored.

Black box through `multiagents refresh-models` (the signature of
`refresh_models()` is the implementer's), `budget.read_provider` and
`server.budget_status`. The provider is the made-up `acme`; the shipped
providers are disabled in the project's `providers.yaml` and PATH is reduced to
a fake bin dir plus the system dirs, so no real CLI runs. Nothing is stubbed.
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import pytest
import yaml

from multiagents import budget as budget_mod
from multiagents import cli, server
from multiagents.executor.local import LocalExecutor
from multiagents.paths import global_config_dir, shipped_defaults_dir
from multiagents.providers import Provider

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c3_harness as h3  # noqa: E402

NAME = "acme"


def _executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def _shipped_disabled() -> dict:
    shipped = yaml.safe_load((shipped_defaults_dir() / "providers.yaml").read_text())
    return {name: {"enabled": False} for name in shipped["providers"]}


class Project:
    def __init__(self, tmp_path: Path, monkeypatch, providers: dict):
        self.root = h3.make_git_repo((tmp_path / "proj").resolve())
        self.config = self.root / ".multiagents" / "config"
        self.scripts = self.config / "providers"
        self.scripts.mkdir(parents=True, exist_ok=True)
        (self.config / "providers.yaml").write_text(
            yaml.safe_dump({"providers": {**_shipped_disabled(), **providers}}))
        self.bin = tmp_path / "fakebin"
        _executable(self.bin / NAME, "#!/bin/sh\necho native\n")
        self.home = tmp_path / "home"
        self.home.mkdir()
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("PATH", f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin")
        monkeypatch.chdir(self.root)

    def refresh_models(self, capsys) -> tuple[dict, str]:
        rc = cli.main(["--path", str(self.root), "refresh-models"])
        out = capsys.readouterr().out
        assert rc in (0, None), out
        data = yaml.safe_load((self.config / "models.yaml").read_text()) or {}
        return data.get("models") or {}, out


def _acme_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.strip().split(" ")[0] == NAME]


# A `models` action that proves it ran with the provider's env (`~` expanded).
MODELS_SH = (
    "#!/bin/sh\n"
    "case \"$1\" in\n"
    "  models) printf 'acme-small\\tSmall\\n'; printf '%s\\tProfile\\n' \"$ACME_PROFILE\" ;;\n"
    "  *) exit 64 ;;\n"
    "esac\n"
)


def _ids(models: dict) -> list[str]:
    return [m.get("id") for m in models.get(NAME) or [] if isinstance(m, dict)]


# ===========================================================================
# CX-C4 — refresh-models runs the `models` action
# ===========================================================================

def test_cx_c4_models_action_runs_when_there_is_no_models_cmd(tmp_path, monkeypatch, capsys):
    p = Project(tmp_path, monkeypatch, {NAME: {
        "bin": NAME, "models_parse": "tsv", "spawn": {"args": ["x"]},
        "env": {"ACME_PROFILE": "~/acme-profile"}}})
    _executable(p.scripts / f"{NAME}.sh", MODELS_SH)
    models, out = p.refresh_models(capsys)
    ids = _ids(models)
    assert "acme-small" in ids, (
        f"no models_cmd: the `models` action must be run; models.yaml has {models!r}, "
        f"output:\n{out}")
    assert str(p.home / "acme-profile") in ids, (
        f"the action runs with provider.env, `~` expanded: {ids}")


def test_cx_c4_adapter_answers_the_models_action(tmp_path, monkeypatch, capsys):
    p = Project(tmp_path, monkeypatch, {NAME: {
        "bin": NAME, "adapter": "acme-adapter.py", "models_parse": "tsv",
        "spawn": {"args": ["x"]}}})
    _executable(p.scripts / "acme-adapter.py", (
        f"#!{sys.executable}\n"
        "import sys\n"
        "if sys.argv[1:2] == ['models']:\n"
        "    print('acme-from-adapter\\tAdapter'); sys.exit(0)\n"
        "sys.exit(64)\n"))
    models, out = p.refresh_models(capsys)
    assert "acme-from-adapter" in _ids(models), (
        f"`adapter:` without `script:` is the action script, `models` included; "
        f"models={models!r}\n{out}")


def test_cx_c4_exit_64_is_skipped_quietly(tmp_path, monkeypatch, capsys):
    p = Project(tmp_path, monkeypatch, {NAME: {
        "bin": NAME, "models_parse": "tsv", "spawn": {"args": ["x"]}}})
    _executable(p.scripts / f"{NAME}.sh", "#!/bin/sh\nexit 64\n")
    models, out = p.refresh_models(capsys)
    assert NAME not in models, models
    assert _acme_lines(out) == [], (
        f"exit 64 means `models` is not implemented: skipped quietly, no line "
        f"about {NAME}; got:\n{out}")


def test_cx_c4_other_nonzero_exit_is_reported(tmp_path, monkeypatch, capsys):
    p = Project(tmp_path, monkeypatch, {NAME: {
        "bin": NAME, "models_parse": "tsv", "spawn": {"args": ["x"]}}})
    _executable(p.scripts / f"{NAME}.sh",
                "#!/bin/sh\necho 'acme: models listing broke' >&2\nexit 3\n")
    models, out = p.refresh_models(capsys)
    assert NAME not in models, models
    lines = _acme_lines(out)
    assert lines and "models listing broke" in " ".join(lines), (
        f"a failing `models` action is a problem worth a line, with its reason; got:\n{out}")


def test_cx_c4_declared_models_cmd_behaves_as_today(tmp_path, monkeypatch, capsys):
    # Guard: `models_cmd` wins; the action is not consulted.
    p = Project(tmp_path, monkeypatch, {NAME: {
        "bin": NAME, "models_parse": "tsv", "spawn": {"args": ["x"]},
        "models_cmd": ["acme-lister"]}})
    _executable(p.bin / "acme-lister", "#!/bin/sh\nprintf 'acme-listed\\tListed\\n'\n")
    _executable(p.scripts / f"{NAME}.sh", MODELS_SH)
    models, _ = p.refresh_models(capsys)
    assert _ids(models) == ["acme-listed"], models


# ===========================================================================
# CX-C15 — stale_seconds from a budget script
# ===========================================================================

def _budget_from(tmp_path, payload: str):
    _executable(global_config_dir() / "providers" / f"{NAME}.sh",
                f"#!/bin/sh\ncase \"$1\" in budget) cat <<'EOF'\n{payload}\nEOF\n;; *) exit 64;; esac\n")
    provider = Provider.from_dict(NAME, {"bin": NAME, "spawn": {"args": ["x"]}})
    return budget_mod.read_provider(NAME, provider, LocalExecutor(), global_config_dir(),
                                    None, use_cache=False)


def _payload(stale) -> str:
    return json.dumps({"known": True, "headroom": 0.6, "source": "script"})[:-1] + \
        f', "stale_seconds": {stale}}}'


@pytest.mark.parametrize("raw, kept, reported", [(42.7, 42.7, 43), (0, 0, 0), (900, 900, 900)])
def test_cx_c15_numeric_stale_seconds_is_kept_and_reported(tmp_path, raw, kept, reported):
    b = _budget_from(tmp_path, _payload(json.dumps(raw)))
    assert b.known is True, b                     # the script was read at all
    assert b.stale_seconds == pytest.approx(kept), (
        f"stale_seconds={raw} from the budget script must be kept; got {b.stale_seconds!r}")
    assert b.to_dict().get("stale_seconds") == reported, b.to_dict()


@pytest.mark.parametrize("raw", ['"abc"', "[1, 2]", "null", "{}", "true"])
def test_cx_c15_malformed_stale_seconds_is_ignored(tmp_path, raw):
    b = _budget_from(tmp_path, _payload(raw))
    assert b.known is True and b.headroom == pytest.approx(0.6), (
        f"a malformed stale_seconds must not spoil the rest of the reading: {b}")
    assert b.stale_seconds is None, f"{raw} is not a number of seconds: {b.stale_seconds!r}"
    assert "stale_seconds" not in b.to_dict()


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
def test_cx_c15_non_finite_stale_seconds_never_breaks_the_report(tmp_path, raw):
    b = _budget_from(tmp_path, _payload(raw))
    data = b.to_dict()                             # must not raise
    value = data.get("stale_seconds")
    assert value is None or math.isfinite(value), data


def test_cx_c15_negative_stale_seconds_is_never_reported_negative(tmp_path):
    # NEED_INFO(stale-negative): ignored, or clamped to 0? Either passes here.
    b = _budget_from(tmp_path, _payload("-5"))
    value = b.to_dict().get("stale_seconds")
    assert value is None or value >= 0, b.to_dict()


def test_cx_c15_budget_status_reports_stale_seconds(tmp_path, monkeypatch):
    p = Project(tmp_path, monkeypatch, {NAME: {"bin": NAME, "spawn": {"args": ["x"]}}})
    _executable(p.scripts / f"{NAME}.sh",
                "#!/bin/sh\ncase \"$1\" in budget) echo '{\"known\": true, \"headroom\": 0.5, "
                "\"stale_seconds\": 120}' ;; *) exit 64;; esac\n")
    h3.as_root(monkeypatch)
    monkeypatch.setenv("MULTIAGENTS_PROJECT", str(p.root))
    server._reset()
    try:
        status = server.budget_status()
    finally:
        server._reset()
    if isinstance(status, str):
        status = json.loads(status)
    entry = (status.get("providers") or {}).get(NAME)
    assert entry is not None, f"{NAME} missing from budget_status: {sorted(status)}"
    assert entry.get("stale_seconds") == 120, entry
