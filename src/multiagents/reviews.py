"""Exact standalone review inputs and advisory verdicts (RM-R2/R3)."""
from __future__ import annotations

import uuid

from . import gitops


def validate_verdict(verdict, findings):
    """The same vocabulary and finding shape used by loop verdicts."""
    if not isinstance(verdict, str) or verdict not in {"approved", "rejected"}:
        raise ValueError("invalid verdict: expected approved or rejected")
    if not isinstance(findings, list) or any(
            not isinstance(f, dict) or not isinstance(f.get("summary"), str)
            or not isinstance(f.get("severity"), str) for f in findings):
        raise ValueError("invalid findings: expected summary and severity records")


def resolve(runner, verifies, *, caller=None, visible_nodes=None, db=None):
    """Resolve once through host operands; never substitute the base branch."""
    if not isinstance(verifies, str) or not verifies.startswith("ag-"):
        raise ValueError(f"{verifies} is not a run id; use the node's ag-* run ids")
    run = runner.tree.get(verifies)
    if run is None:
        raise ValueError(f"unknown review target {verifies}")
    if caller and verifies != caller:
        if run.node_id and visible_nodes is not None:
            visible = run.node_id in visible_nodes
        else:
            visible = caller in runner.tree.ancestry(verifies)
        if not visible:
            raise PermissionError(f"review target {verifies} is not visible to {caller}")
    record = runner.authority.get(verifies) if runner.authority else None
    completion = (record or {}).get("completion") or {}
    if run.status == "discarded" or completion.get("status") == "discarded":
        raise ValueError(f"review target {verifies} is disposed/discarded")
    if not record and not run.branch:
        raise ValueError(f"review target {verifies} has no host-recorded branch or captured commit")
    branch = (record or {}).get("branch") or run.branch
    commit = completion.get("commit")
    if run.node_id:
        from .scheduler.engine import attempts
        from .scheduler.store import Store, matching_records
        from .scheduler.results import Results

        def captured(connection):
            plan_node = Store(runner.paths.root).nodes(connection).get(run.node_id)
            if plan_node and plan_node.get("disposed"):
                raise ValueError(f"review target {verifies} is disposed")
            journal = attempts(connection)
            attempt = next((a for a in matching_records(journal, "run_id", verifies)
                            if a["run_id"] == verifies), None)
            if not attempt:
                raise ValueError(f"review target {verifies} has no capture authority")
            result = attempt.get("result") or {}
            if result.get("commit"):
                return result["commit"]
            if not branch:
                raise ValueError(f"review target {verifies} has no branch or captured commit")
            # A running target needs a snapshot, not its eventual completion.
            # Separate retention identity leaves normal capture replay intact.
            snapshot = {**attempt, "attempt_id": "review-" + uuid.uuid4().hex}
            return Results(runner.paths, runner.config).capture(run, snapshot, runner.authority)["commit"]

        if db is not None:
            commit = captured(db)
        else:
            with Store(runner.paths.root).transaction(write=False) as connection:
                commit = captured(connection)
    elif branch:
        # The host-named branch tip is current, even for a finished legacy run.
        with gitops._host_scope(runner.paths.root):
            commit = gitops.run(runner.paths.root, "rev-parse", "--verify",
                                "refs/heads/" + branch + "^{commit}", check=True).out.strip()
    if not commit:
        raise ValueError(f"review target {verifies} has no branch or captured commit")
    with gitops._host_scope(runner.paths.root):
        resolved = gitops.run(runner.paths.root, "rev-parse", "--verify", commit + "^{commit}",
                              check=True).out.strip()
    if not resolved:
        raise ValueError(f"cannot resolve review target {verifies}'s commit")
    return resolved


def give(runner, run_id, verdict, findings):
    record = runner.authority.get(run_id) if runner.authority and run_id else None
    if not record or record.get("seeded") or not record.get("review"):
        return {"error": "forbidden"}
    try:
        validate_verdict(verdict, findings)
    except ValueError as exc:
        return {"error": "invalid", "detail": str(exc)}
    return runner.authority.review_verdict(run_id, verdict, findings)


def collected(runner, run_id):
    record = runner.authority.get(run_id) if runner.authority else None
    receipt = (record or {}).get("review_verdict")
    return {"review_verdict": receipt} if receipt else {}
