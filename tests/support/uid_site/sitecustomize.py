"""Loaded into the scheduler's interpreter through PYTHONPATH (like oa_site).

While the file named by MA_TEST_HOST_UID_FLAG exists, `os.getuid()` and
`os.geteuid()` report 0 to the Docker backend and the gateway code, which is
where the host UID is read. Process-ownership checks (scheduler owner, tmux)
keep the real answer, so the scheduler itself still starts and runs.
"""
import os
import sys

_flag = os.environ.get("MA_TEST_HOST_UID_FLAG")
_exempt = ("multiagents.scheduler.owner", "multiagents.tmux", "multiagents.scripts", "multiagents.cli")

if _flag:
    _real = {"getuid": os.getuid, "geteuid": os.geteuid}

    def _wrap(name):
        def spoofed():
            caller = sys._getframe(1).f_globals.get("__name__", "")
            if caller.startswith("multiagents.") and caller not in _exempt and os.path.exists(_flag):
                return 0
            return _real[name]()
        return spoofed

    os.getuid = _wrap("getuid")
    os.geteuid = _wrap("geteuid")
