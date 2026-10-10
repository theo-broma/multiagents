"""PS2-R7: compare every T1 id with and without scheduler-enabled cwd config.

This is deliberately one integration test: running the existing behavioural
suite is the subject, so both real pytest invocations are necessary.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

import pytest

REPO = Path(__file__).resolve().parents[1]
# T1 contains real provider processes and deliberate cooldown tests.
SUITE_BOUND = 90
T1 = ["test_t1_deferred_queue.py", "test_t1_amendments.py", "test_t1_review_fixes.py"]


def outcomes(report):
    result = {}
    for case in ET.parse(report).iter("testcase"):
        key = (case.attrib["classname"], case.attrib["name"])
        result[key] = next((kind for kind in ("failure", "error", "skipped")
                            if case.find(kind) is not None), "passed")
    assert result, "nested pytest must collect the T1 cases"
    return result


@pytest.mark.slow
def test_ps2_r7_t1_per_id_outcomes_ignore_scheduler_enabled_start_directory(tmp_path):
    observed = []
    for scheduler in (False, True):
        cwd = tmp_path / ("enabled" if scheduler else "clean")
        cwd.mkdir()
        if scheduler:
            cfg = cwd / ".multiagents/config"
            cfg.mkdir(parents=True)
            (cfg / "project.yaml").write_text("scheduler:\n  enabled: true\n")
        report = tmp_path / f"report-{scheduler}.xml"
        basetemp = Path(tempfile.mkdtemp(prefix="pt-", dir="/var/tmp"))
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-n", "4",
                 "--basetemp=" + str(basetemp), "--junitxml=" + str(report),
                 *[str(REPO / "tests" / name) for name in T1]], cwd=cwd,
                env={**os.environ, "PYTHONPATH": str(REPO / "src")},
                capture_output=True, text=True, timeout=SUITE_BOUND)
            assert result.returncode in (0, 1), result.stdout + result.stderr
            observed.append(outcomes(report))
        finally:
            shutil.rmtree(basetemp, ignore_errors=True)
    assert observed[0].keys() == observed[1].keys(), "ambient config changed collection"
    differences = {key: (value, observed[1].get(key)) for key, value in observed[0].items()
                   if value != observed[1].get(key)}
    assert not differences, f"ambient scheduler config changes outcomes: {differences}"
