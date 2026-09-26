"""A second process reading claude's budget — for QF-R2's per-machine bound.

Every agent runs its own server process, so "one fetch per machine" is a claim
about processes that share nothing but the disk. Two readers in one pytest
process would share module state and prove nothing about that; this runs one
in its own interpreter, with the same seams the in-process tests use.

    python qf_reader.py <spec.json>

The spec names the state and config dirs, the claude home, the endpoint's
answer and the file its calls are counted in, and optionally a fixed clock.
Prints the budget's `to_dict()` as one JSON line.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


class _Patch:
    """Just enough of pytest's monkeypatch for the harness classes."""

    def setattr(self, target, name, value):
        setattr(target, name, value)


def main() -> int:
    spec = json.loads(Path(sys.argv[1]).read_text())
    for name in list(os.environ):
        if name.startswith(("MULTIAGENTS_", "CLAUDE_")):
            del os.environ[name]
    os.environ["MULTIAGENTS_STATE_DIR"] = spec["state_dir"]
    os.environ["MULTIAGENTS_CONFIG_DIR"] = spec["config_dir"]

    import qf_harness as h
    from multiagents import budget

    patch = _Patch()
    if spec.get("clock") is not None:
        fixed = float(spec["clock"])
        patch.setattr(time, "time", lambda: fixed)
    h.ClaudeHome(patch, Path(spec["claude_home"]))
    h.UsageEndpoint(patch, Path(spec["calls_file"]), spec["endpoint"])

    budgets = budget.read_all({"claude": None}, None, Path(spec["config_dir"]),
                              Path(spec["project_config"]), {}, {}, use_cache=False)
    print(json.dumps(budgets["claude"].to_dict()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
