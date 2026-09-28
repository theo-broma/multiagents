"""Codex provider contract: the `budget` action (CX-C11, as made testable).

The quota is read from `token_count` events in rollout files
(`<home>/sessions/**/rollout-*.jsonl`), across every profile in use: the host
profile, the docker private backing when there is one, and each directory of
`MULTIAGENTS_CODEX_QUOTA_HOMES`. Never the user's own `~/.codex` by default
(CX-Q3).

The bounds are asserted without timing: at most 8 files per profile, the
newest by mtime (a counting fixture records every rollout file the adapter's
interpreter opens), and at most 1 MiB from the tail of each (a reading placed
further back than that must not be seen; a multi-GiB sparse file under a
memory limit must not be loaded).
"""

from __future__ import annotations

import json
import resource
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import codex_harness as h                              # noqa: E402

MiB = 1024 * 1024


@pytest.fixture
def fake(tmp_path):
    return h.FakeCodex(tmp_path)


@pytest.fixture
def profile(tmp_path):
    return tmp_path / "profile"


def budget(tmp_path, fake, **extra) -> dict:
    result = h.invoke(["budget"], h.base_env(tmp_path, fake, **extra))
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    data = json.loads(result.stdout)
    assert isinstance(data, dict)
    # `refresh-quota` runs this action and never a model call.
    assert fake.exec_calls() == []
    return data


def reading(ts, p5=25.0, pw=60.0, now=None, r5=None, rw=None):
    now = now or time.time()
    return h.token_count_line(ts, h.window(p5, 300, r5 or int(now + 3600)),
                              h.window(pw, 10080, rw or int(now + 5 * 86400)))


def percent(data, name):
    return data["windows"][name]["percent"]


# ------------------------------------------------------------ the reading --

def test_cx_c11_fresh_reading(tmp_path, fake, profile):
    now = time.time()
    r5, rw = int(now + 3600), int(now + 4 * 86400)
    h.write_rollout(profile, "a", [h.meta_line(now - 60, "/work"),
                                   h.message_line(now - 50, "hello"),
                                   reading(now - 30, 25.0, 60.0, now, r5, rw)])
    data = budget(tmp_path, fake)
    assert data["known"] is True
    assert data["source"] == "rollout"
    assert set(data["windows"]) == {"5h", "weekly"}
    assert percent(data, "5h") == pytest.approx(25.0)
    assert percent(data, "weekly") == pytest.approx(60.0)
    assert h.parse_instant(data["windows"]["5h"]["resets_at"]) == pytest.approx(r5, abs=1)
    assert h.parse_instant(data["windows"]["weekly"]["resets_at"]) == pytest.approx(rw, abs=1)
    assert data["headroom"] == pytest.approx(0.40, abs=1e-6)
    # resets_at of the WORST window, ISO 8601 UTC.
    assert h.parse_instant(data["resets_at"]) == pytest.approx(rw, abs=1)
    assert data["stale_seconds"] == pytest.approx(30, abs=15)


def test_cx_c11_worst_window_decides_headroom_and_reset(tmp_path, fake, profile):
    now = time.time()
    r5 = int(now + 1800)
    h.write_rollout(profile, "a", [reading(now - 5, 88.0, 20.0, now, r5=r5)])
    data = budget(tmp_path, fake)
    assert data["headroom"] == pytest.approx(0.12, abs=1e-6)
    assert h.parse_instant(data["resets_at"]) == pytest.approx(r5, abs=1)


def test_cx_c11_windows_are_mapped_by_minutes_not_position(tmp_path, fake, profile):
    now = time.time()
    line = h.token_count_line(now - 5, h.window(70.0, 10080, int(now + 86400)),
                              h.window(10.0, 300, int(now + 600)))
    h.write_rollout(profile, "a", [line])
    data = budget(tmp_path, fake)
    assert percent(data, "weekly") == pytest.approx(70.0)
    assert percent(data, "5h") == pytest.approx(10.0)


def test_cx_c11_stale_reading_reports_its_age(tmp_path, fake, profile):
    now = time.time()
    h.write_rollout(profile, "a", [reading(now - 3 * 3600, now=now)], mtime=now)
    data = budget(tmp_path, fake)
    assert data["known"] is True
    assert data["stale_seconds"] == pytest.approx(3 * 3600, abs=30)


def test_cx_c11_a_window_past_its_reset_counts_as_zero(tmp_path, fake, profile):
    now = time.time()
    rw = int(now + 2 * 86400)
    h.write_rollout(profile, "a", [h.token_count_line(
        now - 7200, h.window(95.0, 300, int(now - 600)), h.window(30.0, 10080, rw))])
    data = budget(tmp_path, fake)
    assert data["known"] is True
    five = data["windows"].get("5h")
    assert five is None or five["percent"] == 0
    assert data["headroom"] == pytest.approx(0.70, abs=1e-6)
    assert h.parse_instant(data["resets_at"]) == pytest.approx(rw, abs=1)


def test_cx_c11_a_reset_inside_the_120s_margin_still_counts(tmp_path, fake, profile):
    now = time.time()
    h.write_rollout(profile, "a", [h.token_count_line(
        now - 7200, h.window(95.0, 300, int(now - 60)), h.window(30.0, 10080, int(now + 86400)))])
    data = budget(tmp_path, fake)
    assert percent(data, "5h") == pytest.approx(95.0)
    assert data["headroom"] == pytest.approx(0.05, abs=1e-6)


# ------------------------------------------------------------ no reading --

def test_cx_c11_no_sessions_is_unknown_with_a_note(tmp_path, fake):
    data = budget(tmp_path, fake)
    assert data["known"] is False
    assert isinstance(data.get("note"), str) and data["note"].strip()


def test_cx_c11_sessions_without_rate_limits_are_unknown(tmp_path, fake, profile):
    now = time.time()
    h.write_rollout(profile, "a", [
        h.meta_line(now - 20, "/work"), h.message_line(now - 10, "hi"),
        json.dumps({"timestamp": h.iso(now - 5), "type": "event_msg",
                    "payload": {"type": "token_count", "info": None, "rate_limits": None}})])
    assert budget(tmp_path, fake)["known"] is False


def test_cx_c11_the_note_names_no_path_inside_the_users_home(tmp_path, fake):
    data = budget(tmp_path, fake)
    assert str(tmp_path) not in json.dumps(data)


# ----------------------------------------------------------- which is newest --

def test_cx_c11_event_timestamp_not_mtime_decides_newest(tmp_path, fake, profile):
    now = time.time()
    # The file touched most recently holds the OLDER reading.
    h.write_rollout(profile, "touched-late", [reading(now - 3000, 90.0, 90.0, now)], mtime=now)
    h.write_rollout(profile, "touched-early", [reading(now - 100, 20.0, 30.0, now)],
                    mtime=now - 5000)
    data = budget(tmp_path, fake)
    assert percent(data, "5h") == pytest.approx(20.0)
    assert data["stale_seconds"] == pytest.approx(100, abs=15)


def test_cx_c11_newest_event_within_one_file(tmp_path, fake, profile):
    now = time.time()
    h.write_rollout(profile, "a", [reading(now - 300, 10.0, 10.0, now),
                                   h.message_line(now - 200, "x"),
                                   reading(now - 100, 40.0, 50.0, now),
                                   h.message_line(now - 50, "y")])
    assert percent(budget(tmp_path, fake), "5h") == pytest.approx(40.0)


@pytest.mark.parametrize("newer", ["profile", "backing"])
def test_cx_c11_newest_across_host_profile_and_docker_backing(tmp_path, fake, profile, newer):
    now = time.time()
    backing = tmp_path / "backing"
    h.write_rollout(profile, "p", [reading(now - (50 if newer == "profile" else 500),
                                           11.0, 11.0, now)])
    h.write_rollout(backing, "b", [reading(now - (50 if newer == "backing" else 500),
                                           44.0, 44.0, now)])
    data = budget(tmp_path, fake, MULTIAGENTS_EXECUTOR="docker",
                  MULTIAGENTS_PRIVATE_BACKING=str(backing))
    assert percent(data, "5h") == pytest.approx(11.0 if newer == "profile" else 44.0)


def test_cx_c11_docker_budget_never_reads_the_users_own_codex(tmp_path, fake, profile):
    now = time.time()
    backing = tmp_path / "backing"
    h.write_rollout(backing, "b", [reading(now - 500, 30.0, 30.0, now)])
    h.write_rollout(tmp_path / "home" / ".codex", "own", [reading(now - 5, 99.0, 99.0, now)])
    data = budget(tmp_path, fake, MULTIAGENTS_EXECUTOR="docker",
                  MULTIAGENTS_PRIVATE_BACKING=str(backing))
    assert percent(data, "5h") == pytest.approx(30.0)


def test_cx_c11_local_budget_never_reads_the_users_own_codex(tmp_path, fake):
    now = time.time()
    h.write_rollout(tmp_path / "home" / ".codex", "own", [reading(now - 5, 99.0, 99.0, now)])
    assert budget(tmp_path, fake)["known"] is False


def test_cx_c11_extra_quota_homes_are_read(tmp_path, fake, profile):
    now = time.time()
    extra_a, extra_b = tmp_path / "extra-a", tmp_path / "extra-b"
    h.write_rollout(profile, "p", [reading(now - 900, 10.0, 10.0, now)])
    h.write_rollout(extra_a, "a", [reading(now - 600, 20.0, 20.0, now)])
    h.write_rollout(extra_b, "b", [reading(now - 30, 77.0, 80.0, now)])
    data = budget(tmp_path, fake,
                  MULTIAGENTS_CODEX_QUOTA_HOMES=f"{extra_a}::{tmp_path / 'missing'}:{extra_b}:")
    assert percent(data, "5h") == pytest.approx(77.0)
    assert percent(data, "weekly") == pytest.approx(80.0)


def test_cx_c11_extra_quota_homes_alone_are_enough(tmp_path, fake):
    now = time.time()
    extra = tmp_path / "extra"
    h.write_rollout(extra, "a", [reading(now - 30, 33.0, 44.0, now)])
    data = budget(tmp_path, fake, MULTIAGENTS_CODEX_QUOTA_HOMES=str(extra))
    assert data["known"] is True
    assert percent(data, "5h") == pytest.approx(33.0)


def test_cx_c11_extra_quota_homes_are_only_read(tmp_path, fake):
    now = time.time()
    extra = tmp_path / "extra"
    h.write_rollout(extra, "a", [reading(now - 30, now=now)])
    before = sorted((p, p.stat().st_mtime_ns) for p in extra.rglob("*"))
    budget(tmp_path, fake, MULTIAGENTS_CODEX_QUOTA_HOMES=str(extra))
    assert sorted((p, p.stat().st_mtime_ns) for p in extra.rglob("*")) == before


# ------------------------------------------------------------------ bounds --

def test_cx_c11_only_the_8_newest_files_by_mtime_are_examined(tmp_path, fake, profile):
    now = time.time()
    for i in range(8):
        h.write_rollout(profile, f"recent-{i}", [h.message_line(now - 100 - i, "no limits")],
                        mtime=now - i)
    # The 9th newest file by mtime holds the only reading, and the newest event.
    h.write_rollout(profile, "ninth", [reading(now - 1, 90.0, 90.0, now)], mtime=now - 3600)
    assert budget(tmp_path, fake)["known"] is False


def test_cx_c11_a_ninth_file_does_not_override_the_eight_newest(tmp_path, fake, profile):
    now = time.time()
    for i in range(8):
        h.write_rollout(profile, f"recent-{i}", [reading(now - 500 - i, 20.0, 20.0, now)],
                        mtime=now - i)
    h.write_rollout(profile, "ninth", [reading(now - 1, 90.0, 90.0, now)], mtime=now - 3600)
    assert percent(budget(tmp_path, fake), "5h") == pytest.approx(20.0)


def test_cx_c11_the_file_bound_is_per_profile(tmp_path, fake, profile):
    now = time.time()
    extra = tmp_path / "extra"
    for i in range(8):
        h.write_rollout(profile, f"p-{i}", [h.message_line(now - 100, "x")], mtime=now - i)
    h.write_rollout(extra, "e", [reading(now - 30, 55.0, 55.0, now)], mtime=now - 9999)
    data = budget(tmp_path, fake, MULTIAGENTS_CODEX_QUOTA_HOMES=str(extra))
    assert percent(data, "5h") == pytest.approx(55.0)


def test_cx_c11_counting_fixture_at_most_8_files_opened_per_profile(tmp_path, fake, profile):
    now = time.time()
    extra = tmp_path / "extra"
    for home in (profile, extra):
        for i in range(20):
            h.write_rollout(home, f"f{i:02d}", [reading(now - 100 - i, 10.0, 10.0, now)],
                            mtime=now - i, day=f"2026/09/{i % 5 + 10:02d}")
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(h.SITECUSTOMIZE)
    log = tmp_path / "opens.log"
    data = budget(tmp_path, fake, PYTHONPATH=str(site), CODEX_TEST_OPEN_LOG=str(log),
                  MULTIAGENTS_CODEX_QUOTA_HOMES=str(extra))
    assert data["known"] is True
    lines = log.read_text().splitlines()
    assert "#loaded" in lines, "the counting fixture did not load into the adapter"
    opened = {line for line in lines if line != "#loaded"}
    for home in (profile, extra):
        mine = {p for p in opened if p.startswith(str(home) + "/")}
        assert len(mine) <= 8, sorted(mine)
        # ... and they are the newest by mtime: f00..f07.
        assert {Path(p).name for p in mine} <= {f"rollout-f{i:02d}.jsonl" for i in range(8)}


def test_cx_c11_a_reading_beyond_the_1mib_tail_is_not_seen(tmp_path, fake, profile):
    now = time.time()
    filler = h.message_line(now - 10, "f" * 1000)
    lines = [reading(now - 20, 90.0, 90.0, now)]
    lines += [filler] * ((MiB + 256 * 1024) // len(filler))
    h.write_rollout(profile, "big", lines)
    assert budget(tmp_path, fake)["known"] is False


def test_cx_c11_a_reading_at_the_end_of_a_large_file_is_seen(tmp_path, fake, profile):
    now = time.time()
    filler = h.message_line(now - 100, "f" * 1000)
    lines = [filler] * (3 * MiB // len(filler)) + [reading(now - 20, 42.0, 43.0, now)]
    h.write_rollout(profile, "big", lines)
    assert percent(budget(tmp_path, fake), "5h") == pytest.approx(42.0)


def test_cx_c11_a_huge_file_is_never_loaded_whole(tmp_path, fake, profile):
    """A 4 GiB sparse file ending in a reading, under a 1 GiB address-space cap."""
    now = time.time()
    path = h.write_rollout(profile, "huge", [])
    with path.open("r+b") as fh:
        fh.seek(4 * 1024 * MiB)
        fh.write(b"\n" + reading(now - 20, 61.0, 62.0, now).encode() + b"\n")

    def cap():
        resource.setrlimit(resource.RLIMIT_AS, (1024 * MiB, 1024 * MiB))

    result = h.invoke(["budget"], h.base_env(tmp_path, fake), preexec_fn=cap, timeout=60)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["known"] is True
    assert percent(data, "5h") == pytest.approx(61.0)


# ----------------------------------------------------------- malformed input --

def _malformed(now):
    ok5, okw = int(now + 3600), int(now + 86400)
    ts = h.iso(now - 5)

    def limits(primary, secondary=None):
        return json.dumps({"timestamp": ts, "type": "event_msg", "payload": {
            "type": "token_count", "rate_limits": {
                "primary": primary,
                "secondary": secondary or h.window(90.0, 10080, okw)}}})

    good = reading(now - 5, 90.0, 90.0, now)
    return {
        "truncated": good[: len(good) // 2].encode(),
        "not-utf8": b"\xff\xfe\x00garbage\x80\x81{\"type\":",
        "not-an-object": b"[1, 2, 3]",
        "rate-limits-a-string": json.dumps({"timestamp": ts, "type": "event_msg", "payload": {
            "type": "token_count", "rate_limits": "lots"}}).encode(),
        "no-used-percent": limits({"window_minutes": 300, "resets_at": ok5}).encode(),
        "no-window-minutes": limits({"used_percent": 90.0, "resets_at": ok5}).encode(),
        "percent-a-string": limits(h.window("90", 300, ok5)).encode(),
        "percent-over-100": limits(h.window(150.0, 300, ok5)).encode(),
        "percent-negative": limits(h.window(-5.0, 300, ok5)).encode(),
        "resets-at-not-a-number": limits(h.window(90.0, 300, "soon")).encode(),
        "no-timestamp": json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "rate_limits": {
                "primary": h.window(90.0, 300, ok5),
                "secondary": h.window(90.0, 10080, okw)}}}).encode(),
    }


@pytest.mark.parametrize("kind", list(_malformed(0.0)))
def test_cx_c11_a_malformed_newer_event_is_skipped(tmp_path, fake, profile, kind):
    now = time.time()
    h.write_rollout(profile, "a", [reading(now - 600, 10.0, 12.0, now), _malformed(now)[kind]])
    data = budget(tmp_path, fake)
    assert data["known"] is True
    assert percent(data, "5h") == pytest.approx(10.0)
    assert percent(data, "weekly") == pytest.approx(12.0)


def test_cx_c11_only_malformed_input_is_unknown_not_a_crash(tmp_path, fake, profile):
    now = time.time()
    h.write_rollout(profile, "a", list(_malformed(now).values()))
    (profile / "sessions" / "2026" / "09" / "28" / "rollout-dir.jsonl").mkdir()
    assert budget(tmp_path, fake)["known"] is False


# ---------------------------------------------------------------- privacy --

def test_cx_c11_never_emits_message_bodies_or_paths(tmp_path, fake, profile):
    now = time.time()
    secret = "BODY-" + h.SECRET
    h.write_rollout(profile, "a", [
        h.meta_line(now - 60, "/private/" + secret),
        h.message_line(now - 50, secret),
        h.token_count_line(now - 40, h.window(25.0, 300, int(now + 3600)),
                           h.window(35.0, 10080, int(now + 86400)),
                           info={"note": secret, "last_token_usage": {"input_tokens": 1}}),
        h.message_line(now - 30, secret),
    ])
    result = h.invoke(["budget"], h.base_env(tmp_path, fake))
    assert result.returncode == 0, result.stderr
    assert secret not in result.stdout + result.stderr
    assert str(tmp_path) not in result.stdout
    assert json.loads(result.stdout)["known"] is True
