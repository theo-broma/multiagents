"""TB-R4 decisions beyond the original end-to-end rate-limit contract."""
from email.utils import formatdate

import pytest

from multiagents.runner import Runner
from multiagents.config import load
from multiagents.paths import ProjectPaths
from test_tb_b_rate_limit_resume import make_world, rl, interrupted, health


@pytest.mark.parametrize("value, expected", [("0", 0), ("3", 3), ("0.5", 0.5),
                                            ("NaN", None), ("inf", None), ("-1", None),
                                            ("tomorrow", None)])
def test_tb_r4_retry_after_seconds_and_malformed_values(value, expected):
    assert Runner._retry_after(f"Error: HTTP 429\nRetry-After: {value}\n") == expected


def test_tb_r4_retry_after_http_date(monkeypatch):
    monkeypatch.setattr("multiagents.runner.now", lambda: 1800000000)
    assert Runner._retry_after("Retry-After: " + formatdate(1800000090, usegmt=True)) == 90
    assert Runner._retry_after("Retry-After: " + formatdate(1799999990, usegmt=True)) == 0


def test_tb_r4_pending_resume_shape_and_http_date(tmp_path, monkeypatch):
    world = make_world(tmp_path, monkeypatch)
    try:
        import time
        after = time.time() + 1200
        world.rl.queue(rl(retry_after=formatdate(after, usegmt=True)))
        world.start_scheduler()
        node = world.simple("A", "rlagent")
        interrupted(world)
        pending = world.until(lambda: world.get(node).get("pending_resume"), 8)
        assert set(pending) == {"at", "attempt"}
        assert pending["attempt"] == 1
        assert pending["at"] >= int(after)
        assert world.get(node)["outcome"] is None
    finally:
        world.close()


def test_tb_r4_six_consecutive_rate_limits_hold_the_node(tmp_path, monkeypatch):
    world = make_world(tmp_path, monkeypatch, cooldown=0, threshold=1)
    try:
        world.rl.queue(*[rl(retry_after=0) for _ in range(7)])
        world.start_scheduler()
        node = world.simple("A", "rlagent")
        held = world.wait_state(node, "held", 20)
        assert held["hold"]["reason"] == "rate_limited"
        assert held["outcome"] is None
        assert not held.get("pending_resume")
        assert held["rate_limit_attempts"] == 6
        assert world.rl.spawns() == 6
        assert not health(world).get("consecutive_failures", 0)
        world.restart_scheduler()
        world.quiet(1.2)
        assert world.rl.spawns() == 6
    finally:
        world.close()


def test_tb_r4_default_cooldown_is_sixty_seconds(tmp_path):
    paths = ProjectPaths(tmp_path)
    assert load(paths, seed=False).limits["rate_limit_cooldown_seconds"] == 60
