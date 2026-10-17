"""Slice 4C, D-S4C.1 / IC-C36 (agy is unsupported in isolated mode, with its
stated reason) and the IC-C34 placeholder (codex's mode waits for the live
experiment).

Fake Docker CLI; seams assumed: tests/support/ic_s4c.py (NEED_INFO(s4c-seams)).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_s4b as nb  # noqa: E402
import ic_s4c as c4  # noqa: E402
import ic_support as ic  # noqa: E402

ENDED = ("held", "failed", "done", "cancelled")


@pytest.fixture
def world(tmp_path, monkeypatch):
    s = c4.CredSched(tmp_path, monkeypatch, mode=None)          # the field is absent everywhere
    # a second provider named like the shipped one: it declares no mode of its own, so the shipped
    # (or the default) declaration for `agy` is what decides
    agy = s.p.add_provider("agy", models_include=["agy/*", "*"])
    agy.entry["needs"] = {"credentials": True, "network": False, "rpc": False}
    s.p.add_agent("agyworker", "agy", "agy/m1")
    s.p.write()
    s._reset_server()
    yield s
    s.close()


def agy_refusal(s):
    s.start()
    node = s.create(agent="agyworker")["id"]
    view = s.wait(lambda: (v := s.get(node)) and v["state"] in ENDED and v, nb.WAIT, "the node to end")
    assert view["state"] != "done", "agy must be refused in isolated mode, it ran"
    return node, view


def test_c36_agy_is_refused_with_unsupported_credential_handle(world):
    node, view = agy_refusal(world)
    causes = [c for c in ic.causes(view) if "credential_handle" in ic.text_of(c).lower()]
    assert causes, ic.text_of(ic.causes(view))[:500]
    assert "unsupported" in ic.text_of(causes[0]).lower()
    assert causes[0].get("retryable", False) is False


def test_c36_agy_runs_nowhere_no_container_no_host_cli_no_fallback_to_shared_mode(world):
    agy_refusal(world)
    assert world.fd.containers() == {} and world.fd.networks() == {}
    assert world.fake.spawns() == 0
    assert world.p.fakes["agy"].calls.read_text() == "", "the agy CLI was started on the host"


def test_c36_the_refusal_states_why_agy_is_unsupported(world):
    """The reason is what the operator reads: agy holds one opaque keyring token and no gateway exists for it."""
    node, view = agy_refusal(world)
    text = ic.text_of(ic.causes(view)).lower()
    assert "agy" in text, "the refusal must say which provider it is about"
    assert "gateway" in text, "the stated reason must say that agy has no auth gateway yet"


def test_c36_agy_gets_no_credential_material_even_when_the_hosts_vault_holds_a_token(world):
    c4.write_projection_vault(world.ma_state / "container-state" / "shared" / "agy" / "vault")
    agy_refusal(world)
    leaks = c4.leaks_of(c4.tree_texts(world.root), [c4.OAUTH_ACCESS, c4.API_KEY, c4.OAUTH_REFRESH])
    assert leaks == {}


# ===================================================================== IC-C34 (placeholder)

@pytest.mark.xfail(reason="IC-C34 experiment pending", run=False, strict=True)
def test_c34_codex_isolated_mode_is_pinned_by_the_recorded_experiment():
    """The codex access-only experiment decides `projection` or `unsupported`. When it is recorded, replace this
    with a test pinning whichever mode results (and, for `unsupported`, the stated fallback). Until then codex's
    mode is deliberately untested."""
    raise AssertionError("IC-C34 experiment pending")
