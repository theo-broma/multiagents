"""Adversary, user-decisions slice 3 (UD-C13): the Stop-hook watcher and a
damaged tree.json.

Before slice 3 `bg watch` never opened tree.json. Now it reads it on every
0.1 s poll while a job runs, and `Tree.read` on an unreadable file is not a
read: it copies the damage aside to `tree.json.corrupt-<stamp>` and prints a
loud warning, every time. The watcher's stderr is the report the Stop hook
feeds the session, so the warning is repeated into the session once per poll,
and a `.corrupt-*` copy is left per second of watching.
"""
import json

from support.ud_s3_support import bind, watch_argv
from test_dj_seams import live_pid  # noqa: F401  (fixture)
from test_dj_support import CLI_BOUND, jobs  # noqa: F401  (fixture)
import subprocess


def test_adv_a_corrupt_tree_does_not_flood_the_job_report(jobs, live_pid):
    tree_file = jobs.paths.tree_file
    tree_file.parent.mkdir(parents=True, exist_ok=True)
    tree_file.write_text("{ this is not json")          # no .bak beside it
    job, _ = jobs.run("import time; time.sleep(2)", name="slow-job")
    bind(jobs, live_pid.pid)
    # timeout 63 s: the heartbeat deadline is 3 s away, so the watcher polls
    # until the job ends (about 2 s) and then reports the end.
    proc = subprocess.run(watch_argv(jobs, timeout=63), cwd=jobs.paths.root, env=jobs.env,
                          input=json.dumps({"session_id": "ud-session-a"}),
                          capture_output=True, text=True, timeout=CLI_BOUND + 60)
    assert proc.returncode == 2 and job["id"] in proc.stderr, proc.stderr[-2000:]
    warnings = [l for l in proc.stderr.splitlines() if "was unreadable" in l]
    assert len(warnings) <= 1, f"{len(warnings)} tree warnings in one Stop-hook report"
    litter = sorted(p.name for p in tree_file.parent.glob(tree_file.name + ".corrupt-*"))
    assert len(litter) <= 1, litter
