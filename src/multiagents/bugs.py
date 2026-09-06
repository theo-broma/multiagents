"""Filing multiagents' own bugs.

An agent that hits a defect in the tooling has nowhere useful to put it: it
cannot reach the maintainers, and the orchestrator should not be improvising
GitHub calls mid-task. So the bug-reporter agent writes a ticket, the tree
queues it, and this module is the only thing that can make one leave the
machine.

Two rules shape everything here:

**Nothing is published without a decision.** ``automatic: false`` is the
default, and it means the ticket waits for the user however urgent it is. A bug
report is public writing about the user's environment; consent for one is not
consent for the next.

**What is stored is what would be sent.** Depersonalisation happens in
``Tree.add_ticket`` on the way in, not here on the way out, so the orchestrator
and the user review the same text that gets posted. A scrubber that ran at
submission time would mean nobody ever saw the real payload.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

SUBMIT_TIMEOUT = 60


def settings(config: Any) -> dict:
    """The `bug_reporting` block, with defaults applied."""
    raw = dict((config.project or {}).get("bug_reporting") or {})
    return {
        "enabled": bool(raw.get("enabled", True)),
        "automatic": bool(raw.get("automatic", False)),
        "repo": str(raw.get("repo", "") or ""),
        "labels": list(raw.get("labels") or []),
    }


def render(ticket: dict) -> str:
    """The issue body. Plain markdown, no front matter."""
    parts = [ticket.get("body", "").strip()]
    if ticket.get("proposed_fix", "").strip():
        parts += ["", "## Proposed fix", "", ticket["proposed_fix"].strip()]
    parts += ["", "---", f"Filed by the multiagents bug-reporter agent "
                        f"({ticket.get('severity', 'minor')})."]
    return "\n".join(parts).strip() + "\n"


def can_submit(config: Any) -> tuple[bool, str]:
    """Whether an issue can actually be created, and why not."""
    conf = settings(config)
    if not conf["enabled"]:
        return False, "bug reporting is disabled in project.yaml"
    if not conf["repo"]:
        return False, ("no upstream repo configured — set bug_reporting.repo in "
                       "project.yaml to file issues")
    if not shutil.which("gh"):
        return False, "the `gh` CLI is not installed, so an issue cannot be created"
    if not authenticated():
        return False, "`gh` is installed but not logged in — run `gh auth login`"
    return True, ""


def authenticated() -> bool:
    """Whether gh holds a token, without spending a round trip to check it.

    `gh auth token` reads the local store and exits non-zero when empty, where
    `gh auth status` calls the API — which would put a network request on the
    path of every `list_tickets`, and report a network outage as a login
    problem.
    """
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True,
                              timeout=10).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def submit(config: Any, ticket: dict) -> tuple[bool, str]:
    """Create the issue. Returns ``(ok, url_or_reason)``.

    The body goes in on stdin rather than as an argument: a ticket is long,
    contains newlines and backticks, and an argv round trip through a shell is
    exactly where quoting bugs turn into truncated or mangled reports.
    """
    ok, why = can_submit(config)
    if not ok:
        return False, why
    conf = settings(config)
    argv = ["gh", "issue", "create", "--repo", conf["repo"],
            "--title", ticket["title"], "--body-file", "-"]
    for label in conf["labels"]:
        argv += ["--label", label]
    try:
        result = subprocess.run(argv, input=render(ticket), capture_output=True,
                                text=True, timeout=SUBMIT_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()[:400]
    url = next((line.strip() for line in result.stdout.splitlines()
                if line.strip().startswith("http")), "")
    return True, url or result.stdout.strip()[:200]
