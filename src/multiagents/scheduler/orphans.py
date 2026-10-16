"""Host-only discovery and explicit removal of attempt containers."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import shutil

from ..executor import get_executor
from ..executor.isolated import outcome
from ..executor.lifecycle import Absent, Exited, ResourceIdentity
from ..paths import ProjectPaths
from .store import Store


def _run(operation):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(operation)
    # Git effects also replay inside Engine.reconcile's event loop. Their
    # synchronous callers wait for the backend before completing the intent.
    with ThreadPoolExecutor(max_workers=1) as worker:
        return worker.submit(asyncio.run, operation).result()


def backend(paths, config, evidence=None):
    executor = get_executor("isolated", config.project.get("executor", {}).get("isolated", {}), paths=paths)
    if evidence:
        executor.restore(evidence)
    if executor.backend is None or executor.problem:
        raise ValueError(executor.problem or "recorded backend unavailable")
    return executor.backend


def _snapshot(store, db=None):
    if db is None:
        with store.transaction(write=False) as connection:
            return _snapshot(store, connection)
    journal = [json.loads(raw) for (raw,) in db.execute(
        "SELECT record FROM attempts "
        "WHERE json_extract(record, '$.launch_evidence.executor.kind')='isolated'")]
    return journal, store.some_nodes(db, (attempt["node_id"] for attempt in journal))


def _backends(paths, config, journal):
    # Containers outlive the setting that selected isolated execution.
    selected = [backend(paths, config)]
    bindings = []
    for attempt in journal:
        evidence = attempt.get("launch_evidence", {}).get("executor", {})
        if evidence.get("kind") != "isolated":
            continue
        target = backend(paths, config, evidence)
        bindings.append((attempt, evidence, target))
        selected.append(target)
    # Reconstructed adapters for the same daemon share a discovery pass.
    unique = {}
    for target in selected:
        if callable(getattr(target, "metadata", None)):
            key = (getattr(target, "name", ""), str(target.root), getattr(target, "cli", ""))
            unique[key] = target
    return list(unique.values()), bindings


async def _discover(paths, config, journal, nodes):
    targets, bindings = _backends(paths, config, journal)
    recorded = {}
    for attempt, evidence, target in bindings:
        identity = evidence.get("identity")
        if identity:
            recorded[identity] = attempt
        key = evidence.get("creation_key")
        if key:
            found = await outcome(target.find, key)
            if isinstance(found, ResourceIdentity):
                recorded[found.id] = attempt
            elif not isinstance(found, Absent):
                raise ValueError(getattr(found, "reason", "creation key reconciliation failed"))
    found = {}
    owners = {}
    for target in targets:
        identities = await outcome(target.list_owned, paths.slug)
        if not isinstance(identities, list):
            raise ValueError(getattr(identities, "reason", "container discovery failed"))
        for identity in identities:
            metadata = await outcome(target.metadata, identity)
            if isinstance(metadata, Absent):
                continue
            if not isinstance(metadata, dict):
                raise ValueError(getattr(metadata, "reason", "container metadata unavailable"))
            labels = metadata.get("Config", {}).get("Labels") or {}
            if labels.get("multiagents.project") != paths.slug:
                continue
            attempt = recorded.get(identity.id)
            if attempt and not nodes.get(attempt["node_id"], {}).get("disposed"):
                continue
            reason = "discarded_node" if attempt else "unrecorded_attempt"
            found[identity.id] = {"container_id": identity.id,
                "name": metadata["Name"].lstrip("/"), "labels": labels,
                "created_at": metadata["Created"], "project": paths.slug,
                "attempt_id": attempt["attempt_id"] if attempt else None, "reason": reason}
            owners[identity.id] = target
    return {"orphans": [found[key] for key in sorted(found)], "orphans_error": None}, owners, targets


def endpoint_orphans(paths, store, db=None):
    if db is None:
        with store.transaction(write=False) as connection:
            return endpoint_orphans(paths, store, connection)
    directory = paths.scheduler / "endpoints"
    rows = []
    if directory.is_dir():
        for entry in sorted(directory.iterdir()):
            if (entry.is_symlink() or not entry.is_dir()
                    or db.execute("SELECT 1 FROM attempts WHERE id=?", (entry.name,)).fetchone()):
                continue
            rows.append({"endpoint_id": entry.name, "reason": "orphan_endpoint",
                         "container_id": None, "name": None, "labels": None, "created_at": None,
                         "project": paths.slug, "attempt_id": None})
    return rows


def discover(paths, config, store, db=None):
    try:
        journal, nodes = _snapshot(store, db)
        result = _run(_discover(paths, config, journal, nodes))[0]
        result["orphans"].extend(endpoint_orphans(paths, store, db))
        return result
    except Exception as exc:
        return {"orphans": None, "orphans_error": {"reason": str(exc) or type(exc).__name__}}


def remove(project_root, container_id):
    """Recheck host records and daemon ownership; never accept a label as a path."""
    def failure(error, reason):
        return {"ok": False, "container_id": container_id, "error": error, "reason": reason}

    if os.environ.get("MULTIAGENTS_CONTAINER") or os.environ.get("MULTIAGENTS_AGENT_ID"):
        return failure("unknown", "orphan removal is available only to the host user")
    from .endpoints import ENDPOINT_ID
    paths = ProjectPaths(project_root)
    plain_name = (isinstance(container_id, str) and container_id not in {"", ".", ".."}
                  and "/" not in container_id and "\x00" not in container_id)
    if plain_name and (ENDPOINT_ID.fullmatch(container_id)
                       or (paths.scheduler / "endpoints" / container_id).is_dir()):
        store = Store(project_root)
        try:
            # An exact recorded id is never an orphan, even after it ends.
            with store.transaction() as db:
                if db.execute("SELECT 1 FROM attempts WHERE id=?", (container_id,)).fetchone():
                    return failure("not_orphan", "endpoint belongs to a recorded attempt")
                directory = paths.scheduler / "endpoints" / container_id
                if directory.is_symlink():
                    return failure("invalid_id", "endpoint directory cannot be a symlink")
                if directory.exists():
                    shutil.rmtree(directory)
                return {"ok": True, "endpoint_id": container_id, "state": "absent"}
        except Exception as exc:
            return failure("unknown", str(exc) or type(exc).__name__)
    if not isinstance(container_id, str) or not re.fullmatch(r"[0-9a-f]{64}", container_id):
        return failure("invalid_id", "an exact full container id is required")
    from ..config import load
    paths = ProjectPaths(project_root)
    store = Store(project_root)
    try:
        config = load(paths, seed=False)
        async def operation():
            targets, _ = _backends(paths, config, journal)
            if not targets:
                return failure("unknown", "no container discovery backend is available")
            identity = ResourceIdentity(container_id)
            present = False
            for target in targets:
                metadata = await outcome(target.metadata, identity)
                if isinstance(metadata, Absent):
                    continue
                if not isinstance(metadata, dict):
                    return failure("unknown", getattr(metadata, "reason", "container metadata unavailable"))
                present = True
                if (metadata.get("Config", {}).get("Labels") or {}).get("multiagents.project") != paths.slug:
                    return failure("wrong_project", "container belongs to another project")
            if not present:
                return {"ok": True, "container_id": container_id, "state": "absent"}
            _, owners, _ = await _discover(paths, config, journal, nodes)
            target = owners.get(container_id)
            if target is None:
                return failure("not_orphan", "container belongs to a recorded attempt")
            stopped = await outcome(target.stop, identity, 3.0)
            if not isinstance(stopped, (Exited, Absent)):
                return failure("unknown", getattr(stopped, "reason", "container stop is unconfirmed"))
            removed = await outcome(target.remove, identity, False)
            if not isinstance(removed, Absent):
                return failure("unknown", getattr(removed, "reason", "container removal is unconfirmed"))
            return {"ok": True, "container_id": container_id, "state": "absent"}
        # Keep the authority snapshot through deletion. A concurrent launch
        # cannot commit its creation key and then create outside this view.
        with store.transaction(write=False) as db:
            journal, nodes = _snapshot(store, db)
            return _run(operation())
    except Exception as exc:
        return failure("unknown", str(exc) or type(exc).__name__)


def discard(paths, config, journal):
    """Dispose every recorded resource before declaring its node clean."""
    journal = [a for a in journal if a.get("launch_evidence", {}).get("executor", {}).get("kind") == "isolated"]
    if not journal:
        return
    async def operation():
        for attempt in journal:
            evidence = attempt.get("launch_evidence", {}).get("executor", {})
            if evidence.get("kind") != "isolated":
                continue
            target = backend(paths, config, evidence)
            key = evidence.get("creation_key")
            identity = ResourceIdentity(evidence["identity"]) if evidence.get("identity") else Absent()
            if key:
                resolved = await outcome(target.find, key)
                if not isinstance(resolved, (ResourceIdentity, Absent)):
                    raise ValueError(getattr(resolved, "reason", "resource identity unavailable"))
                if isinstance(resolved, ResourceIdentity):
                    if isinstance(identity, ResourceIdentity) and resolved != identity:
                        raise ValueError("creation key differs from the recorded resource identity")
                    identity = resolved
            if isinstance(identity, ResourceIdentity):
                result = await outcome(target.remove, identity, False)
                if not isinstance(result, Absent):
                    raise ValueError(getattr(result, "reason", "resource removal is unconfirmed"))
    _run(operation())
