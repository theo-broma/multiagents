"""Keep the suite off the developer's machine.

`state_root()` and `global_config_dir()` are machine-wide by design — one
`~/.multiagents` holding every project's worktrees and agent homes, one
`~/.config/multiagents` holding the editable defaults. Tests build a
`ProjectPaths` over a `tmp_path`, which makes the *slug* per-test but leaves
the *root* pointing at the real home, so `ProjectPaths.ensure()` has been
doing `mkdir` under it on nearly every test, and `register_project()` has been
appending to the real `projects.json`. Measured before this file existed:

    ~/.multiagents/worktrees        14,231 dirs, 13,886 written by tests
    ~/.multiagents/homes            14,232 dirs, 13,887 written by tests
    ~/.config/multiagents/projects.json   2,167 entries, 2,165 naming
                                          /tmp/pytest-of-*/ paths since deleted

That is the visible half. The half that cost time is that it makes the suite
non-idempotent: pytest recycles its `pytest-of-<user>/pytest-N` directory
names, the slug hashes the absolute path, so a later run collides with its own
leftovers and `git worktree add` fails with "already exists". A refactor is
then indistinguishable from a stale directory, which is exactly the wrong
thing to be guessing about while changing code the suite is supposed to be
guarding.

Both redirects already existed and about seven tests set the state one
themselves; nothing set them globally because there was no conftest.

The root is deliberately NOT under `tmp_path`. Most tests use `tmp_path` as the
project directory and some commit it, so a state root nested inside it would
sweep worktrees and agent homes into the repository under test.

What this does NOT cover, checked rather than assumed. `budget.py` binds
`CLAUDE_STATE` and `CLAUDE_CREDENTIALS` from `Path.home()` at import time, so
no environment variable set in a fixture can move them — an advisor proposed
redirecting `HOME` for exactly this and it would not have worked. Auditing a
full run by wrapping `open()` and recording every path under the real home
found none: the only hit was the package's own shipped `opencode.sh`, matched
on its name. So nothing reads the developer's billing state today, and the
`HOME` redirect is not worth its blast radius. A test that called the budget
readers unmocked would reintroduce it, and would have to mock them instead.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _machine_state_is_disposable(tmp_path_factory, monkeypatch):
    """Point the machine-wide roots at a directory belonging to this test.

    `mktemp` numbers each call, so this is per-test rather than per-session.
    Per-test on purpose: a shared root lets one test read what another wrote
    and makes the suite's result depend on collection order. Cross-project
    behaviour belongs in a test that creates two projects in one root and says
    so, not in accidental bleed between unrelated tests.
    """
    root = tmp_path_factory.mktemp("machine")
    monkeypatch.setenv("MULTIAGENTS_STATE_DIR", str(root / "state"))
    monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(root / "config"))
