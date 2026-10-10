"""PS2-R4: real CLI entry point, real provider child, observable request journal.

Only the settlement I/O result is injected. Both exhaustion sites must end
without allowing a RuntimeError to escape into the CLI's stderr.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
BOUND = 6.0
HELPER = r'''
import json, os, sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(sys.argv[1]) / "tests/support"))
import oc_harness as oc
from multiagents import cli, safepoint
from multiagents.compaction_requests import Requests
base, mode = Path(sys.argv[2]), sys.argv[3]
m = pytest.MonkeyPatch()
s = oc.Oc(base, m, None, declare=oc.declaration("in_session"),
          limits={"compact_grace_seconds": 0.01, "compact_timeout_seconds": 0.3})
s.reading(oc.UNDER)
# The old OC fake predates this optional plugin action.
s.script.write_text(s.script.read_text().replace('if action not in (', 'if action == "launch-command": sys.exit(64)\nif action not in (', 1))
launch = s.paths.data / "launch"
launch.mkdir(exist_ok=True)
(launch / "orchestrator.session").write_text(oc.SID)
s.ctl.write_text(json.dumps({"transcript": str(s.transcript),
    "events": str(s.paths.events_file),
    "launches": [{"life": 1.0, "appends": [[0.02, [oc.CLOSE]]]}],
    "compacts": [{"exit": 1 if mode == "compaction" else 0,
                  "stderr": "compact plugin refused" if mode == "compaction" else "",
                  "stdout": oc.FIGURES + "\n"}]}))
journal = Requests(s.paths)
record = journal.request("orchestrator", oc.SID, "in_session", "PS2 fixture", None)
write_settlement = safepoint.settle
m.setattr(safepoint, "settle", lambda paths, session, nonce, phase:
          False if phase == "settled" else write_settlement(paths, session, nonce, phase))
try:
    code = cli.main(["--path", str(s.root), "run", "--force"])
    sys.exit(code)
finally:
    (base / "observation.json").write_text(json.dumps({
        "request": journal.status("orchestrator", oc.SID, record["request_id"]),
        "calls": s.log.read_text() if s.log.exists() else ""}))
'''


@pytest.mark.parametrize("mode", ["delivery", "compaction"])
def test_ps2_r4_settlement_exhaustion_exits_without_traceback(tmp_path, mode):
    proc = subprocess.Popen([sys.executable, "-c", HELPER, str(REPO), str(tmp_path), mode],
                            env={**os.environ, "PYTHONPATH": str(REPO / "src")},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=BOUND)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate(timeout=BOUND)
    observation = json.loads((tmp_path / "observation.json").read_text())
    record = observation["request"]
    assert record["status"] == "failed", (out, err, record)
    assert "settlement" in record["failure_detail"]
    assert proc.returncode > 0, (out, err)
    assert "Traceback" not in err, err
    lines = [line for line in (out + err).splitlines() if record["request_id"] in line]
    assert len(lines) == 1 and "settlement" in lines[0].lower(), (out, err)
    calls = [json.loads(line) for line in observation["calls"].splitlines()]
    assert any(call["action"] == "exit" for call in calls), calls
    assert not any(call["action"] == "sigterm" for call in calls), calls
