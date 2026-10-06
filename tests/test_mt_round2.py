"""MT round 2: a stored token the monitor cannot write is a refusal, not a crash.

Regression test for the defect reviewer ag-d9d5d9 found in
src/multiagents/monitor/server.py, in `serve`: the `--persistent-token` branch
handled `TokenRefused` but not the `OSError` that `store_token` raises when it
cannot put the token where it belongs — the token path being a directory, or the
state directory one the monitor may not write. The monitor died with a traceback
where `--rotate-token` prints a message and exits non-zero, which is what MT-R4
asks for: a stored token that cannot be used is a refusal that says why and never
silently replaces the token.

Black box like tests/test_mt_monitor_tailnet.py, and using its harness: the
project, the state home and the launcher come from there rather than being built
again.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from test_mt_monitor_tailnet import (  # noqa: E402, F401 - fixtures and helpers
    page, stored, world,
)


def token_path_is_a_directory(stored_token):
    """The path the token would be stored in is a directory instead."""
    path, _ = stored_token
    path.unlink()
    path.mkdir()
    return path


def test_mt_round2_r4_persistent_token_refuses_when_the_token_path_is_a_directory(world, stored):
    """The token path being a directory is an OSError, not a crash (MT-R4).

    `--rotate-token` has always refused this cleanly. `--persistent-token`
    reaches the same `store_token` — on a first start, or when the file is not
    there — and used to let the `OSError` out of `serve`.
    """
    path = token_path_is_a_directory(stored)

    run = world.start("--persistent-token", wait=False)
    code = run.wait_exit()
    out = run.output()

    assert code != 0
    assert "Traceback" not in out, f"the monitor crashed rather than refusing:\n{out}"
    assert str(path) in out, f"the refusal does not say which path it could not use:\n{out}"
    assert not run.url(), "a refused start must not serve, and prints no token"
    # never silently replaced
    assert path.is_dir() and list(path.iterdir()) == []


def test_mt_round2_r4_the_refusal_reaches_stderr_only(world, stored):
    """The reason goes to stderr; stdout stays empty, so nothing scrapes it."""
    token_path_is_a_directory(stored)

    run = world.start("--persistent-token", wait=False)
    run.wait_exit()

    assert run.stderr().strip(), "the refusal has to say something"
    assert run.stdout() == "", run.stdout()


def test_mt_round2_r4_a_plain_start_is_unaffected_by_a_directory_token_path(world, stored):
    """MT-R4: a start without the flag ignores the stored path entirely.

    The refusal belongs to `--persistent-token` alone, so this guards the other
    side of the same change: the flag is what stops the monitor, not the state
    directory being in the way.
    """
    token_path_is_a_directory(stored)

    run = world.start()
    assert page(run, run.token)[0] == 200


@pytest.mark.parametrize("flag", ["--persistent-token", "--rotate-token"])
def test_mt_round2_r4_both_flags_refuse_the_same_way(world, stored, flag):
    """One behaviour, whichever flag asks for a stored token."""
    token_path_is_a_directory(stored)

    run = world.start(flag, wait=False)
    code = run.wait_exit()
    out = run.output()

    assert code != 0
    assert "Traceback" not in out, out
    assert not run.url()
