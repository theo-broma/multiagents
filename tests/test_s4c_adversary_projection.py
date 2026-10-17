"""Adversary, slice 4C (D-S4C.2..4, IC-C35): what a projection carries, where it
lands, and whether renewal survives a scheduler restart.

All values are fictional; vaults are synthetic.
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_s4b as nb  # noqa: E402
import ic_s4c as c4  # noqa: E402
from multiagents.isolated_credentials import CredentialStore, Projection  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402
from multiagents.providers import load_providers  # noqa: E402

ANTHROPIC_ACCESS = "FICTION-ADV-ANTHROPIC-OAUTH-ACCESS-6c1e"
ANTHROPIC_REFRESH = "FICTION-ADV-ANTHROPIC-OAUTH-REFRESH-0b7d"
GO_KEY = "FICTION-ADV-OPENCODE-GO-KEY-44aa"


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTIAGENTS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("MULTIAGENTS_TEST_CREDENTIAL_REFRESH", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    return ProjectPaths(project)


def test_adv_s4c_an_opencode_projection_never_carries_an_anthropic_oauth_token(paths):
    # opencode's own auth store holds a Claude Pro/Max login as
    # {"anthropic": {"type": "oauth", "access", "refresh", "expires"}} next to
    # API keys. D-S4C.2: no Anthropic token reaches an isolated attempt;
    # D-S4C.3: opencode is a projection provider for its API keys.
    providers = load_providers({"opencode": {
        "bin": "opencode", "isolated_credentials": "projection",
        "isolated_projection_path": ".local/share/opencode"}})
    store = CredentialStore(paths, providers)
    source = store.source("opencode")
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({
        "opencode-go": {"type": "api", "key": GO_KEY},
        "anthropic": {"type": "oauth", "access": ANTHROPIC_ACCESS, "refresh": ANTHROPIC_REFRESH,
                      "expires": c4.FAR},
    }))
    data, secrets = store.projection("opencode")
    assert data.get("opencode-go", {}).get("key") == GO_KEY
    assert ANTHROPIC_ACCESS not in json.dumps(data), "an Anthropic OAuth access token is projected into the guest"
    assert ANTHROPIC_ACCESS not in secrets


class _Store:
    def __init__(self, providers):
        self.providers = providers

    def projection(self, name):
        return {"k": {"type": "api", "key": GO_KEY}}, [GO_KEY]


def test_adv_s4c_a_declared_isolated_projection_path_is_where_the_projection_lands(tmp_path):
    # A second opencode account written like the shipped codex-b example: its
    # own private home, inheriting opencode's `isolated_projection_path`.
    providers = load_providers({
        "opencode": {"bin": "opencode", "isolated_credentials": "projection",
                     "isolated_projection_path": ".local/share/opencode"},
        "opencode-b": {"extends": "opencode", "container_private_home": [".config/opencode", ".local/share/opencode"]},
    })
    assert providers["opencode-b"].isolated_projection_path == ".local/share/opencode"
    home = tmp_path / "home"
    home.mkdir()
    inputs = tmp_path / "input"
    inputs.mkdir()
    projection = Projection(_Store(providers), "opencode-b", home, inputs / "redaction-secrets.jsonl", interval=3600)
    try:
        projection.start()
        assert (home / ".local/share/opencode/auth.json").is_file(), (
            "the projection is not at the declared isolated_projection_path, where the CLI reads it; found: "
            + repr(sorted(str(p.relative_to(home)) for p in home.rglob("auth.json"))))
    finally:
        projection.revoke()


@pytest.fixture
def proj(tmp_path, monkeypatch):
    made = []

    def build(**kw):
        s = c4.CredSched(tmp_path, monkeypatch, mode="projection", **kw)
        made.append(s)
        return s
    yield build
    for s in made:
        s.close()


def _access(path):
    try:
        return json.loads(path.read_text())["fakecode"]["access"]
    except (OSError, ValueError, KeyError):
        return None


def test_adv_s4c_a_running_attempt_is_still_renewed_after_the_scheduler_restarts(proj):
    # D-S4C.4: the projection is renewed during the run. The container survives
    # a scheduler restart (adoption, IC-C15); its renewal must too.
    s = proj()
    s.start()
    node = s.launch()
    container = s.running(node)
    path = s.projected_path(container)
    assert path is not None and _access(path) == c4.OAUTH_ACCESS
    s.restart(hard=True)
    assert s.fd.container(container["Id"])["State"]["Running"], "precondition: the attempt outlives the restart"
    data = json.loads((s.vault / "auth.json").read_text())
    data["fakecode"]["expires"] = c4.PAST
    swap = s.vault / "auth.json.swap"
    swap.write_text(json.dumps(data))
    os.replace(swap, s.vault / "auth.json")
    deadline = time.monotonic() + c4.RENEW_WAIT
    while time.monotonic() < deadline and _access(path) in (None, c4.OAUTH_ACCESS):
        time.sleep(0.1)
    assert _access(path) not in (None, c4.OAUTH_ACCESS), (
        "after a scheduler restart the adopted attempt's projection is never renewed again "
        f"(refreshes={s.refreshes()}, vault expired, projection still {_access(path)!r})")
