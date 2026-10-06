"""GG-R7..R9 (extension 2026-10-06): placeholders, fingerprints, reporting.

Same throwaway-repo helpers as the other test_gg_* files. Every real-looking
value is assembled at run time, so no literal of the shape sits in this file:
the guard scans this repository's own pushes. The key directory is always a
temporary `XDG_STATE_HOME`; the real state directory is never touched.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import gg_world as gw  # noqa: E402
from gg_world import World, finding_lines, git, mask, said  # noqa: E402

from multiagents import config as config_mod  # noqa: E402
from multiagents.config import Config  # noqa: E402
from multiagents.paths import ProjectPaths, shipped_defaults_dir  # noqa: E402
from multiagents.runner import Runner  # noqa: E402

EMAIL = gw.email()
OTHER = gw.email("another", "corp-mail.test")
FP_RX = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{16}(?![0-9A-Za-z])")

TS, NET = "ts", "net"
REAL_HOST = "phone.tail-" + NET + "." + TS + "." + NET          # label before ts.net is not "example"
PRIV_HEAD = "-----BEGIN "
PRIV_TAIL = " PRIVATE" + " KEY-----"


def _scan_world(tmp_path, name="xdg", **kw) -> World:
    w = World(tmp_path, **kw)
    w.env["XDG_STATE_HOME"] = str(tmp_path / name)
    return w


@pytest.fixture
def w(tmp_path):
    return _scan_world(tmp_path)


def key_file(w: World) -> Path:
    return Path(w.env["XDG_STATE_HOME"]) / "multiagents" / "guard-key"


def lines_of(p, sha: str) -> list[str]:
    return finding_lines(p, sha)


def fingerprint_of(line: str) -> str | None:
    found = FP_RX.findall(line)
    return found[-1] if found else None


def fps(p, sha: str) -> list[str | None]:
    return [fingerprint_of(ln) for ln in lines_of(p, sha)]


def set_config(w: World, **guard) -> None:
    cfg = w.root / ".multiagents" / "config" / "project.yaml"
    data = yaml.safe_load(cfg.read_text())
    data["git"]["guard"].update(guard)
    cfg.write_text(yaml.safe_dump(data))


def assert_no_leak(w: World, p, *unmasked: str):
    text = said(p)
    files = w.all_output_files()
    xdg = Path(w.env["XDG_STATE_HOME"])
    if xdg.is_dir():
        files += [f for f in xdg.rglob("*") if f.is_file()]
    for secret in unmasked:
        assert secret not in text, "unmasked match in output"
        assert secret[2:] not in text, "unmasked tail of the match in output"
        for f in files:
            assert secret.encode() not in f.read_bytes(), f"unmasked match stored in {f}"


# ================================================================ GG-R7 ======
def commit_line(w: World, text: str, name: str = "n.txt") -> str:
    return w.commit_file(name, "x\ny\n" + text + "\n")


@pytest.mark.parametrize("text", [
    "host phone.example." + TS + "." + NET,
    "host <name>.<tailnet>." + TS + "." + NET,
    "host a-b.c.example." + TS + "." + NET + " and more",
    "https://box.example." + TS + "." + NET + ":8443/path",
    "<anything>." + TS + "." + NET,
])
def test_gg_r7_a_placeholder_tailnet_host_is_not_a_finding(w, text):
    commit_line(w, text)
    p = w.scan()
    assert p.returncode == 0, said(p)


@pytest.mark.parametrize("host", [
    REAL_HOST,
    "phone." + TS + "." + NET,
    "notexample." + TS + "." + NET,
    "example.tail-" + NET + "." + TS + "." + NET,       # "example" is not the label before ts.net
])
def test_gg_r7_near_miss_tailnet_host_is_still_a_finding(w, host):
    sha = commit_line(w, "host " + host)
    p = w.scan()
    assert p.returncode == 1, said(p)
    lines = lines_of(p, sha)
    assert len(lines) == 1 and "tailnet-host" in lines[0], said(p)
    assert_no_leak(w, p, host)


def test_gg_r7_a_placeholder_host_beside_a_real_one_leaves_one_finding(w):
    sha = commit_line(w, "a phone.example." + TS + "." + NET + " b " + REAL_HOST)
    p = w.scan()
    assert p.returncode == 1
    lines = lines_of(p, sha)
    assert len(lines) == 1 and mask(REAL_HOST) in lines[0], said(p)


@pytest.mark.parametrize("text", [
    "range 100.64.0.0/10",
    "(100.64.0.0/10)",
    "100.64.0.0/10.",
    "allow 100.64.0.0/10 only",
])
def test_gg_r7_the_cidr_text_is_not_a_finding(w, text):
    commit_line(w, text)
    p = w.scan()
    assert p.returncode == 0, said(p)


def test_gg_r7_a_bare_address_in_the_range_is_still_a_finding(w):
    ip = gw.tailnet_ip()
    sha = commit_line(w, "ssh " + ip)
    p = w.scan()
    assert p.returncode == 1, said(p)
    assert any("tailnet-ip" in ln for ln in lines_of(p, sha))
    assert_no_leak(w, p, ip)


def test_gg_r7_the_network_address_without_its_prefix_is_still_a_finding(w):
    sha = commit_line(w, "net " + gw.tailnet_ip("64", "0", "0"))
    p = w.scan()
    assert p.returncode == 1, said(p)
    assert any("tailnet-ip" in ln for ln in lines_of(p, sha))


def test_gg_r7_the_cidr_text_does_not_hide_a_bare_address_on_the_same_line(w):
    ip = gw.tailnet_ip()
    sha = commit_line(w, "range 100.64.0.0/10 host " + ip)
    p = w.scan()
    assert p.returncode == 1, said(p)
    lines = lines_of(p, sha)
    assert len(lines) == 1 and "tailnet-ip" in lines[0] and mask(ip) in lines[0], said(p)


@pytest.mark.parametrize("key_type", ["…", "<TYPE>", "RSA…", "<…>"])
def test_gg_r7_a_placeholder_private_key_line_is_not_a_finding(w, key_type):
    commit_line(w, PRIV_HEAD + key_type + PRIV_TAIL)
    p = w.scan()
    assert p.returncode == 0, said(p)


def test_gg_r7_a_real_armour_line_is_still_a_finding(w):
    sha = commit_line(w, gw.private_key_header())
    p = w.scan()
    assert p.returncode == 1, said(p)
    assert any("private-key" in ln for ln in lines_of(p, sha))


@pytest.mark.parametrize("kind", ["RSA", "EC", "OPENSSH", "DSA", "ENCRYPTED"])
def test_gg_r7_real_armour_of_any_ordinary_type_is_still_a_finding(w, kind):
    sha = commit_line(w, PRIV_HEAD + kind + PRIV_TAIL)
    p = w.scan()
    assert p.returncode == 1, said(p)
    assert any("private-key" in ln for ln in lines_of(p, sha))


def test_gg_r7_a_placeholder_key_line_beside_a_real_block_leaves_one_finding(w):
    body = (PRIV_HEAD + "<TYPE>" + PRIV_TAIL + "\n" + gw.private_key_block() + "\n")
    sha = w.commit_file("k.txt", body)
    p = w.scan()
    assert p.returncode == 1
    lines = [ln for ln in lines_of(p, sha) if "private-key" in ln]
    assert len(lines) == 1 and "k.txt:2" in lines[0], said(p)


def test_gg_r7_placeholder_exemptions_hold_in_commit_messages_too(w):
    w.commit("see phone.example." + TS + "." + NET + " on 100.64.0.0/10", add=False)
    p = w.scan()
    assert p.returncode == 0, said(p)


# ================================================================ GG-R8 ======
def two_findings(w: World):
    sha1 = w.commit_file("one.txt", "m " + EMAIL + "\n")
    sha2 = w.commit_file("two.txt", "m " + OTHER + "\n")
    return sha1, sha2


def test_gg_r8_every_finding_line_ends_with_category_sha_location_mask_and_fingerprint(w):
    sha = w.commit_file("src/a.txt", "x\ny\nm " + EMAIL + "\n")
    p = w.scan()
    assert p.returncode == 1, said(p)
    (line,) = lines_of(p, sha)
    assert "email" in line and "src/a.txt:3" in line and mask(EMAIL) in line
    assert fingerprint_of(line), f"no 16-hex fingerprint on the finding line: {line!r}"


def test_gg_r8_a_fingerprint_is_exactly_16_hex_characters(w):
    sha = w.commit_file("a.txt", "m " + EMAIL + "\n")
    p = w.scan()
    (line,) = lines_of(p, sha)
    fp = fingerprint_of(line)
    assert fp and re.fullmatch(r"[0-9a-fA-F]{16}", fp), line


def test_gg_r8_the_same_match_has_the_same_fingerprint_across_runs_commits_and_files(w):
    first = w.commit_file("a.txt", "m " + EMAIL + "\n")
    second = w.commit_file("deep/b.txt", "other\n\nagain " + EMAIL + "\n")
    p1, p2 = w.scan(), w.scan()
    f1 = fps(p1, first) + fps(p1, second)
    f2 = fps(p2, first) + fps(p2, second)
    assert len(f1) == 2 and None not in f1, said(p1)
    assert f1[0] == f1[1], "same match, different location"
    assert f1 == f2, "same match, second run"


def test_gg_r8_different_matches_have_different_fingerprints(w):
    s1, s2 = two_findings(w)
    p = w.scan()
    (a,), (b,) = fps(p, s1), fps(p, s2)
    assert a and b and a != b


def test_gg_r8_a_different_key_gives_a_different_fingerprint(tmp_path):
    a = _scan_world(tmp_path / "a", "xdg")
    b = _scan_world(tmp_path / "b", "xdg")
    out = []
    for world in (a, b):
        sha = world.commit_file("a.txt", "m " + EMAIL + "\n")
        out.append(fps(world.scan(), sha))
    assert out[0][0] and out[1][0] and out[0] != out[1]


def test_gg_r8_the_fingerprint_is_an_hmac_sha256_of_the_exact_match_truncated_to_16(w):
    sha = w.commit_file("a.txt", "m " + EMAIL + "\n")
    p = w.scan()
    (fp,) = fps(p, sha)
    assert key_file(w).is_file(), "no key file at $XDG_STATE_HOME/multiagents/guard-key"
    raw = key_file(w).read_bytes()
    candidates = {raw, raw.strip()}
    for decode in (bytes.fromhex, base64.b64decode, base64.urlsafe_b64decode):
        try:
            candidates.add(decode(raw.strip()))
        except Exception:
            pass
    expected = {hmac.new(k, EMAIL.encode(), hashlib.sha256).hexdigest()[:16] for k in candidates}
    assert fp and fp.lower() in expected, "not HMAC-SHA256(key, match)[:16] for the stored key"


def test_gg_r8_the_key_is_created_on_first_use_with_modes_0600_in_0700(w):
    assert not key_file(w).exists()
    w.commit_file("a.txt", "m " + EMAIL + "\n")
    assert w.scan().returncode == 1
    kf = key_file(w)
    assert kf.is_file() and kf.stat().st_size > 0
    assert stat.S_IMODE(kf.stat().st_mode) == 0o600
    assert stat.S_IMODE(kf.parent.stat().st_mode) == 0o700


def test_gg_r8_the_modes_hold_under_a_permissive_umask(w):
    w.commit_file("a.txt", "m " + EMAIL + "\n")
    old = os.umask(0)
    try:
        assert w.scan().returncode == 1
    finally:
        os.umask(old)
    kf = key_file(w)
    assert stat.S_IMODE(kf.stat().st_mode) == 0o600
    assert stat.S_IMODE(kf.parent.stat().st_mode) == 0o700


def test_gg_r8_the_key_is_kept_between_runs(w):
    w.commit_file("a.txt", "m " + EMAIL + "\n")
    w.scan()
    first = key_file(w).read_bytes()
    w.scan()
    assert key_file(w).read_bytes() == first


def test_gg_r8_the_real_state_directory_is_not_used(tmp_path, monkeypatch):
    home_state = tmp_path / "home" / ".local" / "state"
    w = _scan_world(tmp_path)
    w.commit_file("a.txt", "m " + EMAIL + "\n")
    w.scan()
    assert not (home_state / "multiagents" / "guard-key").exists()


def test_gg_r8_a_listed_fingerprint_suppresses_exactly_that_finding(w):
    s1, s2 = two_findings(w)
    p = w.scan()
    (fp1,), (fp2,) = fps(p, s1), fps(p, s2)
    set_config(w, allow_fingerprints=[fp1])
    p = w.scan()
    assert p.returncode == 1, said(p)
    assert not lines_of(p, s1) and len(lines_of(p, s2)) == 1, said(p)
    set_config(w, allow_fingerprints=[fp1, fp2])
    p = w.scan()
    assert p.returncode == 0, said(p)


def test_gg_r8_a_listed_fingerprint_covers_every_occurrence_of_that_match(w):
    s1 = w.commit_file("a.txt", "m " + EMAIL + "\n")
    s2 = w.commit_file("b.txt", "again " + EMAIL + "\n")
    (fp,) = fps(w.scan(), s1)
    set_config(w, allow_fingerprints=[fp])
    p = w.scan()
    assert p.returncode == 0, said(p)


def test_gg_r8_an_unrelated_or_empty_list_suppresses_nothing(w):
    sha = w.commit_file("a.txt", "m " + EMAIL + "\n")
    (fp,) = fps(w.scan(), sha)
    other = "0" * 16 if fp.lower() != "0" * 16 else "f" * 16
    for listed in ([], [other], [fp[:8]], [fp + "0"]):
        set_config(w, allow_fingerprints=listed)
        p = w.scan()
        assert p.returncode == 1 and len(lines_of(p, sha)) == 1, (listed, said(p))


def test_gg_r8_a_fingerprint_from_another_key_does_not_suppress(tmp_path):
    a = _scan_world(tmp_path / "a", "xdg")
    b = _scan_world(tmp_path / "b", "xdg")
    sha_a = a.commit_file("a.txt", "m " + EMAIL + "\n")
    sha_b = b.commit_file("a.txt", "m " + EMAIL + "\n")
    (fp_a,) = fps(a.scan(), sha_a)
    assert fp_a, said(a.scan())
    set_config(b, allow_fingerprints=[fp_a])
    p = b.scan()
    assert p.returncode == 1 and lines_of(p, sha_b), said(p)


def test_gg_r8_the_listed_fingerprint_also_covers_message_and_author_findings(w):
    sha = w.commit("by " + EMAIL, add=False)
    (fp,) = fps(w.scan(), sha)
    assert fp
    set_config(w, allow_fingerprints=[fp])
    assert w.scan().returncode == 0


def test_gg_r8_allow_and_allow_fingerprints_work_together(w):
    s1, s2 = two_findings(w)
    (fp2,) = fps(w.scan(), s2)
    set_config(w, allow=[EMAIL], allow_fingerprints=[fp2])
    p = w.scan()
    assert p.returncode == 0, said(p)


def test_gg_r8_scan_output_ends_with_a_block_to_paste_into_the_config(w):
    s1, s2 = two_findings(w)
    p = w.scan()
    assert p.returncode == 1
    text = said(p)
    assert "allow_fingerprints" in text, text
    tail = text[text.rindex("allow_fingerprints"):]
    assert not [ln for ln in tail.splitlines() if s1[:7] in ln or s2[:7] in ln], \
        "a finding line comes after the paste block"
    in_lines = {fingerprint_of(ln) for ln in lines_of(p, s1) + lines_of(p, s2)}
    in_block = set(FP_RX.findall(tail))
    assert None not in in_lines and len(in_lines) == 2
    assert in_block == in_lines, (in_block, in_lines)


def test_gg_r8_pasting_the_block_into_the_config_silences_the_scan(w):
    two_findings(w)
    text = said(w.scan())
    tail = text[text.rindex("allow_fingerprints"):]
    set_config(w, allow_fingerprints=sorted(set(FP_RX.findall(tail))))
    p = w.scan()
    assert p.returncode == 0, said(p)
    assert "allow_fingerprints" not in said(p)


def test_gg_r8_no_findings_no_block(w):
    w.commit_file("ok.txt", "nothing to see\n")
    p = w.scan()
    assert p.returncode == 0
    assert "allow_fingerprints" not in said(p)


def test_gg_r8_the_scan_never_writes_the_config(w):
    two_findings(w)
    cfg = w.root / ".multiagents" / "config" / "project.yaml"
    before = cfg.read_bytes()
    assert w.scan().returncode == 1
    assert cfg.read_bytes() == before
    assert sorted(p.name for p in cfg.parent.iterdir()) == ["project.yaml"]


@pytest.mark.parametrize("name,match", [
    ("email", EMAIL), ("token", gw.tokens()["ghp"]), ("tailnet-ip", gw.tailnet_ip()),
    ("tailnet-host", REAL_HOST),
])
def test_gg_r8_the_unmasked_match_appears_in_no_output_and_no_stored_file(w, name, match):
    sha = w.commit_file("n.txt", "x " + match + "\n")
    p = w.scan()
    assert p.returncode == 1, said(p)
    assert [fingerprint_of(ln) for ln in lines_of(p, sha)] != [None]
    assert_no_leak(w, p, match)


def test_gg_r8_the_fingerprint_does_not_contain_the_match(w):
    sha = w.commit_file("n.txt", "x " + gw.tokens()["aws"] + "\n")
    (fp,) = fps(w.scan(), sha)
    assert fp and gw.tokens()["aws"][:8].lower() not in fp.lower()


# ---- config validation of allow_fingerprints ------------------------------
def _load(tmp_path, monkeypatch, git_section):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("MULTIAGENTS_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "proj"
    (root / ".multiagents" / "config").mkdir(parents=True)
    (root / ".multiagents" / "config" / "project.yaml").write_text(
        yaml.safe_dump({"git": git_section}))
    return config_mod.load(ProjectPaths(root), seed=False)


@pytest.mark.parametrize("value", ["a-string", 5, True, {"a": "b"}, [1], [None], ["ok", 2], [["nested"]]])
def test_gg_r8_allow_fingerprints_must_be_a_list_of_strings(tmp_path, monkeypatch, value):
    with pytest.raises(ValueError) as exc:
        _load(tmp_path, monkeypatch, {"guard": {"allow_fingerprints": value}})
    assert "allow_fingerprints" in str(exc.value)


@pytest.mark.parametrize("value", [[], ["0123456789abcdef"], ["0123456789abcdef", "fedcba9876543210"]])
def test_gg_r8_valid_allow_fingerprints_load(tmp_path, monkeypatch, value):
    cfg = _load(tmp_path, monkeypatch, {"guard": {"allow_fingerprints": value}})
    assert cfg.project["git"]["guard"]["allow_fingerprints"] == value


# ================================================================ GG-R9 ======
def make_runner(tmp_path, monkeypatch, allow_fingerprints=None):
    w = _scan_world(tmp_path)
    for key in [k for k in os.environ if k.startswith(("GIT_", "MULTIAGENTS_", "XDG_"))]:
        monkeypatch.delenv(key, raising=False)
    for key in ("HOME", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT",
                "MULTIAGENTS_STATE_DIR", "MULTIAGENTS_CONFIG_DIR", "XDG_STATE_HOME"):
        monkeypatch.setenv(key, w.env[key])
    guard = {"patterns_file": str(w.patterns)}
    if allow_fingerprints is not None:
        guard["allow_fingerprints"] = allow_fingerprints
    section = {"remote": str(w.bare), "base_branch": "main", "push_agent_branches": False,
               "guard": guard}
    w.write_config({"git": section})
    paths = ProjectPaths(w.root)
    paths.ensure()
    runner = Runner(paths, Config(project={"git": section}, providers={}, agents={},
                                  models={}, instruction_dirs=[]))
    return w, runner


def test_gg_r9_a_push_branch_refusal_carries_a_fingerprint_on_every_finding(tmp_path, monkeypatch):
    w, r = make_runner(tmp_path, monkeypatch)
    sha = w.commit_file("src/a.txt", "x\ny\nm " + EMAIL + "\nn " + OTHER + "\n")
    result = r.push_branch(None)
    assert result.get("reason") == "guard" and result.get("ok") is False, result
    findings = result["findings"]
    assert len(findings) == 2, result
    for f in findings:
        assert re.fullmatch(r"[0-9a-fA-F]{16}", str(f.get("fingerprint", ""))), f
    assert len({f["fingerprint"] for f in findings}) == 2
    blob = json.dumps(findings, ensure_ascii=False)
    assert "email" in blob and sha[:7] in blob and "src/a.txt:3" in blob and mask(EMAIL) in blob
    assert EMAIL not in blob and OTHER not in blob


def test_gg_r9_the_push_branch_fingerprint_equals_the_scans(tmp_path, monkeypatch):
    w, r = make_runner(tmp_path, monkeypatch)
    sha = w.commit_file("a.txt", "m " + EMAIL + "\n")
    (scan_fp,) = fps(w.scan(), sha)
    (finding,) = r.push_branch(None)["findings"]
    assert scan_fp and finding.get("fingerprint", "").lower() == scan_fp.lower()


def test_gg_r9_push_branch_honours_a_listed_fingerprint(tmp_path, monkeypatch):
    w, r = make_runner(tmp_path, monkeypatch)
    sha = w.commit_file("a.txt", "m " + EMAIL + "\n")
    (finding,) = r.push_branch(None)["findings"]
    assert w.remote_refs().get("refs/heads/main") != sha
    w2, r2 = w, Runner(r.paths, Config(
        project={"git": {"remote": str(w.bare), "base_branch": "main", "push_agent_branches": False,
                         "guard": {"patterns_file": str(w.patterns),
                                   "allow_fingerprints": [finding["fingerprint"]]}}},
        providers={}, agents={}, models={}, instruction_dirs=[]))
    w.write_config({"git": {"remote": str(w.bare), "base_branch": "main", "push_agent_branches": False,
                            "guard": {"patterns_file": str(w.patterns),
                                      "allow_fingerprints": [finding["fingerprint"]]}}})
    result = r2.push_branch(None)
    assert result.get("pushed") is True, result
    assert w.remote_refs()["refs/heads/main"] == sha


# ---- the composed orchestrator brief ---------------------------------------
TEAMS = ("implement", "review")


def _cli(root: Path, env: dict, *args: str):
    return subprocess.run([sys.executable, "-m", "multiagents.cli", *args], cwd=str(root),
                          env=env, capture_output=True, text=True, timeout=120,
                          stdin=subprocess.DEVNULL)


@pytest.fixture(scope="module")
def briefs(tmp_path_factory):
    base = tmp_path_factory.mktemp("ggr9brief")
    env = gw.base_env(base)
    bin_ = base / "bin"
    bin_.mkdir()
    for tool in ("git", "sh", "env"):
        (bin_ / tool).symlink_to(shutil.which(tool))
    env["PATH"] = str(bin_)
    root = base / "proj"
    root.mkdir()
    gw.git(root, "init", "-q", "-b", "main", env=env)
    (root / "seed.txt").write_text("seed\n")
    gw.git(root, "add", "-A", env=env)
    gw.git(root, "commit", "-q", "-m", "seed", env=env)
    r = _cli(root, env, "init")
    assert r.returncode == 0, r.stdout + r.stderr
    cfg = root / ".multiagents" / "config" / "project.yaml"
    original = yaml.safe_load(cfg.read_text()) or {}
    out = {}
    for value in (True, False):
        data = yaml.safe_load(yaml.safe_dump(original))
        data.setdefault("git", {})["coauthor_orchestrator"] = value
        cfg.write_text(yaml.safe_dump(data))
        for team in TEAMS:
            r = _cli(root, env, "prompt", "orchestrator", "--team", team)
            assert r.returncode == 0, r.stdout + r.stderr
            out[(team, value)] = r.stdout
    return out


def _flat(text: str) -> str:
    return " ".join(text.split())


def _paragraphs(text: str) -> list[str]:
    return [_flat(p) for p in re.split(r"\n\s*\n", text) if p.strip()]


NEGATION = r"\b(never|not|no|must not|do not|don't|doesn't)\b"


def assert_states_the_reporting_rule(text: str):
    flat = _flat(text).lower()
    assert "fingerprint" in flat, "the reporting rule does not mention fingerprints"
    paras = [p for p in _paragraphs(text) if "fingerprint" in p.lower()]
    rule = " ".join(paras).lower()
    assert "table" in rule, "no table in the reporting rule"
    for field in ("category", "commit", "location", "mask"):
        assert field in rule, f"the table's {field} column is not named"
    assert "chat" in rule or "user" in rule
    assert "fictional" in rule and "real" in rule, "does not ask which look fictional / real"
    # no self-allow: a sentence naming the allow lists with a prohibition, and the user decides
    sentences = re.split(r"(?<=[.;:])\s+|\n", _flat(text))
    assert any("allow_fingerprints" in s and "allow" in s and re.search(NEGATION, s, re.I)
               for s in sentences if "fingerprint" in s.lower()), \
        "no prohibition on adding to allow / allow_fingerprints"
    assert re.search(r"(user|owner)\b.{0,40}\b(decide|decides|choose|chooses|adds|add)", rule) or \
        re.search(r"\b(decide|decides|decision)\b.{0,40}\b(user)", rule), \
        "does not leave the decision to the user"


@pytest.mark.parametrize("coauthor", [True, False])
@pytest.mark.parametrize("team", TEAMS)
def test_gg_r9_the_orchestrator_brief_states_the_reporting_rule(briefs, team, coauthor):
    assert_states_the_reporting_rule(briefs[(team, coauthor)])


@pytest.mark.parametrize("team", TEAMS)
def test_gg_r9_the_reporting_rule_ties_to_a_guard_refusal(briefs, team):
    rule = [p for p in _paragraphs(briefs[(team, True)]) if "fingerprint" in p.lower()]
    assert any(re.search(r"guard|refus|push_branch|block", p, re.I) for p in rule)


GIT_MD = shipped_defaults_dir() / "agents" / "library" / "git.md"


def test_gg_r9_the_git_library_brief_states_the_reporting_rule():
    assert_states_the_reporting_rule(GIT_MD.read_text())
