"""TM-R1: `multiagents view <agent_id>` — the follower that makes a stream readable."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
from d2_support import (agent_id, err_text, has_raw_control, out_text,  # noqa: E402
                        proj, visible_escape)

AID = agent_id(1)


def ev(kind, **kw):
    return {"t": 1.0, "kind": kind, "name": "", "args": None, "state": "", "status": "",
            "step": 0, "text": "", **kw}


ALL_KINDS = [
    ev("text", text="the assistant says hello there"),
    ev("tool", name="Read", args={"file_path": "/srv/app/widget_core.py"}, state="started"),
    ev("step", step=3, text="stepping along"),
    ev("raw", text="an unparsed line from the cli"),
    ev("error", text="kaboom exploded here"),
    ev("result", status="SUCCESS", text="finished the whole thing"),
]


def line_with(out: str, *needles: str) -> str | None:
    for line in out.splitlines():
        if all(n in line for n in needles):
            return line
    return None


# ------------------------------------------------------------ rendering ----

def test_tm_r1_fixture_stream_renders_every_event_kind(proj):
    proj.add_agent(AID, "done", ALL_KINDS)
    cp = proj.run("view", AID, "--no-follow", tmux=False)
    out = out_text(cp)
    assert cp.returncode == 0, err_text(cp)
    assert "the assistant says hello there" in out
    assert "an unparsed line from the cli" in out
    assert "stepping along" in out or line_with(out, "3") is not None
    assert "kaboom exploded here" in out
    assert "finished the whole thing" in out


def test_tm_r1_tool_call_shows_name_and_argument_summary_on_one_line(proj):
    proj.add_agent(AID, "done", ALL_KINDS)
    out = out_text(proj.run("view", AID, "--no-follow", tmux=False))
    assert line_with(out, "Read", "widget_core.py"), out


def test_tm_r1_tool_call_with_huge_args_is_a_single_line_summary(proj):
    args = {"command": "echo " + "y" * 6000, "other": ["a"] * 50}
    proj.add_agent(AID, "done", [ev("tool", name="Bash", args=args), ev("text", text="after-marker")])
    out = out_text(proj.run("view", AID, "--no-follow", tmux=False))
    lines = [l for l in out.splitlines() if "Bash" in l]
    assert len(lines) == 1, out
    assert len(lines[0]) <= 4000


def test_tm_r1_errors_and_result_are_marked(proj):
    proj.add_agent(AID, "done", [ev("text", text="plainone"), ev("error", text="badthing"),
                                 ev("result", status="SUCCESS", text="wrapup")])
    out = out_text(proj.run("view", AID, "--no-follow", tmux=False))
    plain, err, res = (line_with(out, w) for w in ("plainone", "badthing", "wrapup"))
    assert plain and err and res, out
    assert "error" in err.lower() or "err" in err.lower() or "!" in err
    assert err.strip() != "badthing" and res.strip() != "wrapup"
    assert re.search(r"result|success|final", res, re.I), res
    # the marking distinguishes them from assistant text
    assert plain.replace("plainone", "").strip() != err.replace("badthing", "").strip()


def test_tm_r1_assistant_text_is_wrapped(proj):
    words = " ".join(f"word{i:03d}" for i in range(150))
    proj.add_agent(AID, "done", [ev("text", text=words)])
    out = out_text(proj.run("view", AID, "--no-follow", tmux=False))
    lines = [l for l in out.splitlines() if "word" in l]
    assert len(lines) > 1, "1100 characters of prose came out as one line"
    assert max(len(l) for l in lines) <= 200
    assert re.findall(r"word\d{3}", out) == [f"word{i:03d}" for i in range(150)]


def test_tm_r1_newlines_inside_text_survive(proj):
    proj.add_agent(AID, "done", [ev("text", text="first para\nsecond para")])
    out = out_text(proj.run("view", AID, "--no-follow", tmux=False))
    a, b = line_with(out, "first para"), line_with(out, "second para")
    assert a and b and a is not b


def test_tm_r1_tabs_inside_text_survive(proj):
    proj.add_agent(AID, "done", [ev("text", text="col1\tcol2")])
    cp = proj.run("view", AID, "--no-follow", tmux=False)
    assert b"col1\tcol2" in cp.stdout


# ---------------------------------------------------------------- bounds ----

def test_tm_r1_event_under_the_limit_is_not_cut(proj):
    proj.add_agent(AID, "done", [ev("text", text="Q" * 2000)])
    out = out_text(proj.run("view", AID, "--no-follow", tmux=False))
    assert out.count("Q") == 2000


@pytest.mark.parametrize("where", ["text", "error", "result", "raw", "tool"])
def test_tm_r1_event_over_4000_chars_is_cut_with_a_marker(proj, where):
    big = "Q" * 10000
    e = {"text": ev("text", text=big), "error": ev("error", text=big),
         "result": ev("result", status="SUCCESS", text=big), "raw": ev("raw", text=big),
         "tool": ev("tool", name="Bash", args={"command": big})}[where]
    proj.add_agent(AID, "done", [e, ev("text", text="tail-event")])
    out = out_text(proj.run("view", AID, "--no-follow", tmux=False))
    assert 0 < out.count("Q") <= 4000
    assert re.search(r"truncat|cut|more|omitted|…|\.\.\.", out, re.I)
    assert "tail-event" in out


# ------------------------------------------------------- untrusted input ----

PAYLOAD = "a\x1b]0;x\x07b\x1b[2Jc\rd\x08e\u009bf\x00g\x7fh"


def test_tm_r1_control_characters_are_escaped_visibly(proj):
    proj.add_agent(AID, "done", [ev("text", text=PAYLOAD + "\nline2\tTab")])
    cp = proj.run("view", AID, "--no-follow", tmux=False)
    out = out_text(cp)
    assert cp.returncode == 0, err_text(cp)
    assert has_raw_control(cp.stdout) == []
    for code in (0x1b, 0x07, 0x0d, 0x08, 0x9b):
        assert visible_escape(out, code), (hex(code), out)
    # the printable payload around them is not silently dropped
    for piece in ("a", "]0;x", "[2J", "line2"):
        assert piece in out
    assert b"line2\tTab" in cp.stdout


@pytest.mark.parametrize("field", ["text", "name", "args_value", "args_key", "status", "raw"])
@pytest.mark.parametrize("kind", ["text", "tool", "step", "result", "raw", "error"])
def test_tm_r1_every_rendered_field_of_every_kind_is_sanitised(proj, kind, field):
    bad = "\x1b[2J\x1b]0;pwn\x07\r\x9b\x08"
    e = ev(kind)
    if field == "text":
        e["text"] = "t" + bad
    elif field == "name":
        e["name"] = "n" + bad
    elif field == "args_value":
        e["args"] = {"k": "v" + bad}
    elif field == "args_key":
        e["args"] = {"k" + bad: "v"}
    elif field == "status":
        e["status"] = "s" + bad
    else:
        e["raw"] = "r" + bad
    proj.add_agent(AID, "done", [e])
    cp = proj.run("view", AID, "--no-follow", tmux=False)
    assert cp.returncode == 0, err_text(cp)
    assert has_raw_control(cp.stdout) == []
    assert has_raw_control(cp.stderr) == []


def test_tm_r1_control_bytes_in_the_stream_file_itself_are_safe(proj):
    # Raw bytes (not JSON escapes): ESC and 0x9b inside a line that is not JSON.
    proj.add_agent(AID, "done", [ev("text", text="before")],
                   raw_lines=[b"\x1b[31mnot json\x9b\r\n", b"\xff\xfe\x00garbage\n"])
    proj.append(AID, ev("text", text="after"))
    cp = proj.run("view", AID, "--no-follow", tmux=False)
    assert cp.returncode == 0
    assert has_raw_control(cp.stdout) == []
    assert b"before" in cp.stdout and b"after" in cp.stdout


def test_tm_r1_stream_content_is_never_run_by_a_shell(proj, tmp_path):
    marker = tmp_path / "PWNED"
    evil = [f"$(touch {marker})", f"`touch {marker}`", f"; touch {marker} #",
            f"'; touch {marker}; '", f"\n!touch {marker}"]
    events = [ev("text", text=s) for s in evil]
    events += [ev("tool", name=s, args={"c": s}) for s in evil]
    proj.add_agent(AID, "done", events)
    cp = proj.run("view", AID, "--no-follow", tmux=False, cwd=tmp_path)
    assert cp.returncode == 0
    assert not marker.exists()
    assert f"$(touch {marker})" in out_text(cp)          # printed literally


def test_tm_r1_stream_content_does_not_reach_tmux(proj):
    proj.add_agent(AID, "done", [ev("text", text="-t evil:0 kill-server")])
    proj.run("view", AID, "--no-follow", tmux=True)
    assert proj.tmux.calls == []


# ------------------------------------------------------ bad / odd lines ----

def test_tm_r1_malformed_lines_are_skipped_not_fatal(proj):
    raw = [b'{"kind": "text", "text": "cut off\n', b"\n", b"not json at all\n",
           b"[1, 2, 3]\n", b"42\n", b"null\n", b'"a string"\n',
           b'{"kind": "text", "text": 17}\n', b'{"kind": "text", "text": null}\n',
           b'{"kind": "tool", "name": null, "args": "flat string"}\n',
           b'{"kind": "tool", "name": 5, "args": [1, {"a": null}]}\n',
           b'{"kind": "mystery-kind", "text": "unknown"}\n',
           b'{"text": "no kind at all"}\n',
           b'{"kind": "text", "text": {"nested": "dict"}}\n']
    proj.add_agent(AID, "done", [ev("text", text="first-good")], raw_lines=raw)
    proj.append(AID, ev("text", text="last-good"))
    cp = proj.run("view", AID, "--no-follow", tmux=False)
    assert cp.returncode == 0, err_text(cp)
    assert "Traceback" not in err_text(cp) + out_text(cp)
    out = out_text(cp)
    assert "first-good" in out and "last-good" in out
    assert out.count("first-good") == 1 and out.count("last-good") == 1
    assert "cut off" not in out                            # the unparsable line


def test_tm_r1_empty_stream_prints_nothing_and_exits_zero(proj):
    proj.add_agent(AID, "done", [])
    cp = proj.run("view", AID, "--no-follow", tmux=False)
    assert cp.returncode == 0
    assert "Traceback" not in err_text(cp)


def test_tm_r1_truncated_last_line_is_not_printed_in_no_follow(proj):
    proj.add_agent(AID, "done", [ev("text", text="whole")],
                   raw_lines=[b'{"kind": "text", "text": "half wri'])
    cp = proj.run("view", AID, "--no-follow", tmux=False)
    assert cp.returncode == 0
    assert "whole" in out_text(cp)
    assert "half wri" not in out_text(cp)


# ------------------------------------------------------------------ ids ----

BAD_IDS = ["../x", "ag-zzz", "ag-ABCDEF", "ag-abc12", "ag-abc1234", "ag-abc123-",
           "ag-abc123-x", "ag-abc123-1-2", "ag-abc123\n", " ag-abc123", "ag-abc123 ",
           "ag-abc123/../ag-abc124", "/etc/passwd", "ag-abc123-١", "x-abc123",
           "ag_abc123", "AG-abc123", "ag-ab\u0441123"]


@pytest.mark.parametrize("bad", BAD_IDS)
def test_tm_r1_malformed_agent_id_exits_2_before_touching_any_path(proj, bad):
    decoy = proj.paths.data / "x"
    decoy.mkdir()
    (decoy / "stream.jsonl").write_text('{"kind":"text","text":"DECOY_LEAK"}\n')
    cp = proj.run("view", bad, "--no-follow", tmux=False)
    assert cp.returncode == 2, (cp.returncode, err_text(cp))
    assert "DECOY_LEAK" not in out_text(cp) + err_text(cp)
    assert err_text(cp).strip() != ""


def test_tm_r1_missing_agent_id_argument_is_a_usage_error(proj):
    cp = proj.run("view", tmux=False)
    assert cp.returncode == 2


def test_tm_r1_unknown_agent_id_exits_2_with_a_message(proj):
    cp = proj.run("view", agent_id(9), "--no-follow", tmux=False)
    assert cp.returncode == 2
    assert agent_id(9) in err_text(cp) + out_text(cp) or err_text(cp).strip()
    assert "Traceback" not in err_text(cp)


@pytest.mark.parametrize("aid", ["ag-abcdef", "ag-000000", "ag-abc123-1", "ag-abc123-000", "ag-fedcba-99999"])
def test_tm_r1_well_formed_ids_are_accepted(proj, aid):
    proj.add_agent(aid, "done", [ev("text", text="hello-there")])
    cp = proj.run("view", aid, "--no-follow", tmux=False)
    assert cp.returncode == 0, err_text(cp)
    assert "hello-there" in out_text(cp)


# ---------------------------------------------------- symlinks and FIFOs ----

def test_tm_r1_symlinked_stream_is_refused(proj, tmp_path):
    secret = tmp_path / "secret.jsonl"
    secret.write_text('{"kind": "text", "text": "TOP-SECRET-CONTENT"}\n')
    proj.add_agent(AID, "done")
    proj.stream(AID).symlink_to(secret)
    cp = proj.run("view", AID, "--no-follow", tmux=False)
    assert cp.returncode != 0
    assert "TOP-SECRET-CONTENT" not in out_text(cp) + err_text(cp)
    assert err_text(cp).strip()


def test_tm_r1_dangling_symlinked_stream_is_refused_not_waited_for(proj, tmp_path):
    proj.add_agent(AID, "running")
    proj.stream(AID).symlink_to(tmp_path / "nowhere")
    cp = proj.run("view", AID, "--no-follow", tmux=False, timeout=15)
    assert cp.returncode != 0


def test_tm_r1_symlinked_stream_is_refused_in_follow_mode_too(proj, tmp_path):
    secret = tmp_path / "secret.jsonl"
    secret.write_text('{"kind": "text", "text": "TOP-SECRET-CONTENT"}\n')
    proj.add_agent(AID, "running")
    proj.stream(AID).symlink_to(secret)
    f = proj.follow("view", AID, "--follow")
    try:
        code = f.wait_exit(15)
        assert code not in (None, 0), f.out
        assert "TOP-SECRET-CONTENT" not in f.out
    finally:
        f.stop()


def test_tm_r1_fifo_stream_is_refused_without_blocking(proj):
    proj.add_agent(AID, "done")
    os.mkfifo(proj.stream(AID))
    cp = proj.run("view", AID, "--no-follow", tmux=False, timeout=15)   # a hang fails here
    assert cp.returncode != 0
    assert err_text(cp).strip()


def test_tm_r1_directory_as_stream_is_refused(proj):
    proj.add_agent(AID, "done")
    proj.stream(AID).mkdir()
    cp = proj.run("view", AID, "--no-follow", tmux=False, timeout=15)
    assert cp.returncode != 0
    assert "Traceback" not in err_text(cp)


# ------------------------------------------------- following / lifecycle ----

def test_tm_r1_no_follow_on_a_finished_run_exits_zero(proj):
    proj.add_agent(AID, "done", ALL_KINDS)
    cp = proj.run("view", AID, "--no-follow", tmux=False)
    assert cp.returncode == 0


def test_tm_r1_no_follow_on_an_active_run_prints_what_exists_and_exits_zero(proj):
    proj.add_agent(AID, "running", [ev("text", text="so-far")])
    cp = proj.run("view", AID, "--no-follow", tmux=False, timeout=15)
    assert cp.returncode == 0
    assert "so-far" in out_text(cp)


def test_tm_r1_no_follow_with_no_stream_yet_exits_zero(proj):
    proj.add_agent(AID, "running")
    cp = proj.run("view", AID, "--no-follow", tmux=False, timeout=15)
    assert cp.returncode == 0
    assert "Traceback" not in err_text(cp)


@pytest.mark.parametrize("flags", [(), ("--follow",)])
def test_tm_r1_terminal_run_prints_everything_and_final_status_then_exits(proj, flags):
    proj.add_agent(AID, "failed", [ev("text", text="all-of-it")])
    # TS-R2: a 1 s linger instead of TM-R3's 60 s; the 60 s default is pinned
    # by test_tm_r3_the_view_lingers_60s_after_the_run_is_terminal.
    f = proj.follow("view", AID, *flags, linger=1)
    try:
        code = f.wait_exit(90)          # TM-R3 allows a grace period of about 60 s
        assert code is not None, f.out
        assert "all-of-it" in f.out
        assert "failed" in f.out.lower()
        assert f.out.lower().rindex("failed") > f.out.index("all-of-it")
    finally:
        f.stop()


def test_tm_r3_the_view_lingers_60s_after_the_run_is_terminal(proj, monkeypatch, capsys):
    """TM-R3 at the production default, on a fake clock (TS-R2, TS-R3a): a
    followed view of a terminal run keeps following for 60 s, then exits. The
    subprocess tests above shorten the linger; this is what keeps its default
    and its two sides tested."""
    from multiagents import viewer

    class Clock:
        start = now = 1_000_000.0

        def time(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds
            if self.now - self.start > 600:
                raise AssertionError("the view did not exit after its linger")

        def __getattr__(self, name):
            return getattr(time, name)

    clock = Clock()
    monkeypatch.setattr(viewer, "time", clock)
    proj.add_agent(AID, "done", [ev("text", text="the-end")])
    viewer.view_stream(proj.paths, AID, follow=True)
    out = capsys.readouterr().out
    assert "the-end" in out and "final status: done" in out, out
    lingered = clock.now - clock.start
    assert 60 < lingered <= 61, f"lingered {lingered:.1f}s, TM-R3 says about 60 s"


def test_tm_r1_terminal_run_without_a_stream_does_not_wait_forever(proj):
    proj.add_agent(AID, "done")
    f = proj.follow("view", AID, "--follow")
    try:
        assert f.wait_exit(90) is not None, f.out
        assert "Traceback" not in f.out
    finally:
        f.stop()


def test_tm_r1_default_on_an_active_run_is_to_follow(proj):
    proj.add_agent(AID, "running", [ev("text", text="one")])
    f = proj.follow("view", AID)
    try:
        assert f.wait_for("one")
        time.sleep(1.5)
        assert f.alive(), "exited although the run is still active"
        proj.append(AID, ev("text", text="two"))
        assert f.wait_for("two")
    finally:
        f.stop()


def test_tm_r1_follow_prints_new_events_then_final_status_when_the_run_ends(proj):
    proj.add_agent(AID, "running", [ev("text", text="early")])
    f = proj.follow("view", AID, "--follow", linger=1)      # TS-R2, as above
    try:
        assert f.wait_for("early")
        proj.append(AID, ev("text", text="later"), ev("result", status="SUCCESS", text="wrapped"))
        assert f.wait_for("wrapped")
        assert f.alive()                        # still active: still following
        proj.set_status(AID, "cancelled")
        assert f.wait_for("cancelled", 15), f.out
        assert f.out.count("early") == 1 and f.out.count("later") == 1
        assert f.wait_exit(90) is not None
    finally:
        f.stop()


def test_tm_r1_stream_that_appears_later_is_waited_for(proj):
    proj.add_agent(AID, "running")
    f = proj.follow("view", AID, "--follow")
    try:
        time.sleep(1.5)
        assert f.alive(), f.out
        proj.stream(AID).write_text("")
        proj.append(AID, ev("text", text="finally-here"))
        assert f.wait_for("finally-here")
    finally:
        f.stop()


def test_tm_r1_a_line_still_being_written_is_printed_once_when_complete(proj):
    proj.add_agent(AID, "running", [ev("text", text="anchor")])
    f = proj.follow("view", AID, "--follow")
    try:
        assert f.wait_for("anchor")
        proj.append(AID, raw=b'{"kind": "text", "text": "hal')
        time.sleep(1.5)
        assert "hal" not in f.out.replace("anchor", "")
        proj.append(AID, raw=b'f-and-half"}\n')
        assert f.wait_for("half-and-half")
        time.sleep(1.5)
        assert f.out.count("half-and-half") == 1
        assert "hal " not in f.out and "halfand" not in f.out
    finally:
        f.stop()


def test_tm_r1_follow_survives_malformed_lines_between_good_ones(proj):
    proj.add_agent(AID, "running", [ev("text", text="g1")])
    f = proj.follow("view", AID, "--follow")
    try:
        assert f.wait_for("g1")
        proj.append(AID, raw=b"garbage {{{\n\xff\xfe\n")
        proj.append(AID, ev("text", text="g2"))
        assert f.wait_for("g2")
        assert f.alive()
    finally:
        f.stop()


def test_tm_r1_truncation_mid_follow_prints_one_marker_then_the_new_content(proj):
    proj.add_agent(AID, "running", [ev("text", text="old-alpha " + "x" * 200),
                                    ev("text", text="old-beta " + "x" * 200)])
    f = proj.follow("view", AID, "--follow")
    try:
        assert f.wait_for("old-beta")
        os.truncate(proj.stream(AID), 0)
        time.sleep(2.0)
        proj.append(AID, ev("text", text="new-gamma"))
        assert f.wait_for("new-gamma")
        lines = f.out.splitlines()
        b = max(i for i, l in enumerate(lines) if "old-beta" in l)
        g = min(i for i, l in enumerate(lines) if "new-gamma" in l)
        between = [l for l in lines[b + 1:g] if l.strip() and set(l.strip()) != {"x"}]
        assert len(between) == 1, between                   # a one-line marker
        assert f.out.count("old-alpha") == 1 and f.out.count("old-beta") == 1
        assert f.alive()
    finally:
        f.stop()


def test_tm_r1_truncation_then_rewrite_larger_than_before_is_still_noticed(proj):
    proj.add_agent(AID, "running", [ev("text", text="short-one")])
    f = proj.follow("view", AID, "--follow")
    try:
        assert f.wait_for("short-one")
        os.truncate(proj.stream(AID), 0)
        time.sleep(2.0)
        proj.append(AID, ev("text", text="big-one " + "y" * 3000), ev("text", text="big-two"))
        assert f.wait_for("big-two")
        assert "big-one" in f.out
        assert f.out.count("short-one") == 1
    finally:
        f.stop()


def test_tm_r1_replaced_file_is_reopened_from_the_start(proj):
    proj.add_agent(AID, "running", [ev("text", text="orig-one")])
    f = proj.follow("view", AID, "--follow")
    try:
        assert f.wait_for("orig-one")
        new = proj.run_dir(AID) / "stream.new"
        import json
        new.write_text("".join(json.dumps(ev("text", text=t)) + "\n" for t in
                               ("swap-1 " + "z" * 500, "swap-2")))
        os.replace(new, proj.stream(AID))
        assert f.wait_for("swap-2")
        assert "swap-1" in f.out
        assert f.out.count("orig-one") == 1
    finally:
        f.stop()


# ------------------------------------------------------------ read-only ----

@pytest.mark.parametrize("status,flags", [("done", ["--no-follow"]), ("running", ["--no-follow"]),
                                          ("done", [])])
def test_tm_r1_viewing_writes_nothing_under_the_project(proj, status, flags):
    proj.add_agent(AID, status, ALL_KINDS, raw_lines=[b"junk\n", b'{"partial": ' ])
    (proj.run_dir(AID) / "result.json").write_text('{"ok": true}')
    (proj.run_dir(AID) / "prompt.md").write_text("do the thing")
    before = proj.snapshot_of_files(proj.root)
    state_before = sorted(str(p) for p in proj.state.rglob("*"))
    cp = proj.run("view", AID, *flags, tmux=False, timeout=100)
    assert cp.returncode == 0, err_text(cp)
    assert proj.snapshot_of_files(proj.root) == before
    assert sorted(str(p) for p in proj.state.rglob("*")) == state_before


def test_tm_r1_following_writes_nothing_either(proj):
    proj.add_agent(AID, "running", ALL_KINDS)
    before = proj.snapshot_of_files(proj.paths.data)
    f = proj.follow("view", AID, "--follow")
    try:
        assert f.wait_for("finished the whole thing")
        time.sleep(1.0)
    finally:
        f.stop()
    assert proj.snapshot_of_files(proj.paths.data) == before


def test_tm_r1_view_needs_no_tmux_and_never_calls_it(proj):
    proj.add_agent(AID, "done", ALL_KINDS)
    cp = proj.run("view", AID, "--no-follow", tmux=True)
    assert cp.returncode == 0
    assert proj.tmux.calls == []
