"""C19 review decisions: fail closed on credentials/links and distinguish busy."""

import json
import threading
import time
import uuid
from urllib.parse import quote

import pytest

from multiagents.monitor import quota
from test_c19_quota_details_page import (
    Actions, claude_profile, codex, codex_profile, entries, make, reading,
    run_script, uniq, win,
)

pytestmark = pytest.mark.real_providers


def test_qd_r7_identity_capacity_exhaustion_returns_busy(make, monkeypatch):
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(quota, "_IDENTITY_SLOTS", slots)
    monkeypatch.setattr(quota, "IDENTITY_WAIT", 0.1)
    name = uniq("busy")
    acts = Actions()
    acts.identities[(name, None)] = "known@example.invalid"
    fx = make({name: reading(name, {"week": win(20)})}, acts)
    slots.acquire()
    try:
        resp = fx.reveal(name, None)
        assert resp.status == 200
        assert json.loads(resp.text) == {"identity": None, "status": "busy"}
        assert acts.calls == []
    finally:
        slots.release()
    assert fx.revealed(name, None) == "known@example.invalid"


def test_qd_r7_pending_refresh_keeps_availability_and_reveal_reports_busy(make, monkeypatch):
    monkeypatch.setattr(quota, "IDENTITY_WAIT", 0.1)
    name = uniq("refresh")
    acts = Actions()
    acts.identities[(name, None)] = "before@example.invalid"
    fx = make({name: reading(name, {"week": win(20)})}, acts)
    assert fx.revealed(name, None) == "before@example.invalid"
    with quota._LOCK:
        key = next(k for k in quota._CACHE if k[0] == str(fx.paths.config))
        stamp, identity = quota._CACHE[key]
        monkeypatch.setitem(quota._CACHE, key, (stamp - quota.IDENTITY_TTL - 1, identity))
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def refresh(_account):
        started.set()
        try:
            assert release.wait(5)
            return 0, json.dumps({"identity": "after@example.invalid", "kind": "email"}), ""
        finally:
            finished.set()

    acts.behave[name] = refresh
    try:
        row = fx.rows()[name]
        assert started.wait(1)
        assert all(e["identity_available"] for e in entries(row))
        resp = fx.reveal(name, None)
        assert json.loads(resp.text) == {"identity": None, "status": "busy"}
        assert all(e["identity_available"] for e in entries(fx.rows()[name]))
        assert "before@example.invalid" not in fx.get("/api/quota").text
    finally:
        release.set()
        assert finished.wait(2)
    assert fx.revealed(name, None) == "after@example.invalid"


@pytest.mark.parametrize("mode", ["claude-local", "claude-vault", "codex-local", "codex-backing"])
def test_qd_r4a_symlinked_trusted_root_ancestor_yields_identity(tmp_path, mode):
    real = tmp_path / "real"
    selected = real / "profile"
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    linked = alias / "profile"
    email = "outside@example.invalid"
    if mode.startswith("claude"):
        claude_profile(selected, email)
        env = {"HOME": str(linked), "MULTIAGENTS_EXECUTOR": "local"}
        if mode == "claude-vault":
            env.update(MULTIAGENTS_EXECUTOR="docker", MULTIAGENTS_PRIVATE_VAULT=str(linked))
        done = run_script("claude.sh", "identity", env, tmp_path)
    else:
        codex_profile(selected, {"email": email})
        env = {}
        if mode == "codex-backing":
            env.update(MULTIAGENTS_EXECUTOR="docker", MULTIAGENTS_PRIVATE_BACKING=str(linked))
        done = codex(tmp_path, linked, **env)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"identity": email, "kind": "email"}


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_qd_r4a_unfamiliar_nested_credential_values_cannot_be_an_identity(tmp_path, provider):
    secret = uuid.uuid4().hex
    email = f"{secret}@example.test"
    profile = tmp_path / "profile"
    if provider == "claude":
        claude_profile(profile, email, extra={"futureCredentials": [{"future_token": secret}]})
        done = run_script("claude.sh", "identity",
                          {"HOME": str(profile), "MULTIAGENTS_EXECUTOR": "local"}, tmp_path)
    else:
        codex_profile(profile, {"email": email})
        auth = profile / "auth.json"
        data = json.loads(auth.read_text())
        data["futureCredentials"] = [{"future_token": secret}]
        auth.write_text(json.dumps(data))
        done = codex(tmp_path, profile)
    assert done.returncode == 64
    assert not done.stdout.strip()
    assert secret not in done.stdout + done.stderr


def round3_identity(tmp_path, provider, email, extra, *, credentials=False):
    profile = tmp_path / "profile"
    if provider == "claude":
        claude_profile(profile, email)
        path = profile / (".credentials.json" if credentials else ".claude.json")
    else:
        codex_profile(profile, {"email": email})
        path = profile / "auth.json"
    data = json.loads(path.read_text())
    data["nested"] = [{"metadata": extra}]
    path.write_text(json.dumps(data))
    if provider == "claude":
        return run_script("claude.sh", "identity",
                          {"HOME": str(profile), "MULTIAGENTS_EXECUTOR": "local"}, tmp_path)
    return codex(tmp_path, profile)


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("key,value", [
    ("type", "admin"),
    ("opaque", "admin@example.invalid"),
    ("secret", "admin"),
    ("api_key", "admin@example.invalid"),
    ("secret", "admin@example.invalid "),
])
def test_qd_r4a_noncredential_and_short_values_do_not_hide_identity(tmp_path, provider, key, value):
    done = round3_identity(tmp_path, provider, "admin@example.invalid", {key: value})
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["identity"] == "admin@example.invalid"


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("transform", ["padded", "uppercase", "encoded", "double-encoded", "whitespace"])
def test_qd_r4a_normalized_credentials_inside_identity_are_refused(tmp_path, provider, transform):
    secret = "abcdefgh" + uuid.uuid4().hex[:16]
    token, claimed = secret, secret
    if transform == "padded":
        token = " \t" + secret + "\n "
    elif transform == "uppercase":
        claimed = secret.upper()
    elif transform in ("encoded", "double-encoded"):
        claimed = "".join(f"%{ord(c):02X}" for c in secret)
        if transform == "double-encoded":
            claimed = quote(claimed, safe="")
    else:
        token = " \t".join(secret)
    done = round3_identity(tmp_path, provider, f"{claimed}@example.test",
                           {"AcCeSs_ToKeN": token}, credentials=True)
    assert done.returncode == 64
    assert not done.stdout.strip()
    assert secret not in done.stdout + done.stderr


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("key", ["token", "secret", "password", "key", "refresh_token", "api_key", "client_secret"])
def test_qd_r4a_identity_contained_in_normalized_credential_is_refused(tmp_path, provider, key):
    email = "member@example.invalid"
    secret = f"prefix-{quote(email.upper(), safe='')}-suffix"
    done = round3_identity(tmp_path, provider, email, {key: secret}, credentials=True)
    assert done.returncode == 64
    assert not done.stdout.strip()


def test_qd_r4a_claude_credential_directory_symlink_beneath_home_is_refused(tmp_path):
    home, other = tmp_path / "home", tmp_path / "other"
    claude_profile(home, "selected@example.invalid")
    claude_profile(other, "other@example.invalid")
    (home / ".claude").symlink_to(other, target_is_directory=True)
    done = run_script("claude.sh", "identity",
                      {"HOME": str(home), "MULTIAGENTS_EXECUTOR": "local"}, tmp_path)
    assert done.returncode == 64
    assert not done.stdout.strip()
    assert "other@example.invalid" not in done.stdout + done.stderr


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_qd_r4a_hostile_percent_encoding_is_skipped_without_delay_or_leaks(tmp_path, provider):
    hostile = "%" + "25" * 50000
    started = time.monotonic()
    done = round3_identity(tmp_path, provider, "reader@example.invalid",
                           {"secret": hostile}, credentials=True)
    elapsed = time.monotonic() - started
    assert elapsed < 0.75, elapsed
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"identity": "reader@example.invalid", "kind": "email"}
    assert not done.stderr
    assert "%2525" not in done.stdout + done.stderr


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("size,multibyte", [(8192, False), (8193, False), (8192, True), (8193, True)])
def test_qd_r4a_credential_size_limit_is_measured_in_bytes(tmp_path, provider, size, multibyte):
    email = "reader@example.invalid"
    padding = size - len(email)
    secret = email + ("é" * (padding // 2) + "x" * (padding % 2)
                      if multibyte else "x" * padding)
    assert len(secret.encode()) == size
    done = round3_identity(tmp_path, provider, email, {"secret": secret}, credentials=True)
    if size <= 8192:
        assert done.returncode == 64
        assert not done.stdout.strip()
    else:
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout)["identity"] == email
    assert secret not in done.stdout + done.stderr


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_qd_r4a_oversized_candidate_identity_is_unknown(tmp_path, provider):
    email = "x" * 321 + "@example.test"
    done = round3_identity(tmp_path, provider, email, {"secret": "unrelated-value-123456"})
    assert done.returncode == 64
    assert not done.stdout.strip()
    assert email not in done.stdout + done.stderr


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_qd_r4a_three_decodes_still_reject_encoded_credentials(tmp_path, provider):
    secret = "abcdefgh" + uuid.uuid4().hex[:16]
    claimed = "".join(f"%{ord(c):02X}" for c in secret.upper())
    claimed = quote(quote(claimed, safe=""), safe="")
    done = round3_identity(tmp_path, provider, f"{claimed}@example.test",
                           {"secret": secret}, credentials=True)
    assert done.returncode == 64
    assert not done.stdout.strip()
    assert secret not in done.stdout + done.stderr
