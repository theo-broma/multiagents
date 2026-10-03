"""C13 — plans and the user's notes directory (contract `context/specs/c13-plans-and-notes.md`).

Ids covered here: PN-R1 (instructions, via the assembled prompt), PN-R1a (the
`multiagents plan commit` helper), PN-R2 / PN-R2a (plan format, discovery,
bounds, symlinks), PN-R3a and PN-R3 (instructions only, via the assembled
orchestrator prompt), PN-R4 / PN-R4a (`list_plans`, `multiagents doctor`),
PN-R5 / PN-R5a (notes processing, scaffolding), PN-R7 (existing surface still
works).

Everything is exercised through real processes: the MCP server over stdio, the
`multiagents` CLI, and `git`. Nothing in multiagents is patched.

Not expressible as tests (see the report): whether an agent obeys the write
rules or imports plans (PN-R3, PN-R6, PN-R6a — review and one live exercise).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import sv_harness as h  # noqa: E402

# ===========================================================================
# NAMES THE CONTRACT DOES NOT FIX — change them here and only here.
# The developer picks them; the test author's guesses below are NEED_INFO
# assumptions, not requirements. Everything else in this file is contract.
# ===========================================================================
TOOL_NAME = "list_plans"                  # fixed by PN-R4
PLANS_KEY = "plans"                       # list of one dict per plan file
NOTES_KEY = "notes"                       # the notes summary (dict)
P_PATH = "path"                           # repo-relative path of the plan
P_STATUS = "status"
P_TITLE = "title"                         # first heading of the plan
P_IMPORTED_IN = "imported_in"
P_SECTIONS = "sections"                   # dict {name: bool} OR list of present names;
                                          # names compared lower-cased, spaces -> "_"
P_MALFORMED = "malformed"                 # truthy for a malformed plan
P_REASON = "reason"                       # non-empty string for a malformed plan
N_TOTAL = "total"                         # notes counted (README excluded, refused excluded)
N_UNPROCESSED = "unprocessed"             # int count of unprocessed notes
PLAN_COMMIT_LOCK_WAIT_ENV = "MULTIAGENTS_PLAN_COMMIT_LOCK_WAIT"   # seconds; overrides the 30 s bound
# ===========================================================================

SRC = h.SRC
SECTIONS = ("Apply now", "Next phase", "Config changes", "Notes considered")
MIB = 1024 * 1024


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _norm(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


def plan_text(status: str | None = "ready", *, title: str | None = "A plan",
              sections=SECTIONS, notes: tuple[str, ...] = (), fm_extra: str = "",
              front: bool = True) -> str:
    fm = ""
    if front:
        fm = "---\n"
        if status is not None:
            fm += f"status: {status}\n"
        fm += fm_extra + "---\n"
    body = f"# {title}\n\n" if title else ""
    for sec in sections:
        body += f"## {sec}\n"
        if sec == "Notes considered":
            body += "".join(n + "\n" for n in notes) or "(none)\n"
        else:
            body += "- something\n"
        body += "\n"
    return fm + body


def sha(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def note_line(rel: str, data: bytes | str, what: str = "used it") -> str:
    return f"- {rel} sha256:{sha(data)} — {what}"


def write(path: Path, text: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, str):
        text = text.encode()
    path.write_bytes(text)
    return path


def git(root: Path, *args: str, env=None, check=True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                       env=env or _git_env())
    if check and r.returncode:
        raise AssertionError(f"git {args}: {r.stderr}")
    return r


def _git_env() -> dict:
    return {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def head(root: Path) -> str:
    return git(root, "rev-parse", "HEAD").stdout.strip()


def head_files(root: Path) -> list[str]:
    return sorted(git(root, "show", "--name-only", "--format=", "HEAD").stdout.split())


def _restricted_bin(base: Path) -> Path:
    b = base / "bin"
    if not b.exists():
        b.mkdir()
        for tool in ("git", "sh", "env"):
            (b / tool).symlink_to(shutil.which(tool))
    return b


def cli_env(base: Path) -> dict:
    """A CLI environment that reaches nothing outside `base`: own HOME, own
    state/config roots, and a PATH holding only git/sh/env so the provider
    probes in `doctor` and `init` fail fast instead of hitting the network."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MULTIAGENTS_", "CLAUDE_", "XDG_"))}
    home = base / "home"
    home.mkdir(exist_ok=True)
    env.update({
        "PYTHONPATH": SRC, "HOME": str(home), "PYTHONUNBUFFERED": "1",
        "MULTIAGENTS_STATE_DIR": str(base / "state"),
        "MULTIAGENTS_CONFIG_DIR": str(base / "config"),
        "PATH": str(_restricted_bin(base)),
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    })
    return env


def run_cli(base: Path, cwd: Path, *args: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "multiagents.cli", *args], cwd=str(cwd),
                          env=cli_env(base), capture_output=True, text=True, timeout=timeout)


def fresh_git_dir(base: Path, name: str = "proj") -> Path:
    root = base / name
    root.mkdir()
    for args in (["init", "-q", "-b", "main"],):
        git(root, *args)
    (root / "seed.txt").write_text("seed\n")
    git(root, "add", "seed.txt")
    git(root, "commit", "-q", "-m", "seed")
    return root


def init_project(base: Path, name: str = "proj", *args: str) -> Path:
    root = fresh_git_dir(base, name)
    r = run_cli(base, root, "init", *args)
    assert r.returncode == 0, r.stdout + r.stderr
    return root


@pytest.fixture
def proj(tmp_path, monkeypatch):
    """The stub-provider project of the survival harness: a git repo on `main`
    with a `.multiagents` config, `context/` empty."""
    monkeypatch.setenv("MULTIAGENTS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(tmp_path / "config"))
    p = h.Project(tmp_path)
    try:
        yield p
    finally:
        for s in list(p.servers):
            try:
                s.close()
            except Exception:
                pass


def _list_plans(p: h.Project) -> dict:
    s = p.server()
    res = s.call(TOOL_NAME)
    assert isinstance(res, dict), res
    assert "error" not in res, res
    return res


def plans_by_name(res: dict) -> dict[str, dict]:
    return {Path(e[P_PATH]).name: e for e in res[PLANS_KEY]}


def sections_of(entry: dict) -> set[str]:
    s = entry[P_SECTIONS]
    if isinstance(s, dict):
        return {_norm(k) for k, v in s.items() if v}
    return {_norm(x) for x in s}


def notes_summary(res: dict) -> dict:
    return res[NOTES_KEY]


# ===========================================================================
# PN-R4 / PN-R2a: list_plans — the read side
# ===========================================================================

def test_r4_list_plans_is_a_registered_read_only_tool(proj):
    s = proj.server()
    msg = s.request("tools/list", {})
    names = {t["name"] for t in msg["result"]["tools"]}
    assert TOOL_NAME in names


def test_r4_reports_each_status_title_imported_in_and_sections(proj):
    ctx = proj.root / "context" / "plans"
    write(ctx / "2026-10-01-draft.md", plan_text("draft", title="Draft plan"))
    write(ctx / "2026-10-02-ready.md", plan_text("ready", title="Ready plan",
                                                 sections=("Apply now", "Next phase")))
    write(ctx / "2026-10-03-applied.md", plan_text("applied", title="Applied plan",
                                                   fm_extra="applied_in: abc1234\n"))
    write(ctx / "2026-10-04-imported.md", plan_text("imported", title="Imported plan",
                                                    fm_extra="imported_in: def5678\n"))
    by = plans_by_name(_list_plans(proj))
    assert set(by) == {"2026-10-01-draft.md", "2026-10-02-ready.md",
                       "2026-10-03-applied.md", "2026-10-04-imported.md"}
    assert by["2026-10-01-draft.md"][P_STATUS] == "draft"
    assert by["2026-10-02-ready.md"][P_STATUS] == "ready"
    assert by["2026-10-03-applied.md"][P_STATUS] == "applied"
    assert by["2026-10-04-imported.md"][P_STATUS] == "imported"
    assert by["2026-10-02-ready.md"][P_TITLE] == "Ready plan"
    assert by["2026-10-04-imported.md"][P_TITLE] == "Imported plan"
    assert by["2026-10-04-imported.md"][P_IMPORTED_IN] == "def5678"
    assert not by["2026-10-02-ready.md"].get(P_IMPORTED_IN)
    assert not any(e.get(P_MALFORMED) for e in by.values())
    assert sections_of(by["2026-10-02-ready.md"]) == {"apply_now", "next_phase"}
    assert sections_of(by["2026-10-01-draft.md"]) == {_norm(x) for x in SECTIONS}
    assert by["2026-10-02-ready.md"][P_PATH] == "context/plans/2026-10-02-ready.md"


def test_r2a_title_is_the_first_heading_even_when_front_matter_has_none(proj):
    write(proj.root / "context/plans/p.md",
          "---\nstatus: ready\n---\nintro text\n\n# The first heading\n\n## Apply now\n- x\n\n"
          "# A later heading\n")
    assert plans_by_name(_list_plans(proj))["p.md"][P_TITLE] == "The first heading"


def test_r2a_a_missing_section_is_empty_not_an_error(proj):
    write(proj.root / "context/plans/p.md", plan_text("ready", sections=()))
    e = plans_by_name(_list_plans(proj))["p.md"]
    assert not e.get(P_MALFORMED)
    assert sections_of(e) == set()


def test_r2a_sections_match_exactly_level_2_only(proj):
    write(proj.root / "context/plans/p.md",
          "---\nstatus: ready\n---\n# T\n"
          "## apply now\n- wrong case\n"                 # wrong case
          "### Next phase\n- level three\n"              # wrong level
          "## Config changes and more\n- longer\n"       # not exact
          "## Notes considered\n(none)\n")               # the only exact one
    assert sections_of(plans_by_name(_list_plans(proj))["p.md"]) == {"notes_considered"}


@pytest.mark.parametrize("name, text, reason_re", [
    ("nofront.md", "# Just a heading\n\n## Apply now\n- x\n", r"front"),
    ("emptyfile.md", "", r"front|empty"),
    ("nostatus.md", "---\ntitle: x\n---\n# T\n", r"status"),
    ("bogus.md", "---\nstatus: bogus\n---\n# T\n", r"status|bogus"),
    ("listy.md", "---\nstatus: [ready]\n---\n# T\n", r"status"),
    ("unterminated.md", "---\nstatus: ready\n# T\n## Apply now\n", r"front"),
    ("badyaml.md", "---\nstatus: ready\n  : : [\n---\n# T\n", r"front|yaml|parse|header"),
    ("emptyfm.md", "---\n---\n# T\n", r"status|front"),
])
def test_r2a_malformed_plans_are_reported_with_a_reason_not_ignored(proj, name, text, reason_re):
    ctx = proj.root / "context/plans"
    write(ctx / name, text)
    write(ctx / "good.md", plan_text("ready"))
    by = plans_by_name(_list_plans(proj))
    assert name in by, "a malformed plan must be reported, not dropped"
    bad = by[name]
    assert bad[P_MALFORMED]
    assert isinstance(bad[P_REASON], str) and re.search(reason_re, bad[P_REASON], re.I), bad
    assert bad.get(P_STATUS) != "ready"
    # one bad file does not take the others down
    assert by["good.md"][P_STATUS] == "ready" and not by["good.md"].get(P_MALFORMED)


def test_r4_front_matter_is_parsed_as_data_never_executed(proj):
    marker = proj.root / "pwned.marker"
    write(proj.root / "context/plans/evil.md",
          "---\nstatus: ready\n"
          f"x: !!python/object/apply:os.system ['touch {marker}']\n---\n# T\n")
    res = _list_plans(proj)          # must not crash, must not execute
    assert not marker.exists()
    assert "evil.md" in plans_by_name(res)      # reported either way; not executing is the rule


def test_r4_discovery_is_direct_children_md_only_with_template_excluded(proj):
    ctx = proj.root / "context/plans"
    write(ctx / "TEMPLATE.md", plan_text("draft", title="Template"))
    write(ctx / "real.md", plan_text("ready"))
    write(ctx / "notes.txt", plan_text("ready"))
    write(ctx / "sub" / "nested.md", plan_text("ready"))
    write(ctx / "README.md", "no front matter here\n")          # only TEMPLATE.md is excluded
    by = plans_by_name(_list_plans(proj))
    assert set(by) == {"real.md", "README.md"}
    assert by["README.md"][P_MALFORMED]


def test_r4_absent_directories_are_not_an_error(proj):
    assert not (proj.root / "context").exists()
    res = _list_plans(proj)
    assert list(res[PLANS_KEY]) == []
    assert notes_summary(res)[N_UNPROCESSED] == 0


def test_r4_listing_never_modifies_anything(proj):
    write(proj.root / "context/plans/p.md", plan_text("ready"))
    write(proj.root / "context/notes/n.md", "hello\n")
    before = {p: p.read_bytes() for p in (proj.root / "context").rglob("*") if p.is_file()}
    mt = {p: p.stat().st_mtime_ns for p in before}
    _list_plans(proj)
    assert {p: p.read_bytes() for p in (proj.root / "context").rglob("*") if p.is_file()} == before
    assert {p: p.stat().st_mtime_ns for p in before} == mt
    assert git(proj.root, "log", "--oneline").stdout.count("\n") == 1


def test_r2_the_shipped_template_and_a_filled_example_both_parse(tmp_path):
    root = init_project(tmp_path)
    tpl = root / "context/plans/TEMPLATE.md"
    assert tpl.is_file()
    # copy the template under a real plan name: it must be a well-formed plan
    write(root / "context/plans/2026-10-03-from-template.md", tpl.read_text())
    s = h_server(tmp_path, root)
    try:
        res = s.call(TOOL_NAME)
    finally:
        s.close()
    by = plans_by_name(res)
    assert "TEMPLATE.md" not in by
    e = by["2026-10-03-from-template.md"]
    assert not e.get(P_MALFORMED), e
    assert e[P_STATUS] in ("draft", "ready", "applied", "imported")
    assert sections_of(e) == {_norm(x) for x in SECTIONS}


def h_server(base: Path, root: Path):
    """An MCP server over an arbitrary (init'd) project directory."""
    env = cli_env(base)
    env["MULTIAGENTS_PROJECT"] = str(root)
    env["MULTIAGENTS_SESSION_ID"] = h.ORCH_SESSION

    class _P:                                 # the bits of Project that Server uses
        pass
    pr = _P()
    pr.base, pr.root, pr.servers = base, root, []
    pr.env = lambda session="": env
    return h.Server(pr)


# ---------------------------------------------------------------------------
# bounds and symlinks (PN-R2a, PN-R4 "untrusted content")
# ---------------------------------------------------------------------------

def _padded_plan(total: int) -> bytes:
    base = plan_text("ready").encode()
    return base + b"x" * (total - len(base) - 1) + b"\n"


def test_r2a_a_plan_of_exactly_1_mib_is_read(proj):
    write(proj.root / "context/plans/exact.md", _padded_plan(MIB))
    assert (proj.root / "context/plans/exact.md").stat().st_size == MIB
    e = plans_by_name(_list_plans(proj))["exact.md"]
    assert not e.get(P_MALFORMED) and e[P_STATUS] == "ready"


def test_r2a_a_plan_one_byte_over_1_mib_is_reported_malformed_not_read(proj):
    write(proj.root / "context/plans/big.md", _padded_plan(MIB + 1))
    e = plans_by_name(_list_plans(proj))["big.md"]
    assert e[P_MALFORMED]
    assert re.search(r"size|large|big|bound|limit|MiB|bytes", e[P_REASON], re.I), e
    assert e.get(P_STATUS) != "ready"


def test_r2a_500_plans_are_all_listed(proj):
    ctx = proj.root / "context/plans"
    for i in range(500):
        write(ctx / f"p{i:03d}.md", plan_text("draft", sections=()))
    res = _list_plans(proj)
    assert len(res[PLANS_KEY]) == 500
    assert not any(e.get(P_MALFORMED) for e in res[PLANS_KEY])


def test_r2a_the_501st_plan_trips_the_bound_and_is_reported(proj):
    ctx = proj.root / "context/plans"
    for i in range(501):
        write(ctx / f"p{i:03d}.md", plan_text("draft", sections=()))
    res = _list_plans(proj)
    read_ok = [e for e in res[PLANS_KEY] if not e.get(P_MALFORMED)]
    assert len(read_ok) <= 500
    assert re.search(r"500|too many|limit|bound", json.dumps(res), re.I)


def test_r2a_a_symlinked_plan_file_is_refused_and_not_followed(proj):
    outside = write(proj.root / "elsewhere.md", plan_text("ready", title="SENTINEL-TARGET-TITLE"))
    ctx = proj.root / "context/plans"
    ctx.mkdir(parents=True)
    (ctx / "link.md").symlink_to(outside)
    write(ctx / "real.md", plan_text("draft"))
    res = _list_plans(proj)
    assert "SENTINEL-TARGET-TITLE" not in json.dumps(res)
    by = plans_by_name(res)
    assert by["real.md"][P_STATUS] == "draft"
    assert "link.md" in by and by["link.md"][P_MALFORMED]
    assert re.search(r"link", by["link.md"][P_REASON], re.I)


def test_r2a_a_symlinked_plans_directory_is_refused_and_reported(proj):
    real = proj.root / "elsewhere"
    write(real / "ready.md", plan_text("ready", title="SENTINEL-DIR-TITLE"))
    (proj.root / "context").mkdir()
    (proj.root / "context/plans").symlink_to(real)
    res = _list_plans(proj)
    assert "SENTINEL-DIR-TITLE" not in json.dumps(res)
    assert not [e for e in res[PLANS_KEY] if e.get(P_STATUS) == "ready"]
    assert re.search(r"link", json.dumps(res), re.I), "the refusal must be reported"


def test_r2a_a_symlinked_notes_directory_is_refused_and_reported(proj):
    real = proj.root / "elsewhere"
    write(real / "secret.md", "SENTINEL-NOTE-BODY\n")
    (proj.root / "context").mkdir()
    (proj.root / "context/notes").symlink_to(real)
    res = _list_plans(proj)
    assert notes_summary(res)[N_UNPROCESSED] == 0
    assert notes_summary(res).get(N_TOTAL, 0) == 0
    assert re.search(r"link", json.dumps(res), re.I)


def test_r2a_a_symlinked_note_file_is_refused_not_counted(proj):
    outside = write(proj.root / "elsewhere.md", "SENTINEL-NOTE\n")
    notes = proj.root / "context/notes"
    notes.mkdir(parents=True)
    (notes / "link.md").symlink_to(outside)
    write(notes / "real.md", "real note\n")
    res = _list_plans(proj)
    assert notes_summary(res)[N_UNPROCESSED] == 1       # only real.md
    assert notes_summary(res)[N_TOTAL] == 1
    assert "link.md" in json.dumps(res), "the refused symlink must be reported"


def test_r2a_a_note_over_1_mib_is_reported_not_read(proj):
    notes = proj.root / "context/notes"
    big = b"x" * (MIB + 1)
    write(notes / "big.md", big)
    # even a ready plan that records its hash cannot make an unread note processed
    write(proj.root / "context/plans/p.md",
          plan_text("ready", notes=(note_line("context/notes/big.md", big),)))
    res = _list_plans(proj)
    assert re.search(r"big\.md", json.dumps(res))
    assert re.search(r"size|large|bound|limit|MiB|bytes", json.dumps(res), re.I)


def test_r2a_more_than_500_notes_trips_the_bound_and_is_reported(proj):
    notes = proj.root / "context/notes"
    for i in range(501):
        write(notes / f"n{i:03d}.md", f"note {i}\n")
    res = _list_plans(proj)
    assert notes_summary(res)[N_UNPROCESSED] <= 500
    assert re.search(r"500|too many|limit|bound", json.dumps(res), re.I)


# ---------------------------------------------------------------------------
# notes: PN-R5
# ---------------------------------------------------------------------------

NOTE = "context/notes/idea.md"


@pytest.mark.parametrize("status, processed", [
    ("ready", True), ("applied", True), ("imported", True), ("draft", False),
])
def test_r5_which_plan_statuses_mark_a_note_processed(proj, status, processed):
    body = "Please add dark mode.\n"
    write(proj.root / NOTE, body)
    write(proj.root / "context/plans/p.md",
          plan_text(status, notes=(note_line(NOTE, body, "used for the next phase"),)))
    n = notes_summary(_list_plans(proj))
    assert n[N_TOTAL] == 1
    assert n[N_UNPROCESSED] == (0 if processed else 1)


def test_r5_a_note_no_plan_mentions_is_unprocessed(proj):
    write(proj.root / NOTE, "x\n")
    write(proj.root / "context/plans/p.md", plan_text("ready"))
    assert notes_summary(_list_plans(proj))[N_UNPROCESSED] == 1


@pytest.mark.parametrize("bad_status_text", [
    "---\nstatus: bogus\n---\n",         # unknown status → malformed
    "",                                  # no front matter → malformed
])
def test_r5_a_malformed_plan_does_not_mark_notes_processed(proj, bad_status_text):
    body = "n\n"
    write(proj.root / NOTE, body)
    write(proj.root / "context/plans/p.md",
          bad_status_text + "# T\n## Notes considered\n" + note_line(NOTE, body) + "\n")
    assert notes_summary(_list_plans(proj))[N_UNPROCESSED] == 1


def test_r5_editing_a_processed_note_makes_it_unprocessed_again(proj):
    note = proj.root / NOTE
    write(note, "version one\n")
    write(proj.root / "context/plans/p.md",
          plan_text("imported", notes=(note_line(NOTE, "version one\n"),)))
    s = proj.server()
    assert notes_summary(s.call(TOOL_NAME))[N_UNPROCESSED] == 0
    note.write_text("version one, edited\n")
    assert notes_summary(s.call(TOOL_NAME))[N_UNPROCESSED] == 1       # same server, no restart
    note.write_text("version one\n")                                   # reverted: hash matches again
    assert notes_summary(s.call(TOOL_NAME))[N_UNPROCESSED] == 0


def test_r5_a_one_byte_edit_is_enough(proj):
    note = proj.root / NOTE
    write(note, b"abc")
    write(proj.root / "context/plans/p.md", plan_text("ready", notes=(note_line(NOTE, b"abc"),)))
    note.write_bytes(b"abcd")
    assert notes_summary(_list_plans(proj))[N_UNPROCESSED] == 1


def test_r5_the_hash_is_over_the_raw_bytes(proj):
    raw = b"caf\xc3\xa9\r\nnot utf8 tail: \xff\xfe\x00\r\n"       # CRLF, invalid UTF-8, NUL
    write(proj.root / NOTE, raw)
    write(proj.root / "context/plans/p.md", plan_text("ready", notes=(note_line(NOTE, raw),)))
    assert notes_summary(_list_plans(proj))[N_UNPROCESSED] == 0


def test_r5_a_hash_of_the_text_normalised_is_not_the_raw_hash(proj):
    raw = b"line one\r\nline two\r\n"
    write(proj.root / NOTE, raw)
    normalised = b"line one\nline two\n"
    write(proj.root / "context/plans/p.md", plan_text("ready", notes=(note_line(NOTE, normalised),)))
    assert notes_summary(_list_plans(proj))[N_UNPROCESSED] == 1


def test_r5_any_plan_may_record_the_hash_and_each_note_is_judged_alone(proj):
    a, b, c = "one\n", "two\n", "three\n"
    for name, body in (("a", a), ("b", b), ("c", c)):
        write(proj.root / f"context/notes/{name}.md", body)
    write(proj.root / "context/plans/p1.md",
          plan_text("applied", notes=(note_line("context/notes/a.md", a),)))
    write(proj.root / "context/plans/p2.md",
          plan_text("draft", notes=(note_line("context/notes/b.md", b),)))       # draft: no
    n = notes_summary(_list_plans(proj))
    assert (n[N_TOTAL], n[N_UNPROCESSED]) == (3, 2)


def test_r5_readme_is_never_a_note_but_other_names_are(proj):
    notes = proj.root / "context/notes"
    write(notes / "README.md", "explains the directory\n")
    write(notes / "TEMPLATE.md", "this one is a note\n")       # only README.md is excluded
    write(notes / "todo.txt", "not markdown\n")                # not *.md
    write(notes / "sub" / "deep.md", "not a direct child\n")
    n = notes_summary(_list_plans(proj))
    assert (n[N_TOTAL], n[N_UNPROCESSED]) == (1, 1)


def test_r5_empty_note_is_a_note(proj):
    write(proj.root / "context/notes/empty.md", b"")
    n = notes_summary(_list_plans(proj))
    assert (n[N_TOTAL], n[N_UNPROCESSED]) == (1, 1)


# ===========================================================================
# PN-R4: multiagents doctor
# ===========================================================================

def _doctor_project(tmp_path, monkeypatch) -> h.Project:
    monkeypatch.setenv("MULTIAGENTS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(tmp_path / "config"))
    p = h.Project(tmp_path)
    f = p.root / ".multiagents/config/providers.yaml"
    cfg = yaml.safe_load(f.read_text())
    for name in ("codex", "opencode-zai", "opencode-deepinfra", "agy-partner", "opencode-go"):
        cfg["providers"][name] = {"enabled": False}
    f.write_text(yaml.safe_dump(cfg))
    return p


def _doctor(p: h.Project) -> list[str]:
    env = p.env(session="")
    env["PATH"] = str(_restricted_bin(p.base))
    r = subprocess.run([sys.executable, "-m", "multiagents.cli", "doctor"], cwd=str(p.root),
                       env=env, capture_output=True, text=True, timeout=300)
    return (r.stdout + r.stderr).splitlines()


def test_r4_doctor_prints_one_line_per_ready_unimported_plan_and_the_unprocessed_count(
        tmp_path, monkeypatch):
    p = _doctor_project(tmp_path, monkeypatch)
    plans = p.root / "context/plans"
    write(plans / "2026-10-01-draft.md", plan_text("draft"))
    write(plans / "2026-10-02-ready-one.md", plan_text("ready"))
    write(plans / "2026-10-03-ready-two.md", plan_text("ready"))
    write(plans / "2026-10-04-applied.md", plan_text("applied"))
    write(plans / "2026-10-05-imported.md", plan_text("imported"))
    write(plans / "TEMPLATE.md", plan_text("ready"))
    kept = "kept\n"
    write(p.root / "context/notes/kept.md", kept)
    write(p.root / "context/notes/a.md", "a\n")
    write(p.root / "context/notes/b.md", "b\n")
    write(p.root / "context/notes/c.md", "c\n")
    write(p.root / "context/notes/README.md", "readme\n")
    write(plans / "2026-10-06-notes.md",
          plan_text("imported", notes=(note_line("context/notes/kept.md", kept),)))
    lines = _doctor(p)
    text = "\n".join(lines)
    ready = [ln for ln in lines if "2026-10-02-ready-one" in ln]
    ready2 = [ln for ln in lines if "2026-10-03-ready-two" in ln]
    assert len(ready) == 1 and len(ready2) == 1
    for quiet in ("2026-10-01-draft", "2026-10-04-applied", "2026-10-05-imported", "TEMPLATE"):
        assert quiet not in text, f"{quiet} must not be listed"
    counts = [ln for ln in lines if re.search(r"notes?\b", ln, re.I) and re.search(r"\b3\b", ln)]
    assert counts, f"no line gives 3 unprocessed notes:\n{text}"


def test_r4_doctor_is_quiet_when_both_directories_are_absent(tmp_path, monkeypatch):
    p = _doctor_project(tmp_path, monkeypatch)
    assert not (p.root / "context").exists()
    lines = _doctor(p)
    mentions = [ln for ln in lines if re.search(r"\bplans?\b|\bnotes?\b|unprocessed", ln, re.I)]
    assert mentions == []
    # control: the same doctor does speak once there is something to say
    write(p.root / "context/plans/2026-10-02-now-present.md", plan_text("ready"))
    assert any("2026-10-02-now-present" in ln for ln in _doctor(p))


def test_r4_doctor_does_not_follow_a_symlinked_notes_directory(tmp_path, monkeypatch):
    p = _doctor_project(tmp_path, monkeypatch)
    outside = p.root / "elsewhere"
    write(outside / "sneaky-note.md", "x\n")
    write(p.root / "context/plans/2026-10-02-real-ready.md", plan_text("ready"))
    (p.root / "context/notes").symlink_to(outside)
    lines = _doctor(p)
    assert any("2026-10-02-real-ready" in ln for ln in lines)         # the plans side works
    assert not any(re.search(r"notes?\b", ln, re.I) and re.search(r"\b1\b", ln) for ln in lines)
    assert "sneaky" not in "\n".join(lines)


def test_r4_doctor_does_not_follow_a_symlinked_plans_directory(tmp_path, monkeypatch):
    p = _doctor_project(tmp_path, monkeypatch)
    outside = p.root / "elsewhere"
    write(outside / "2026-10-02-sneaky.md", plan_text("ready"))
    (p.root / "context").mkdir()
    (p.root / "context/plans").symlink_to(outside)
    write(p.root / "context/notes/a-real-note.md", "x\n")
    lines = _doctor(p)
    assert any(re.search(r"notes?\b", ln, re.I) and re.search(r"\b1\b", ln) for ln in lines)
    assert "sneaky" not in "\n".join(lines)


# ===========================================================================
# PN-R1a: multiagents plan commit
# ===========================================================================

def plan_commit(p: h.Project, *paths: str, env_extra=None, timeout=60.0):
    env = p.env(session="")
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "multiagents.cli", "plan", "commit", *paths],
                          cwd=str(p.root), env=env, capture_output=True, text=True,
                          timeout=timeout)


PLAN = "context/plans/2026-10-03-x.md"
CONTROL = "context/plans/2026-10-03-control.md"


def assert_helper_works(p: h.Project):
    """Positive control for every refusal test: in a state with no obstacle the
    helper commits a valid new plan. Without it a refusal test would also pass
    against a CLI that has no `plan commit` at all."""
    write(p.root / CONTROL, plan_text("draft", title="control"))
    before = head(p.root)
    r = plan_commit(p, CONTROL)
    assert r.returncode == 0, f"the helper must work when nothing is wrong:\n{r.stdout}{r.stderr}"
    assert head(p.root) != before and head_files(p.root) == [CONTROL]


def _new_plan(p, rel=PLAN, text=None):
    write(p.root / rel, text or plan_text("ready"))


def test_r1a_commits_exactly_the_named_new_files(proj):
    _new_plan(proj)
    write(proj.root / "context/specs/new-spec.md", "# a new spec\n")
    write(proj.root / "unrelated-untracked.txt", "u\n")
    before = head(proj.root)
    r = plan_commit(proj, PLAN, "context/specs/new-spec.md")
    assert r.returncode == 0, r.stdout + r.stderr
    assert head(proj.root) != before
    assert head_files(proj.root) == sorted([PLAN, "context/specs/new-spec.md"])
    assert "unrelated-untracked.txt" in git(proj.root, "status", "--porcelain").stdout


def test_r1a_a_staged_unrelated_change_stays_staged_and_out_of_the_commit(proj):
    write(proj.root / "other.txt", "orchestrator work\n")
    git(proj.root, "add", "other.txt")                       # the orchestrator's staged change
    (proj.root / ".gitignore").write_text(".multiagents/\n# edited, unstaged\n")
    _new_plan(proj)
    r = plan_commit(proj, PLAN)
    assert r.returncode == 0, r.stdout + r.stderr
    assert head_files(proj.root) == [PLAN]
    assert git(proj.root, "diff", "--cached", "--name-only").stdout.split() == ["other.txt"]
    assert ".gitignore" in git(proj.root, "diff", "--name-only").stdout


def test_r1a_updating_an_existing_plan_is_allowed(proj):
    _new_plan(proj, text=plan_text("draft"))
    assert plan_commit(proj, PLAN).returncode == 0
    _new_plan(proj, text=plan_text("ready"))
    before = head(proj.root)
    r = plan_commit(proj, PLAN)
    assert r.returncode == 0, r.stdout + r.stderr
    assert head(proj.root) != before and head_files(proj.root) == [PLAN]


@pytest.mark.parametrize("bad", [
    "BRIEF.md", "README.md", "src/code.py", "context/notes/idea.md", "context/other.md",
    "context/plans/../../escape.md", "context/plansfoo/x.md", ".multiagents/config/agents.yaml",
])
def test_r1a_refuses_paths_outside_context_plans_and_specs(proj, bad):
    write(proj.root / bad, "content\n")
    _new_plan(proj)
    before = head(proj.root)
    r = plan_commit(proj, bad)
    assert r.returncode != 0
    assert head(proj.root) == before
    assert (r.stderr + r.stdout).strip(), "a refusal says why"
    assert "usage:" not in r.stderr, "a refusal is the helper's, not an argument-parsing error"
    assert_helper_works(proj)


def test_r1a_an_absolute_path_outside_the_repository_is_refused(proj, tmp_path):
    outside = write(tmp_path / "outside.md", "x\n")
    before = head(proj.root)
    r = plan_commit(proj, str(outside))
    assert r.returncode != 0 and head(proj.root) == before
    assert_helper_works(proj)


def test_r1a_one_bad_path_refuses_the_whole_commit(proj):
    _new_plan(proj)
    write(proj.root / "BRIEF.md", "x\n")
    before = head(proj.root)
    r = plan_commit(proj, PLAN, "BRIEF.md")
    assert r.returncode != 0
    assert head(proj.root) == before
    assert PLAN in git(proj.root, "status", "--porcelain", "-uall").stdout   # still uncommitted
    assert plan_commit(proj, PLAN).returncode == 0                           # and fine alone
    assert head_files(proj.root) == [PLAN]


def test_r1a_a_symlink_under_plans_pointing_outside_is_refused(proj):
    write(proj.root / "secret.txt", "s\n")
    (proj.root / "context/plans").mkdir(parents=True)
    (proj.root / "context/plans/link.md").symlink_to(proj.root / "secret.txt")
    before = head(proj.root)
    r = plan_commit(proj, "context/plans/link.md")
    assert r.returncode != 0 and head(proj.root) == before
    assert_helper_works(proj)


def test_r1a_modifying_an_existing_spec_is_refused(proj):
    write(proj.root / "context/specs/old.md", "# v1\n")
    git(proj.root, "add", "context/specs/old.md")
    git(proj.root, "commit", "-q", "-m", "add spec")
    write(proj.root / "context/specs/old.md", "# v2 — amended\n")
    before = head(proj.root)
    r = plan_commit(proj, "context/specs/old.md")
    assert r.returncode != 0
    assert head(proj.root) == before
    assert (proj.root / "context/specs/old.md").read_text() == "# v2 — amended\n"   # left alone
    assert_helper_works(proj)
    # a NEW spec file is fine
    write(proj.root / "context/specs/brand-new.md", "# new\n")
    assert plan_commit(proj, "context/specs/brand-new.md").returncode == 0


def test_r1a_a_missing_path_or_no_path_is_an_error(proj):
    before = head(proj.root)
    assert plan_commit(proj, "context/plans/does-not-exist.md").returncode != 0
    assert plan_commit(proj).returncode != 0
    assert head(proj.root) == before
    assert_helper_works(proj)


def _merge_in_progress(p: h.Project, *, conflict: bool):
    root = p.root
    git(root, "checkout", "-q", "-b", "side")
    write(root / "f.txt", "side\n")
    git(root, "add", "f.txt")
    git(root, "commit", "-q", "-m", "side")
    git(root, "checkout", "-q", "main")
    if conflict:
        write(root / "f.txt", "main\n")
        git(root, "add", "f.txt")
        git(root, "commit", "-q", "-m", "main")
        assert git(root, "merge", "side", check=False).returncode != 0
    else:
        write(root / "g.txt", "main\n")
        git(root, "add", "g.txt")
        git(root, "commit", "-q", "-m", "main")
        git(root, "merge", "--no-commit", "--no-ff", "side")
    assert (root / ".git/MERGE_HEAD").exists()


@pytest.mark.parametrize("conflict", [False, True], ids=["clean-merge-uncommitted", "conflicted-merge"])
def test_r1a_refuses_while_a_merge_is_in_progress(proj, conflict):
    _merge_in_progress(proj, conflict=conflict)
    _new_plan(proj)
    before = head(proj.root)
    r = plan_commit(proj, PLAN)
    assert r.returncode != 0
    assert head(proj.root) == before
    assert (proj.root / ".git/MERGE_HEAD").exists(), "the merge must be left as it was"
    assert (r.stderr + r.stdout).strip()
    assert "usage:" not in r.stderr
    git(proj.root, "merge", "--abort")
    assert_helper_works(proj)


def test_r1a_refuses_while_a_rebase_is_in_progress(proj):
    root = proj.root
    git(root, "checkout", "-q", "-b", "side")
    write(root / "f.txt", "side\n")
    git(root, "add", "f.txt")
    git(root, "commit", "-q", "-m", "side")
    git(root, "checkout", "-q", "main")
    write(root / "f.txt", "main\n")
    git(root, "add", "f.txt")
    git(root, "commit", "-q", "-m", "main")
    git(root, "checkout", "-q", "side")
    assert git(root, "rebase", "main", check=False).returncode != 0
    assert (root / ".git/rebase-merge").exists() or (root / ".git/rebase-apply").exists()
    _new_plan(proj)
    before = head(proj.root)
    r = plan_commit(proj, PLAN)
    assert r.returncode != 0 and head(proj.root) == before
    assert (root / ".git/rebase-merge").exists() or (root / ".git/rebase-apply").exists()
    assert "usage:" not in r.stderr
    git(root, "rebase", "--abort")
    assert_helper_works(proj)


def test_r1a_waits_out_a_held_index_lock_released_after_two_seconds(proj):
    _new_plan(proj)
    lock = proj.root / ".git/index.lock"
    lock.write_text("")
    before = head(proj.root)
    env = proj.env(session="")
    t0 = time.monotonic()
    proc = subprocess.Popen([sys.executable, "-m", "multiagents.cli", "plan", "commit", PLAN],
                            cwd=str(proj.root), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(1.0)
        assert proc.poll() is None, "must keep retrying while the lock is held"
        time.sleep(1.0)
        lock.unlink()
        out, err = proc.communicate(timeout=40)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, out + err
    assert time.monotonic() - t0 >= 1.9
    assert head(proj.root) != before and head_files(proj.root) == [PLAN]


def test_r1a_a_lock_that_is_never_released_fails_after_the_bound_and_is_not_removed(proj):
    _new_plan(proj)
    lock = proj.root / ".git/index.lock"
    lock.write_text("held by someone else\n")
    before = head(proj.root)
    t0 = time.monotonic()
    try:
        r = plan_commit(proj, PLAN, env_extra={PLAN_COMMIT_LOCK_WAIT_ENV: "3"}, timeout=25)
    except subprocess.TimeoutExpired:
        pytest.fail(f"the retry bound was not overridable through {PLAN_COMMIT_LOCK_WAIT_ENV}"
                    " (or the helper never gives up)")
    elapsed = time.monotonic() - t0
    assert r.returncode != 0
    assert elapsed >= 2.0, "it must retry up to the bound, not fail on first contact"
    assert lock.exists() and lock.read_text() == "held by someone else\n", "never deletes a lock"
    assert head(proj.root) == before
    assert (r.stderr + r.stdout).strip()
    assert "usage:" not in r.stderr
    lock.unlink()                                   # released at last: the helper works again
    assert plan_commit(proj, PLAN).returncode == 0
    assert head_files(proj.root) == [PLAN]


def test_r1a_a_failure_that_is_not_lock_contention_is_not_retried(proj):
    hook = proj.root / ".git/hooks/pre-commit"
    counter = proj.root / "hook-runs.txt"
    hook.write_text(f"#!/bin/sh\necho run >> {counter}\nexit 1\n")
    hook.chmod(0o755)
    _new_plan(proj)
    before = head(proj.root)
    t0 = time.monotonic()
    r = plan_commit(proj, PLAN, timeout=40)
    assert r.returncode != 0
    assert time.monotonic() - t0 < 10
    runs = counter.read_text().count("run") if counter.exists() else 0
    assert runs == 1, f"the pre-commit hook ran {runs} times; a non-lock failure is tried once"
    assert head(proj.root) == before


def test_r1a_never_stashes_or_resets_the_orchestrators_work(proj):
    write(proj.root / "seed.txt", "orchestrator edit, unstaged\n")
    write(proj.root / "staged.txt", "staged\n")
    git(proj.root, "add", "staged.txt")
    _new_plan(proj)
    r = plan_commit(proj, PLAN)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (proj.root / "seed.txt").read_text() == "orchestrator edit, unstaged\n"
    assert git(proj.root, "stash", "list").stdout == ""
    assert git(proj.root, "diff", "--cached", "--name-only").stdout.split() == ["staged.txt"]


# ===========================================================================
# PN-R5a: multiagents init scaffolding
# ===========================================================================

def test_r5a_init_creates_the_template_and_the_notes_readme(tmp_path):
    root = init_project(tmp_path)
    tpl = root / "context/plans/TEMPLATE.md"
    readme = root / "context/notes/README.md"
    assert tpl.is_file() and tpl.stat().st_size > 0
    assert readme.is_file() and readme.stat().st_size > 0
    text = tpl.read_text()
    for sec in SECTIONS:
        assert f"## {sec}" in text
    assert text.startswith("---\n") and "status:" in text


def test_r5a_init_adds_them_when_context_already_exists(tmp_path):
    root = fresh_git_dir(tmp_path)
    write(root / "context/README.md", "mine\n")
    write(root / "context/specs/s.md", "spec\n")
    r = run_cli(tmp_path, root, "init")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (root / "context/plans/TEMPLATE.md").is_file()
    assert (root / "context/notes/README.md").is_file()
    assert (root / "context/README.md").read_text() == "mine\n"
    assert (root / "context/specs/s.md").read_text() == "spec\n"


def test_r5a_init_adds_only_what_is_missing(tmp_path):
    root = fresh_git_dir(tmp_path)
    write(root / "context/plans/TEMPLATE.md", "my own template\n")
    assert run_cli(tmp_path, root, "init").returncode == 0
    assert (root / "context/plans/TEMPLATE.md").read_text() == "my own template\n"
    assert (root / "context/notes/README.md").is_file()


@pytest.mark.parametrize("flags", [(), ("--force",)], ids=["plain", "force"])
@pytest.mark.parametrize("custom, missing", [
    ("context/plans/TEMPLATE.md", "context/notes/README.md"),
    ("context/notes/README.md", "context/plans/TEMPLATE.md"),
], ids=["custom-template", "custom-readme"])
def test_r5a_init_never_overwrites_them_even_with_force(tmp_path, flags, custom, missing):
    root = fresh_git_dir(tmp_path)
    write(root / custom, "customised\n")
    write(root / "context/notes/my-note.md", "a note of mine\n")
    write(root / "context/plans/2026-10-03-mine.md", "a plan of mine\n")
    r = run_cli(tmp_path, root, "init", *flags)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (root / missing).is_file(), "the missing one is still scaffolded"
    assert (root / custom).read_text() == "customised\n"
    assert (root / "context/notes/my-note.md").read_text() == "a note of mine\n"
    assert (root / "context/plans/2026-10-03-mine.md").read_text() == "a plan of mine\n"


@pytest.mark.parametrize("flags", [(), ("--force",)], ids=["plain", "force"])
def test_r5a_a_second_init_leaves_the_scaffolding_byte_identical(tmp_path, flags):
    root = init_project(tmp_path)
    files = ("context/plans/TEMPLATE.md", "context/notes/README.md")
    first = {f: (root / f).read_bytes() for f in files}
    assert all(first.values())
    assert run_cli(tmp_path, root, "init", *flags).returncode == 0
    assert {f: (root / f).read_bytes() for f in files} == first


def test_r5a_the_scaffolded_readme_and_template_are_not_counted_as_notes_or_plans(tmp_path):
    root = init_project(tmp_path)
    s = h_server(tmp_path, root)
    try:
        res = s.call(TOOL_NAME)
    finally:
        s.close()
    assert list(res[PLANS_KEY]) == []
    assert (notes_summary(res)[N_TOTAL], notes_summary(res)[N_UNPROCESSED]) == (0, 0)


def test_r7_init_still_creates_the_context_readme_on_a_new_project(tmp_path):
    root = init_project(tmp_path)
    assert (root / "context/README.md").is_file()
    assert (root / ".multiagents/config/agents.yaml").is_file()


# ===========================================================================
# PN-R1 / PN-R3 / PN-R5: the ASSEMBLED launch prompts
# ===========================================================================

NEG = re.compile(r"\b(never|not|don't|do not|must not|cannot|no)\b", re.I)


def near(text: str, needle: str, *others: str, radius: int = 500, neg: bool = False) -> bool:
    """Is there an occurrence of `needle` with every `others` (case-insensitive
    substrings) — and a negation word if `neg` — within `radius` characters?"""
    low = text.lower()
    start = 0
    while True:
        i = low.find(needle.lower(), start)
        if i < 0:
            return False
        window = low[max(0, i - radius): i + len(needle) + radius]
        if all(o.lower() in window for o in others) and (not neg or NEG.search(window)):
            return True
        start = i + 1


@pytest.fixture(scope="module")
def prompts(tmp_path_factory):
    base = tmp_path_factory.mktemp("c13prompts")
    root = init_project(base)
    out = {}
    for key, args in (("initializer", ("initializer",)),
                      ("orch-implement", ("orchestrator", "--team", "implement")),
                      ("orch-review", ("orchestrator", "--team", "review"))):
        r = run_cli(base, root, "prompt", *args)
        assert r.returncode == 0, r.stdout + r.stderr
        out[key] = r.stdout
    return out


def test_r1_initializer_prompt_names_plans_and_the_commit_helper(prompts):
    t = prompts["initializer"]
    assert "context/plans/" in t
    assert "multiagents plan commit" in t
    assert "YYYY-MM-DD" in t or re.search(r"\d{4}-\d{2}-\d{2}", t[t.find("context/plans/"):][:600])


def test_r1_initializer_prompt_forbids_editing_brief_outside_the_bootstrap(prompts):
    t = prompts["initializer"]
    assert near(t, "BRIEF.md", "bootstrap", neg=True), \
        "no sentence forbids BRIEF.md edits and names the bootstrap exception"


def test_r1_initializer_prompt_forbids_modifying_an_existing_spec(prompts):
    t = prompts["initializer"]
    assert near(t, "context/specs/", "existing", neg=True)


def test_r5_initializer_prompt_reads_notes_never_touches_them_and_records_hashes(prompts):
    t = prompts["initializer"]
    assert "context/notes/" in t
    assert near(t, "context/notes/", "edit", neg=True) or near(t, "context/notes/", "delete", neg=True)
    assert "Notes considered" in t
    assert re.search(r"sha-?256", t, re.I)
    assert near(t, "context/notes/", "start", "session") or near(t, "context/notes/", "every", "read")


@pytest.mark.parametrize("key", ["orch-implement", "orch-review"])
def test_r3_orchestrator_prompt_covers_plans_in_every_team(prompts, key):
    t = prompts[key]
    assert "context/plans/" in t
    assert near(t, "context/plans/", "ready")
    for word in ("Apply now", "Next phase", "applied", "imported", "applied_in", "imported_in"):
        assert word in t, word
    assert near(t, "draft", "context/plans/", neg=True) or near(t, "draft", "ready", neg=True)


@pytest.mark.parametrize("key", ["orch-implement", "orch-review"])
def test_r3_orchestrator_prompt_never_acts_on_notes_directly(prompts, key):
    t = prompts[key]
    assert "context/notes/" in t
    assert near(t, "context/notes/", "plan", neg=True)


@pytest.mark.parametrize("key", ["orch-implement", "orch-review"])
def test_r3a_orchestrator_prompt_marks_in_a_following_commit(prompts, key):
    t = prompts[key]
    assert near(t, "applied_in", "commit", "following") or near(t, "applied_in", "commit", "then")
    assert near(t, "imported_in", "commit", "following") or near(t, "imported_in", "commit", "then")


def test_r7_prompts_still_assemble_for_the_existing_roles(prompts):
    assert prompts["initializer"].lstrip().startswith("#")
    assert len(prompts["orch-implement"]) > 1000
