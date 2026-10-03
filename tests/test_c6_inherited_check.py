"""C6-R1a: direct checks preserve inherited pins and their login owner."""

from dataclasses import replace

from multiagents import auth, scripts
from multiagents.providers import load_providers
from test_c6_pinned_auth_status import put, rig


def test_unpinned_borrower_detail_and_fix_name_the_login_owner(rig):
    raw = {**rig.raw, "pool_borrower": {"auth_from": "claude"}}
    providers = load_providers(raw)
    rig.ex.providers = providers
    put(rig, "default", offset=-60)
    state = auth.check("pool_borrower", providers["pool_borrower"],
                       rig.ex, rig.paths.config)
    assert state.status == "not_authenticated"
    assert state.fix == "multiagents auth login claude"
    assert state.fix in state.detail
    assert "multiagents auth login pool_borrower" not in state.detail


def test_direct_inherited_check_names_the_login_owner(rig):
    put(rig, "default")
    put(rig, "b", offset=-60)
    state = auth.check("heir", rig.providers["heir"], rig.ex, rig.paths.config)
    assert state.status == "not_authenticated"
    assert state.fix == "multiagents auth login second"
    assert "multiagents auth login second" in state.detail
    assert '"b": "expired"' in state.detail
    assert '"default"' not in state.detail


def test_direct_inherited_script_check_uses_the_owners_pin(rig):
    put(rig, "default")
    put(rig, "b", offset=-60)
    provider = replace(rig.providers["heir"],
                       script=rig.providers["second"].script_name)
    code, out, err = scripts.run_action(
        "heir", provider, rig.ex, "check", rig.paths.config)
    assert code == auth.NOT_AUTHENTICATED, out + err
    assert '"b": "expired"' in out
    assert '"default"' not in out
