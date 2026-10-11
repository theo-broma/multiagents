"""PS2-R7: every test starts in a directory with disposable project config."""
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def _starting_directory_is_disposable(tmp_path, monkeypatch):
    # Config-loading tests explicitly chdir to their prepared project. All
    # other tests must never inherit the invocation directory's config, so
    # the starting directory sits outside every project: no .multiagents
    # directory is found at or above it, whatever directory pytest was
    # started from. Its `src` symlink keeps the suite's repo-relative
    # reads (src/multiagents/...) resolving to the real source tree.
    start = tmp_path.parent / f".start-{tmp_path.name}"
    start.mkdir()
    (start / "src").symlink_to(REPO / "src")
    monkeypatch.chdir(start)
