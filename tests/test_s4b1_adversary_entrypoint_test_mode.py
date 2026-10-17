"""Adversary, slice 4B.1: the K4B.7 test mode is honoured in a root (production) start.

HOLE (severity LOW, privilege separation silently disabled):
the entrypoint (src/multiagents/executor/log_entrypoint.py) reads
`MULTIAGENTS_TEST_REDACTOR_UNPRIVILEGED` from PID 1's own environment, which in
a container is the IMAGE's environment: the host passes no `--env` to
`docker create` and `strip_run_environment` only cleans the invocation
environment. An image built with `ENV MULTIAGENTS_TEST_REDACTOR_UNPRIVILEGED=1`
therefore selects test mode in a production credential-carrying attempt. With
the container started as uid 0 and SETUID/SETGID/SETPCAP (K4B.7), `main()`
then skips the input owner switch and `ordinary()` runs the guest child
without any drop: the child is container root with those capabilities, shares
PID 1's UID, and can reach PID 1's raw Docker log descriptors. Redaction and
the separation both become advisory.

K4B.7: the test mode is "for local tests that run the extracted script" and
"If the separation cannot be established, production startup fails closed".
A root start is the production start; nothing in it should run the child
unseparated.

Root is simulated by reporting euid/uid 0 inside the entrypoint process only;
the child is a real, unprivileged process on this machine.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from multiagents.executor.log_entrypoint import ENTRYPOINT

PASSWORD = "c4" * 32
URL = f"http://ma1-att_0001:{PASSWORD}@ma-egress:8888"


def test_s4b1_adversary_a_root_start_with_the_test_variable_does_not_run_the_child_unseparated(tmp_path):
    inputs, cwd = tmp_path / "input", tmp_path / "cwd"
    inputs.mkdir()
    cwd.mkdir()
    marker = tmp_path / "child-ran"
    child = f"import os; open({str(marker)!r}, 'w').write(str(os.getuid()))"
    (inputs / "invocation.json").write_text(json.dumps({
        "argv": [sys.executable, "-c", child], "env": {"HTTP_PROXY": URL, "PATH": os.environ.get("PATH", "")},
        "cwd": str(cwd), "stdin": None, "output_prefix": "multiagents-output-" + "0" * 32 + ":",
        "redaction_secrets": [URL, PASSWORD], "uid": 1000, "gid": 1000}))
    script = ENTRYPOINT.replace("/multiagents-input", str(inputs))
    # PID 1 as Docker starts it under K4B.7: uid 0. Only the report is faked.
    prelude = "import os\nos.geteuid = lambda: 0\nos.getuid = lambda: 0\n"
    env = dict(os.environ, MULTIAGENTS_TEST_REDACTOR_UNPRIVILEGED="1")    # from the image's ENV
    proc = subprocess.run([sys.executable, "-c", prelude + script], capture_output=True, timeout=30,
                          cwd=cwd, env=env)
    assert not marker.exists(), (
        "a root (production) start honoured the test variable from its own environment and exec'd the "
        f"guest child with no privilege drop at all (exit {proc.returncode})")
    assert proc.returncode != 0
