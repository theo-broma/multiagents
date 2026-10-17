"""Slice 4C, D-S4C.3..6 / IC-C35: projection mechanics for a fake projection
provider modelled on opencode (an `auth.json` store, `isolated_credentials:
projection`).

Through the scheduler on the fake Docker CLI. Observed in Docker's vocabulary
(mounts, start order), in the host files behind the mounts, and in the host's
public views. Seams assumed (S3..S6): tests/support/ic_s4c.py,
NEED_INFO(s4c-seams). Real-Docker cases: test_s4c_docker.py.
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_s4b as nb  # noqa: E402
import ic_s4c as c4  # noqa: E402

SETTLE = 1.0              # a bounded negative wait: no second refresh may follow the first


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


def projected(s, container) -> Path:
    path = s.projected_path(container)
    assert path is not None, "no mount covers the guest path of the projected file"
    return path


def read_projection(path: Path):
    return json.loads(path.read_text())


def replace_vault_atomically(s, data):
    tmp = s.vault / "auth.json.swap"
    tmp.write_text(json.dumps(data))
    os.replace(tmp, s.vault / "auth.json")


def expire_vault(s):
    data = json.loads((s.vault / "auth.json").read_text())
    data["fakecode"]["expires"] = c4.PAST
    replace_vault_atomically(s, data)


def access_of(path: Path):
    try:
        return read_projection(path)["fakecode"]["access"]
    except (OSError, ValueError, KeyError):
        return None


# ============================================================ placement, content, mounts

def test_c35_the_projection_exists_in_the_private_home_before_the_cli_starts(proj):
    s = proj()
    s.start()
    node = s.launch()
    container = s.running(node)
    (record,) = [r for r in s.started() if r["ref"] == container["Id"]]
    info, text = c4.snapshot_file(record, s.home_of(container))
    assert text is not None, "the projected file was not in place when the container started"
    assert json.loads(text)["fakecode"]["access"] == c4.OAUTH_ACCESS


def test_c35_the_projection_carries_the_access_token_and_the_api_key_and_no_refresh_material(proj):
    s = proj()
    s.start()
    container = s.running(s.launch())
    data = read_projection(projected(s, container))
    assert data["fakecode"]["access"] == c4.OAUTH_ACCESS
    assert data["fakekey"]["key"] == c4.API_KEY
    assert "refresh" not in set(c4.keys_anywhere(data)), "refresh material in the projection"
    assert c4.leaks_of(s.guest_files(container, with_input=True), [c4.OAUTH_REFRESH]) == {}
    assert c4.OAUTH_REFRESH not in repr(s.invocation(container))


def test_c35_the_projection_is_mounted_as_a_directory_never_a_file_bind(proj):
    s = proj()
    s.start()
    container = s.running(s.launch())
    guest = s.home_of(container).rstrip("/") + "/" + c4.PROJECTED_FILE
    mounts = s.credential_mounts(container)
    assert guest not in [m["destination"] for m in mounts], "the file itself is bound"
    covering = [m for m in mounts if guest.startswith(m["destination"].rstrip("/") + "/")]
    assert covering, "no directory mount covers the projected file"
    assert all(Path(m["source"]).is_dir() for m in covering)
    assert all(Path(m["source"]).is_dir() for m in mounts if m["source"]), "a mount source is a single file"


def test_c35_the_projected_directory_holds_only_the_projection_no_temporary_leftovers(proj):
    s = proj()
    s.start()
    path = projected(s, s.running(s.launch()))
    assert sorted(p.name for p in path.parent.iterdir()) == ["auth.json"]


def test_c35_each_attempt_has_its_own_projection_and_ending_one_leaves_the_other(proj):
    s = proj()
    s.start()
    a, b = s.launch(), s.launch()
    ca, cb = s.running(a), s.running(b)
    pa, pb = projected(s, ca), projected(s, cb)
    assert pa != pb and pa.parent != pb.parent
    s.fd.finish(ca["Id"], 0)
    s.wait(lambda: not pa.exists(), nb.WAIT, "attempt A's projection to be deleted")
    assert access_of(pb) == c4.OAUTH_ACCESS, "ending one attempt deleted or damaged another's projection"


def test_c35_the_projection_never_leaves_the_private_home_or_the_public_views(proj):
    s = proj()
    s.start()
    node = s.launch()
    container = s.running(node)
    secrets = [c4.OAUTH_ACCESS, c4.API_KEY, c4.OAUTH_REFRESH]
    assert c4.leaks_of(s.public_texts(node), secrets) == {}, "a projected secret is in a public view"
    env = s.invocation(container)["env"]
    assert c4.leaks_of({"env": repr(env)}, secrets) == {}, "a projected secret is in the guest environment"


# ============================================================ redaction (D-S4C.5)

def test_c35_every_projected_secret_joins_the_redactors_list(proj):
    s = proj()
    s.start()
    container = s.running(s.launch())
    listed = s.invocation(container).get("redaction_secrets") or []
    assert c4.API_KEY in listed and c4.OAUTH_ACCESS in listed, listed and [len(x) for x in listed]


# ============================================================ renewal during the run (D-S4C.4)

def test_c35_a_token_expiring_in_the_vault_is_renewed_host_side_while_the_attempt_runs(proj):
    s = proj()
    s.start()
    node = s.launch()
    container = s.running(node)
    path = projected(s, container)
    assert s.refreshes() == 0
    expire_vault(s)
    new = s.wait(lambda: (a := access_of(path)) not in (None, c4.OAUTH_ACCESS) and a, c4.RENEW_WAIT,
                 "the projection to be renewed")
    assert new.startswith("FICTION-RENEWED-ACCESS-")
    assert c4.vault_entry(s.vault / "auth.json")["access"] == new, "the vault must hold the renewed credential"
    data = read_projection(path)
    assert "refresh" not in set(c4.keys_anywhere(data)), "renewal must not project refresh material"
    assert data["fakekey"]["key"] == c4.API_KEY
    rotated = c4.vault_entry(s.vault / "auth.json")["refresh"]
    assert c4.leaks_of(s.guest_files(container, with_input=True), [rotated, c4.OAUTH_REFRESH]) == {}


def test_c35_renewal_never_cuts_the_attempt(proj):
    s = proj()
    s.start()
    node = s.launch()
    container = s.running(node)
    path = projected(s, container)
    expire_vault(s)
    s.wait(lambda: access_of(path) not in (None, c4.OAUTH_ACCESS), c4.RENEW_WAIT, "renewal")
    now = s.fd.container(container["Id"])
    assert now["State"]["Running"] and now["StartCount"] == 1
    assert s.get(node)["state"] == "running" and len(s.runs(node)) == 1


def test_c35_renewal_is_an_atomic_replacement_of_the_file(proj):
    s = proj()
    s.start()
    container = s.running(s.launch())
    path = projected(s, container)
    old_inode = path.stat().st_ino
    held = open(path)                                   # a reader that opened the old file
    bad = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                text = path.read_text()
            except FileNotFoundError:
                bad.append("missing")
                continue
            try:
                json.loads(text)
            except ValueError:
                bad.append(text[:40])
    t = threading.Thread(target=reader)
    t.start()
    try:
        expire_vault(s)
        s.wait(lambda: access_of(path) not in (None, c4.OAUTH_ACCESS), c4.RENEW_WAIT, "renewal")
    finally:
        stop.set()
        t.join()
    assert bad == [], "a reader saw the file missing or half-written during the renewal"
    assert path.stat().st_ino != old_inode, "renewal rewrote the file in place instead of replacing it"
    assert json.loads(held.read())["fakecode"]["access"] == c4.OAUTH_ACCESS, "the old file was modified in place"
    held.close()


def test_c35_a_vault_that_does_not_expire_is_never_refreshed(proj):
    s = proj()
    s.start()
    s.running(s.launch())
    deadline = time.monotonic() + SETTLE
    while time.monotonic() < deadline:
        assert s.refreshes() == 0
        time.sleep(0.05)


# ============================================================ the (provider, account) lock

def test_c35_two_attempts_on_one_account_never_refresh_concurrently_at_launch(proj):
    s = proj(expired=True, refresh_delay=0.6)
    s.start()
    a, b = s.launch(), s.launch()
    pa, pb = projected(s, s.running(a, 20)), projected(s, s.running(b, 20))
    assert s.max_overlap() <= 1, "two refreshes of one account overlapped"
    assert s.refreshes() == 1, f"one expired account must be refreshed once, not {s.refreshes()} times"
    renewed = c4.vault_entry(s.vault / "auth.json")["access"]
    assert access_of(pa) == access_of(pb) == renewed


def test_c35_two_running_attempts_on_one_account_share_one_renewal(proj):
    s = proj(refresh_delay=0.4)
    s.start()
    a, b = s.launch(), s.launch()
    pa, pb = projected(s, s.running(a)), projected(s, s.running(b))
    expire_vault(s)
    s.wait(lambda: all(access_of(p) not in (None, c4.OAUTH_ACCESS) for p in (pa, pb)), c4.RENEW_WAIT, "both renewals")
    deadline = time.monotonic() + SETTLE
    while time.monotonic() < deadline:
        time.sleep(0.05)
    assert s.max_overlap() <= 1
    assert s.refreshes() == 1
    assert access_of(pa) == access_of(pb) == c4.vault_entry(s.vault / "auth.json")["access"]


# ============================================================ deletion on every end path (D-S4C.6)

def end_exit0(s, node, container):
    s.fd.finish(container["Id"], 0)


def end_exit1(s, node, container):
    s.fd.finish(container["Id"], 1)


def end_oom(s, node, container):
    s.fd.finish(container["Id"], 137, oom=True)


def end_cancel(s, node, container):
    assert s.cancel_raw(node).get("ok") is True


def end_container_gone(s, node, container):
    s.fd.remove_behind_its_back(container["Id"])


def end_cancel_while_docker_cannot_stop_or_remove(s, node, container):
    s.fd.rule("stop", effect="fail")
    s.fd.rule("rm", effect="fail")
    s.fd.rule("kill", effect="fail")
    assert s.cancel_raw(node).get("ok") is True


ENDS = [end_exit0, end_exit1, end_oom, end_cancel, end_container_gone, end_cancel_while_docker_cannot_stop_or_remove]


@pytest.mark.parametrize("end", ENDS, ids=lambda f: f.__name__.removeprefix("end_"))
def test_c35_the_projection_is_deleted_on_every_end_path(proj, end):
    s = proj()
    s.start()
    node = s.launch()
    container = s.running(node)
    path = projected(s, container)
    assert path.is_file()
    end(s, node, container)
    s.wait(lambda: not path.exists(), nb.WAIT * 2, f"the projection to be deleted after {end.__name__}")
    secrets = [c4.OAUTH_ACCESS, c4.API_KEY]
    assert c4.leaks_of(c4.host_secret_texts(s), secrets) == {}, "a projected secret survives the attempt on the host"


def test_c35_deleting_again_is_harmless(proj):
    s = proj()
    s.start()
    node = s.launch()
    container = s.running(node)
    path = projected(s, container)
    assert s.cancel_raw(node).get("ok") is True
    s.wait(lambda: not path.exists(), nb.WAIT, "deletion")
    again = s.cancel_raw(node)                                  # a second end of the same attempt
    assert isinstance(again, dict)
    assert not path.exists()


def test_c35_a_projection_is_renewed_no_more_after_the_attempt_ended(proj):
    s = proj()
    s.start()
    node = s.launch()
    container = s.running(node)
    path = projected(s, container)
    s.fd.finish(container["Id"], 0)
    s.wait(lambda: not path.exists(), nb.WAIT, "deletion")
    expire_vault(s)
    deadline = time.monotonic() + SETTLE
    while time.monotonic() < deadline:
        assert not path.exists(), "the worker re-created a deleted projection"
        time.sleep(0.05)
    assert s.refreshes() == 0, "an ended attempt must not keep its account's refresh worker alive"
