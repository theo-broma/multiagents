"""Test-only fault injection for tests/test_cw_safe_point.py.

Loaded at interpreter start in the MCP server subprocess when CW_LAUNCH_GATE is
set (the directory is put on PYTHONPATH by the test). While the file named by
CW_LAUNCH_GATE exists, starting an agent's launch wrapper blocks: the launch is
held after its claim and reservation and before its pid is recorded. A sibling
`<gate>.waiting` file says a launch has reached the hold.
"""
import os
import subprocess
import time

_gate = os.environ.get("CW_LAUNCH_GATE")
if _gate:
    _init = subprocess.Popen.__init__

    def _gated(self, args, *a, **k):
        parts = args if isinstance(args, (list, tuple)) else [args]
        if any("agentwrap" in str(x) for x in parts):
            open(_gate + ".waiting", "a").close()
            while os.path.exists(_gate):
                time.sleep(0.05)
        _init(self, args, *a, **k)

    subprocess.Popen.__init__ = _gated
