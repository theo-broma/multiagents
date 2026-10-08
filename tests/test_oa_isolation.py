"""OA-R8: a root limit pauses only the root's account.

The root runs on the host's default profile (provider `claude`); a spawned
agent of the same provider name runs on a vault account (docker executor) that
has headroom. After the driver has handled the root's limit, the admission
decision the runner consults before spawning must still admit that agent.
The decision is called as the public `Runner.start(..., admission_only=True)`
in a subprocess over the fake machine; it is never patched.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from support.oa_world import World  # noqa: E402

HOURS = 3600
ADMIT = '''
import asyncio, json, sys
from multiagents.cli import _resolve
from multiagents.config import load as load_config
from multiagents.runner import Runner, LaunchContext
paths = _resolve(sys.argv[1])
runner = Runner(paths, load_config(paths))
ctx = LaunchContext(caller=None, run_parent=None, depth=1, node_id="", attempt_id="",
                    admission_only=True)
try:
    print(json.dumps(asyncio.run(runner.start(sys.argv[2], "admission", launch_context=ctx)),
                     default=str))
except Exception as exc:
    print(json.dumps({"raised": type(exc).__name__, "text": str(exc)}))
'''


def admit(w, agent):
    p = subprocess.run([sys.executable, "-c", ADMIT, str(w.proj), agent], env=w.env(),
                       capture_output=True, text=True, cwd=str(w.proj), timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    w.project_set("", executor="docker")
    w.make_profile("claude", used=40)
    w.make_vault(None, used=0)                    # the agents' account has headroom
    # the root hits its limit; the driver is then interrupted during its wait
    w.extra_env["OA_INTERRUPT_AT_SLEEP"] = "1"
    w.plan({"launches": [{"do": "limit", "exhaust": {"used": 100, "reset_in": 3 * HOURS}}],
            "default": {"do": "ok"}})
    return w


def test_r8_control_an_agent_is_admitted_before_any_limit(world):
    assert admit(world, "implementer").get("admitted") is True


def test_r8_a_root_limit_on_the_host_profile_does_not_refuse_an_agent_on_a_vault_account(world):
    world.run(3)
    assert world.launches(), "the root ran and hit its limit"
    result = admit(world, "implementer")
    assert result.get("admitted") is True, result


def test_r8_the_agent_is_not_refused_for_the_roots_reset_either(world):
    world.run(3)
    result = admit(world, "implementer")
    assert "Paused" not in json.dumps(result) and "blocked" not in result, result
