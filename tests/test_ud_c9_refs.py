"""UD-C9 / D-UD2.3: refs bind a "yes" to exact bytes. Every ref rule, the
approved digests, `check_refs`, snapshots (content, survival, modes)."""
import os
import stat

import pytest

from support.ud2_support import (CLI, ROOT_ORCH, ask, call, make, n_events, refused, sha,
                                 snapshot_of, succeeded, world)  # noqa: F401
from support.ud_support import D, assert_valid, code_of, decision_events, events

MIB = 1024 * 1024


def _create(world, refs, **over):
    return make(world, refs=refs, **over)


def _refused(world, refs, **over):
    # Control: a plain, valid path ref is accepted, so that what follows is
    # refused for its own fault and not because path refs are unsupported.
    world.write("control.md", "control")
    assert _create(world, [{"path": "control.md"}])["refs"][0]["sha256"] == sha("control")
    before, n = snapshot_of(world.tree), n_events(world.tree)
    with pytest.raises(D.DecisionError) as err:
        _create(world, refs, **over)
    assert code_of(err.value) == "invalid_record"
    assert str(getattr(err.value, "field", "")).startswith("refs"), err.value.__dict__
    assert snapshot_of(world.tree) == before and n_events(world.tree) == n
    return err.value


# ------------------------------------------------------------- path refs --

def test_c9_a_path_ref_records_the_digest_and_stores_a_snapshot(world):
    world.write("docs/draft.md", "draft v1\n")
    rec = _create(world, [{"path": "docs/draft.md"}])
    assert rec["refs"] == [{"path": "docs/draft.md", "sha256": sha("draft v1\n")}]
    assert_valid(rec)
    assert D.get(world.tree, rec["id"])["refs"] == rec["refs"]
    assert (world.snapshots / sha("draft v1\n")).read_bytes() == b"draft v1\n"


def test_c9_a_correct_caller_given_digest_is_accepted(world):
    world.write("draft.md", "x")
    rec = _create(world, [{"path": "draft.md", "sha256": sha("x")}])
    assert rec["refs"] == [{"path": "draft.md", "sha256": sha("x")}]
    assert (world.snapshots / sha("x")).read_bytes() == b"x"


@pytest.mark.parametrize("digest", ["0" * 64, sha("other"), "abc", "", "Z" * 64])
def test_c9_a_digest_that_differs_from_the_file_is_invalid_record(world, digest):
    world.write("draft.md", "x")
    _refused(world, [{"path": "draft.md", "sha256": digest}])


def test_c9_an_empty_file_is_a_valid_ref(world):
    world.write("empty.md", b"")
    rec = _create(world, [{"path": "empty.md"}])
    assert rec["refs"][0]["sha256"] == sha(b"")


def test_c9_binary_bytes_are_hashed_and_kept_exactly(world):
    blob = bytes(range(256)) * 3
    world.write("blob.bin", blob)
    rec = _create(world, [{"path": "blob.bin"}])
    assert rec["refs"][0]["sha256"] == sha(blob)
    assert D.snapshot(world.tree, rec["id"], "blob.bin") == blob


def test_c9_size_boundary_is_one_mib(world):
    world.write("exact.bin", b"a" * MIB)
    rec = _create(world, [{"path": "exact.bin"}])
    assert rec["refs"][0]["sha256"] == sha(b"a" * MIB)
    world.write("over.bin", b"a" * (MIB + 1))
    _refused(world, [{"path": "over.bin"}])


def test_c9_path_must_be_relative(world, tmp_path):
    outside = tmp_path / "abs.md"
    outside.write_text("x")
    _refused(world, [{"path": str(outside)}])
    world.write("draft.md", "x")
    _refused(world, [{"path": str(world.root / "draft.md")}])      # absolute, even inside


@pytest.mark.parametrize("path", ["../outside.md", "docs/../../outside.md", "docs/../draft.md",
                                  "..", "./../x", "docs/..", "a/b/../../draft.md"])
def test_c9_a_dotdot_component_is_refused_even_if_it_resolves_inside(world, tmp_path, path):
    world.write("draft.md", "x")
    world.write("docs/keep.md", "y")
    (tmp_path / "outside.md").write_text("o")
    _refused(world, [{"path": path}])


@pytest.mark.parametrize("path", ["", " ", "nope.md", "docs", "docs/", "a\x00b.md", "."])
def test_c9_a_path_that_is_not_a_file_is_refused(world, path):
    world.write("docs/keep.md", "y")
    _refused(world, [{"path": path}])


def test_c9_a_symlink_escaping_the_root_is_refused(world, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    (world.root / "link.md").symlink_to(secret)
    _refused(world, [{"path": "link.md"}])
    assert not world.snapshots.exists() or all(
        (p.read_bytes() != b"TOP SECRET") for p in world.snapshots.iterdir())


def test_c9_a_symlinked_directory_escaping_the_root_is_refused(world, tmp_path):
    outside = tmp_path / "outdir"
    outside.mkdir()
    (outside / "f.md").write_text("o")
    (world.root / "dl").symlink_to(outside, target_is_directory=True)
    _refused(world, [{"path": "dl/f.md"}])


def test_c9_a_chain_of_symlinks_ending_outside_is_refused(world, tmp_path):
    (tmp_path / "t.txt").write_text("t")
    (world.root / "b").symlink_to(tmp_path / "t.txt")
    (world.root / "a").symlink_to(world.root / "b")
    _refused(world, [{"path": "a"}])


def test_c9_a_symlink_loop_is_refused_not_a_crash(world):
    (world.root / "a").symlink_to("b")
    (world.root / "b").symlink_to("a")
    _refused(world, [{"path": "a"}])


def test_c9_a_symlink_that_stays_inside_the_root_is_allowed(world):
    world.write("real/draft.md", "inside\n")
    (world.root / "alias.md").symlink_to(world.root / "real" / "draft.md")
    rec = _create(world, [{"path": "alias.md"}])
    assert rec["refs"][0]["sha256"] == sha("inside\n")


def test_c9_a_directory_is_not_a_regular_file(world):
    (world.root / "d").mkdir()
    _refused(world, [{"path": "d"}])


@pytest.mark.parametrize("ref", [{}, {"path": "a.md", "commit": "abc"}, {"path": 5},
                                 {"commit": 5}, {"sha256": "0" * 64}, {"path": "a.md", "x": 1},
                                 "a.md", None, ["a.md"]])
def test_c9_malformed_refs_are_invalid_record(world, ref):
    world.write("a.md", "x")
    _refused(world, [ref])


def test_c9_ref_count_boundary_is_eight(world):
    for i in range(9):
        world.write(f"f{i}.md", f"v{i}")
    refs = [{"path": f"f{i}.md"} for i in range(8)]
    rec = _create(world, refs)
    assert len(rec["refs"]) == 8
    _refused(world, [{"path": f"f{i}.md"} for i in range(9)])


def test_c9_one_bad_ref_among_good_ones_stores_nothing(world):
    world.write("good.md", "g")
    _refused(world, [{"path": "good.md"}, {"path": "missing.md"}])


def test_c9_a_scrubbable_ref_is_still_refused(world):
    _refused(world, [{"path": "src/sk-" + "a" * 20}])


def test_c9_paths_are_relative_to_the_project_not_the_working_directory(world):
    world.write("draft.md", "root copy")
    cwd = os.getcwd()
    assert os.path.realpath(cwd) != os.path.realpath(world.root)
    open(os.path.join(cwd, "draft.md"), "w").write("cwd copy")
    rec = _create(world, [{"path": "draft.md"}])
    assert rec["refs"][0]["sha256"] == sha("root copy")


# ----------------------------------------------------------- commit refs --

def test_c9_a_commit_ref_is_recorded_as_a_full_sha(world):
    head = world.init_repo()
    for spelling in (head[:7], head[:12], head, "HEAD", "main"):
        rec = _create(world, [{"commit": spelling}])
        assert rec["refs"] == [{"commit": head}], spelling
        assert_valid(rec)


@pytest.mark.parametrize("commit", ["", "deadbeefdeadbeef", "zzzz", "--all", "-h", "HEAD\n--all",
                                    "HEAD --", "no-such-branch", "main..main", "HEAD^{tree}x"])
def test_c9_a_commit_that_does_not_resolve_is_refused(world, commit):
    world.init_repo()
    _refused(world, [{"commit": commit}])


def test_c9_a_commit_ref_in_a_project_that_is_not_a_repository_is_refused(world):
    _refused(world, [{"commit": "HEAD"}])


def test_c9_a_commit_option_is_never_run_as_a_git_option(world):
    world.init_repo()
    marker = world.root / "pwned"
    _refused(world, [{"commit": f"--output={marker}"}])
    assert not marker.exists()


def test_c9_path_and_commit_refs_mix(world):
    head = world.init_repo()
    world.write("draft.md", "x")
    rec = _create(world, [{"path": "draft.md"}, {"commit": head[:8]}])
    assert rec["refs"] == [{"path": "draft.md", "sha256": sha("x")}, {"commit": head}]


# ------------------------------------------------------ the ask_user tool --

def test_c9_ask_user_returns_the_recorded_refs(world):
    world.write("draft.md", "x")
    head = world.init_repo()
    out = succeeded(ask(refs=[{"path": "draft.md"}, {"commit": head[:9]}]))
    assert out["refs"] == [{"path": "draft.md", "sha256": sha("x")}, {"commit": head}]
    assert D.get(world.tree, out["id"])["refs"] == out["refs"]


@pytest.mark.parametrize("refs", [[{"path": "../x"}], [{"path": "/etc/passwd"}],
                                  [{"path": "draft.md", "sha256": "0" * 64}],
                                  [{"path": "gone.md"}], [{"commit": "nope"}]])
def test_c9_ask_user_refuses_a_bad_ref_and_stores_nothing(world, refs):
    world.write("draft.md", "x")
    world.init_repo()
    out = ask(refs=refs)
    assert out.get("error") == "invalid_record" and str(out.get("field", "")).startswith("refs"), out
    assert D.list(world.tree)["decisions"] == [] and events(world.tree) == []


def test_c9_ask_user_refuses_a_symlink_escape(world, tmp_path):
    (tmp_path / "s.txt").write_text("secret")
    (world.root / "l.md").symlink_to(tmp_path / "s.txt")
    out = ask(refs=[{"path": "l.md"}])
    assert out.get("error") == "invalid_record", out
    assert D.list(world.tree)["decisions"] == []


def test_c9_ask_user_refuses_nine_refs(world):
    for i in range(9):
        world.write(f"f{i}.md", str(i))
    assert ask(refs=[{"path": f"f{i}.md"} for i in range(9)]).get("error") == "invalid_record"
    succeeded(ask(refs=[{"path": f"f{i}.md"} for i in range(8)]))


# ---------------------------------------------------------- approved refs --

def test_c9_the_answer_records_the_approved_digests(world):
    world.write("a.md", "A")
    world.write("b.md", "B")
    rec = _create(world, [{"path": "a.md"}, {"path": "b.md"}])
    new = D.answer(world.tree, rec["id"], revision=1, by=CLI, options=["yes"])["decision"]
    assert_valid(new)
    approved = new["answer"]["approved_refs"]
    for ref in rec["refs"]:
        assert ref in approved
    assert len(approved) == 2


def test_c9_approved_refs_are_the_digests_recorded_at_the_ask_not_the_current_bytes(world):
    p = world.write("draft.md", "v1")
    rec = _create(world, [{"path": "draft.md"}])
    p.write_text("v2 edited after the ask")
    new = D.answer(world.tree, rec["id"], revision=1, by=CLI, options=["yes"])["decision"]
    assert new["answer"]["approved_refs"] == [{"path": "draft.md", "sha256": sha("v1")}]
    assert D.check_refs(world.tree, rec["id"])["changed"] == ["draft.md"]


def test_c9_approved_refs_through_the_mcp_answer(world):
    world.write("draft.md", "v1")
    rec = _create(world, [{"path": "draft.md"}], audience="orchestrator", by=ROOT_ORCH)
    succeeded(call("answer_decision", id=rec["id"], revision=1, options=["yes"]))
    got = D.get(world.tree, rec["id"])["answer"]["approved_refs"]
    assert {"path": "draft.md", "sha256": sha("v1")} in got


def test_c9_a_retry_of_an_answer_keeps_the_original_approved_refs(world):
    p = world.write("draft.md", "v1")
    rec = _create(world, [{"path": "draft.md"}])
    D.answer(world.tree, rec["id"], revision=1, by=CLI, options=["yes"])
    p.write_text("v2")
    out = D.answer(world.tree, rec["id"], revision=1, by=CLI, options=["yes"])
    assert out["already_answered"] is True
    assert out["decision"]["answer"]["approved_refs"] == [{"path": "draft.md", "sha256": sha("v1")}]


# ------------------------------------------------------------ check_refs --

def test_c9_check_refs_unchanged(world):
    world.write("a.md", "A")
    rec = _create(world, [{"path": "a.md"}])
    assert D.check_refs(world.tree, rec["id"]) == {"ok": True, "changed": [], "missing": []}


def test_c9_check_refs_after_editing_a_draft(world):
    p = world.write("a.md", "A")
    world.write("b.md", "B")
    rec = _create(world, [{"path": "a.md"}, {"path": "b.md"}])
    p.write_text("A edited")
    assert D.check_refs(world.tree, rec["id"]) == {"ok": False, "changed": ["a.md"], "missing": []}


def test_c9_check_refs_after_deleting_a_draft(world):
    p = world.write("a.md", "A")
    world.write("b.md", "B")
    rec = _create(world, [{"path": "a.md"}, {"path": "b.md"}])
    p.unlink()
    assert D.check_refs(world.tree, rec["id"]) == {"ok": False, "changed": [], "missing": ["a.md"]}


def test_c9_check_refs_edit_and_delete_together(world):
    a = world.write("a.md", "A")
    b = world.write("b.md", "B")
    rec = _create(world, [{"path": "a.md"}, {"path": "b.md"}])
    a.write_text("A2")
    b.unlink()
    assert D.check_refs(world.tree, rec["id"]) == {"ok": False, "changed": ["a.md"], "missing": ["b.md"]}


def test_c9_check_refs_is_ok_again_when_the_original_bytes_return(world):
    p = world.write("a.md", "A")
    rec = _create(world, [{"path": "a.md"}])
    p.write_text("A2")
    assert D.check_refs(world.tree, rec["id"])["ok"] is False
    p.write_text("A")
    assert D.check_refs(world.tree, rec["id"])["ok"] is True


def test_c9_check_refs_after_the_file_is_replaced_by_a_symlink_to_the_same_bytes(world):
    p = world.write("a.md", "A")
    world.write("elsewhere/a.md", "A")
    rec = _create(world, [{"path": "a.md"}])
    p.unlink()
    p.symlink_to(world.root / "elsewhere" / "a.md")
    assert D.check_refs(world.tree, rec["id"])["ok"] is True


def test_c9_check_refs_a_commit_ref_is_always_ok(world):
    head = world.init_repo()
    rec = _create(world, [{"commit": head}])
    world.write("more.md", "new commit")
    world.git("add", "-A")
    world.git("commit", "-q", "-m", "second")
    assert D.check_refs(world.tree, rec["id"]) == {"ok": True, "changed": [], "missing": []}


def test_c9_check_refs_with_no_refs_is_ok(world):
    assert D.check_refs(world.tree, make(world)["id"]) == {"ok": True, "changed": [], "missing": []}


def test_c9_check_refs_does_not_touch_the_record_or_the_events(world):
    p = world.write("a.md", "A")
    rec = _create(world, [{"path": "a.md"}])
    p.write_text("B")
    before, n = snapshot_of(world.tree), n_events(world.tree)
    D.check_refs(world.tree, rec["id"])
    assert snapshot_of(world.tree) == before and n_events(world.tree) == n


@pytest.mark.parametrize("status", ["answered", "handled", "withdrawn", "superseded"])
def test_c9_check_refs_works_in_every_status(world, status):
    from support.ud_support import in_status
    p = world.write("a.md", "A")
    rec = _create(world, [{"path": "a.md"}])
    if status == "answered":
        D.answer(world.tree, rec["id"], revision=1, by=CLI, options=["yes"])
    elif status == "handled":
        D.answer(world.tree, rec["id"], revision=1, by=CLI, options=["yes"])
        D.mark_handled(world.tree, rec["id"], revision=2, by=ROOT_ORCH, outcome="applied")
    elif status == "withdrawn":
        D.withdraw(world.tree, rec["id"], revision=1, by=ROOT_ORCH, reason="r")
    else:
        other = make(world)
        D.supersede(world.tree, rec["id"], revision=1, by=ROOT_ORCH, superseded_by=other["id"])
    p.write_text("B")
    assert D.check_refs(world.tree, rec["id"])["changed"] == ["a.md"]


def test_c9_check_refs_unknown_id(world):
    with pytest.raises(D.DecisionError) as err:
        D.check_refs(world.tree, "d-00000000")
    assert code_of(err.value) == "unknown"


def test_c9_check_refs_on_a_legacy_question_is_ok(world):
    q = world.tree.add_question("ag-3", "t", "q?")
    assert D.check_refs(world.tree, q["id"]) == {"ok": True, "changed": [], "missing": []}


# ------------------------------------------------------------- snapshots --

def test_c9_a_snapshot_survives_the_files_deletion(world):
    p = world.write("a.md", "original bytes")
    rec = _create(world, [{"path": "a.md"}])
    p.unlink()
    assert D.snapshot(world.tree, rec["id"], "a.md") == b"original bytes"


def test_c9_a_snapshot_is_the_ask_time_bytes_not_the_current_ones(world):
    p = world.write("a.md", "v1")
    rec = _create(world, [{"path": "a.md"}])
    p.write_text("v2")
    assert D.snapshot(world.tree, rec["id"], "a.md") == b"v1"


def test_c9_snapshots_are_content_addressed_and_shared(world):
    world.write("a.md", "same")
    world.write("b.md", "same")
    world.write("c.md", "different")
    one = _create(world, [{"path": "a.md"}])
    two = _create(world, [{"path": "b.md"}, {"path": "c.md"}])
    names = sorted(p.name for p in world.snapshots.iterdir())
    assert names == sorted({sha("same"), sha("different")})
    assert D.snapshot(world.tree, two["id"], "b.md") == b"same"
    assert D.snapshot(world.tree, one["id"], "a.md") == b"same"


@pytest.mark.parametrize("umask", [0o022, 0o000, 0o077])
def test_c9_snapshot_modes_are_0600_in_a_0700_directory(world, umask):
    world.write("a.md", "secret draft")
    old = os.umask(umask)
    try:
        _create(world, [{"path": "a.md"}])
    finally:
        os.umask(old)
    assert stat.S_IMODE(world.snapshots.stat().st_mode) == 0o700
    snap = world.snapshots / sha("secret draft")
    assert stat.S_IMODE(snap.stat().st_mode) == 0o600
    assert snap.is_file() and not snap.is_symlink()


def test_c9_the_snapshot_directory_is_under_the_project_data_dir(world):
    world.write("a.md", "x")
    _create(world, [{"path": "a.md"}])
    assert world.snapshots == world.root / ".multiagents" / "decisions" / "snapshots"
    assert world.snapshots.is_dir()


def test_c9_a_snapshot_is_kept_after_every_transition(world):
    p = world.write("a.md", "keep me")
    for how in ("answered", "withdrawn", "handled"):
        rec = _create(world, [{"path": "a.md"}])
        if how == "withdrawn":
            D.withdraw(world.tree, rec["id"], revision=1, by=ROOT_ORCH, reason="r")
        else:
            D.answer(world.tree, rec["id"], revision=1, by=CLI, options=["yes"])
            if how == "handled":
                D.mark_handled(world.tree, rec["id"], revision=2, by=ROOT_ORCH, outcome="applied")
    p.unlink()
    for rec in D.list(world.tree, limit=200)["decisions"]:
        assert D.snapshot(world.tree, rec["id"], "a.md") == b"keep me"
    assert (world.snapshots / sha("keep me")).exists()


def test_c9_a_snapshot_of_a_path_the_decision_does_not_refer_to_is_refused(world):
    world.write("a.md", "A")
    world.write("other.md", "O")
    rec = _create(world, [{"path": "a.md"}])
    with pytest.raises(D.DecisionError) as err:
        D.snapshot(world.tree, rec["id"], "other.md")
    assert code_of(err.value) in ("invalid_argument", "unknown")
    with pytest.raises(D.DecisionError):
        D.snapshot(world.tree, rec["id"], "../../etc/passwd")


def test_c9_a_snapshot_of_a_commit_ref_or_unknown_decision(world):
    head = world.init_repo()
    rec = _create(world, [{"commit": head}])
    with pytest.raises(D.DecisionError):
        D.snapshot(world.tree, rec["id"], head)
    with pytest.raises(D.DecisionError) as err:
        D.snapshot(world.tree, "d-00000000", "a.md")
    assert code_of(err.value) == "unknown"


def test_c9_the_snapshot_is_never_written_outside_the_snapshot_directory(world):
    world.write("a.md", "x")
    _create(world, [{"path": "a.md"}])
    decisions_dir = world.root / ".multiagents" / "decisions"
    assert {p.name for p in decisions_dir.iterdir()} == {"snapshots"}
