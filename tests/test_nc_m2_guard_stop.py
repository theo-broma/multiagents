"""NC-R16/R17: stopping prevents an unlaunched worker from spending its claim."""
import asyncio
import subprocess
import sys

from test_nc_m2_guard_recovery import local, deposit, claim, read


def test_nc_r16_stop_abandons_an_unlaunched_claim_while_its_worker_is_booting(local, monkeypatch):
    _, engine = local
    node = deposit(engine)
    attempt = claim(engine, node)
    popen = subprocess.Popen

    def slow_boot(argv, **kwargs):
        return popen([sys.executable, "-c", "import time; time.sleep(10)"], **kwargs)

    monkeypatch.setattr("multiagents.scheduler.engine.subprocess.Popen", slow_boot)
    engine.spawn(attempt)
    engine.start()
    engine.stop()
    nodes, journal = read(engine)
    assert engine.children[0].poll() is None
    assert journal[attempt["attempt_id"]]["state"] == "abandoned"
    assert nodes[node["id"]]["state"] == "open"


def test_nc_r16_a_stopped_claim_cannot_be_spent_by_a_worker_that_boots_later(local):
    world, engine = local
    node = deposit(engine)
    attempt = claim(engine, node)
    engine.start()
    engine.stop()
    from multiagents.scheduler.worker import supervise
    asyncio.run(supervise(world.paths.root, attempt["attempt_id"]))
    assert world.fx.calls() == []
    assert engine.runner.tree.get(attempt["run_id"]) is None
    nodes, journal = read(engine)
    assert nodes[node["id"]]["state"] == "open"
    assert journal[attempt["attempt_id"]]["state"] == "abandoned"
