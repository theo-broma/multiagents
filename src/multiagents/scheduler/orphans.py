"""Host-only discovery and explicit removal of attempt resources."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import shutil
from datetime import datetime

from ..executor import get_executor
from ..executor.isolated import outcome
from ..executor.lifecycle import Absent, Created, Exited, ResourceIdentity, Running
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
            if labels.get("multiagents.gateway"):
                continue
            attempt = recorded.get(identity.id)
            if attempt and not nodes.get(attempt["node_id"], {}).get("disposed"):
                continue
            reason = "discarded_node" if attempt else "unrecorded_attempt"
            found[identity.id] = {"resource_kind": "container", "resource_id": identity.id,
                "container_id": identity.id,
                "name": metadata["Name"].lstrip("/"), "labels": labels,
                "created_at": metadata["Created"], "project": paths.slug,
                "attempt_id": attempt["attempt_id"] if attempt else None, "reason": reason}
            owners[identity.id] = target
    return {"orphans": [found[key] for key in sorted(found)], "orphans_error": None}, owners, targets


async def _network_metadata(target, identity):
    result = await target._cli("network", "inspect", identity)
    if result[0]:
        # Docker uses a different missing-object diagnostic for networks.
        diagnostic = result[2].lower()
        missing = ("network " + identity + " not found", "no such network: " + identity,
                   "no such object: " + identity)
        if any(message.encode() in diagnostic for message in missing):
            return Absent()
        raise ValueError("network metadata unavailable")
    try:
        values = json.loads(result[1])
        if not isinstance(values, list) or len(values) != 1:
            raise ValueError
        value = values[0]
        if (value["Id"] != identity or not isinstance(value["Name"], str)
                or not isinstance(value["Labels"], dict)
                or not isinstance(value["Containers"], dict)):
            raise ValueError
        stamp = datetime.fromisoformat(value["Created"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError
        return value
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError("network metadata is invalid") from None


def _network_attempt(metadata, journal):
    """Reconcile both durable identifiers, including a create before id commit."""
    matches = []
    labels = metadata["Labels"]
    for attempt in journal:
        network = attempt.get("launch_evidence", {}).get("executor", {}).get("network") or {}
        by_id = network.get("id") == metadata["Id"]
        by_key = (bool(network.get("creation_key"))
                  and network["creation_key"] == labels.get("multiagents.creation_key"))
        if not (by_id or by_key):
            continue
        if (network.get("id") and not by_id
                or labels.get("multiagents.owner") != attempt["attempt_id"]
                or network.get("creation_key") != labels.get("multiagents.creation_key")):
            raise ValueError("network identity conflicts with the host journal")
        matches.append(attempt)
    if len(matches) > 1:
        raise ValueError("network has ambiguous host ownership")
    return matches[0] if matches else None


async def _ended_network_attempt(target, attempt):
    if attempt["state"] not in {"recorded", "abandoned"}:
        return False
    evidence = attempt.get("launch_evidence", {}).get("executor", {})
    identity = ResourceIdentity(evidence["identity"]) if evidence.get("identity") else Absent()
    if evidence.get("creation_key"):
        resolved = await outcome(target.find, evidence["creation_key"])
        if not isinstance(resolved, (ResourceIdentity, Absent)):
            raise ValueError("network owner identity is uncertain")
        if isinstance(resolved, ResourceIdentity):
            if isinstance(identity, ResourceIdentity) and identity != resolved:
                raise ValueError("network owner identity conflicts with the host journal")
            identity = resolved
    if isinstance(identity, Absent):
        return True
    state = await outcome(target.inspect, identity)
    if isinstance(state, (Created, Exited, Absent)):
        return True
    if isinstance(state, Running):
        return False
    raise ValueError("network owner liveness is uncertain")


async def _network_orphans(paths, targets, journal, nodes):
    rows, owners = {}, {}
    for target in targets:
        if not callable(getattr(target, "_cli", None)):
            continue
        result = await target._cli("network", "ls", "-q", "--no-trunc", "--filter",
                                   "label=multiagents.project=" + paths.slug)
        if result[0]:
            raise ValueError("network discovery failed")
        identities = result[1].decode(errors="replace").split()
        if any(not re.fullmatch(r"[0-9a-f]{64}", identity) for identity in identities):
            raise ValueError("network discovery returned invalid identities")
        keys = {}
        for identity in identities:
            metadata = await _network_metadata(target, identity)
            if isinstance(metadata, Absent):
                continue
            labels = metadata["Labels"]
            if labels.get("multiagents.project") != paths.slug or labels.get("multiagents.gateway"):
                continue
            # Only the reserved attempt namespace belongs to this discovery.
            if not labels.get("multiagents.owner") or not labels.get("multiagents.creation_key"):
                continue
            key = labels["multiagents.creation_key"]
            attempt = _network_attempt(metadata, journal)
            if attempt and key in keys and keys[key] != identity:
                raise ValueError("multiple networks have the same creation key")
            keys[key] = identity
            if attempt and not await _ended_network_attempt(target, attempt):
                continue
            reason = "unrecorded_attempt"
            if attempt:
                reason = ("discarded_node" if nodes.get(attempt["node_id"], {}).get("disposed")
                          else "ended_attempt_network")
            rows[identity] = {"resource_kind": "network", "resource_id": identity,
                "network_id": identity, "container_id": None, "name": metadata["Name"],
                "labels": labels, "created_at": metadata["Created"], "project": paths.slug,
                "attempt_id": attempt["attempt_id"] if attempt else None, "reason": reason}
            owners[identity] = (target, attempt)
    return list(rows.values()), owners


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
            rows.append({"resource_kind": "endpoint", "resource_id": entry.name,
                         "endpoint_id": entry.name, "reason": "orphan_endpoint",
                         "container_id": None, "name": None, "labels": None, "created_at": None,
                         "project": paths.slug, "attempt_id": None})
    return rows


def discover(paths, config, store, db=None):
    try:
        journal, nodes = _snapshot(store, db)
        async def operation():
            result, _, targets = await _discover(paths, config, journal, nodes)
            networks, _ = await _network_orphans(paths, targets, journal, nodes)
            result["orphans"].extend(networks)
            return result
        result = _run(operation())
        result["orphans"].extend(endpoint_orphans(paths, store, db))
        result["orphans"].sort(key=lambda row: (row["resource_kind"], row["resource_id"]))
        return result
    except Exception:
        return {"orphans": None,
                "orphans_error": {"reason": "orphan discovery could not be completed"}}


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


def remove_network(project_root, network_id):
    """Remove a rechecked orphan without stopping any of its members."""
    def failure(error, reason):
        return {"ok": False, "network_id": network_id, "error": error, "reason": reason}

    def success():
        return {"ok": True, "network_id": network_id, "state": "absent"}

    if os.environ.get("MULTIAGENTS_CONTAINER") or os.environ.get("MULTIAGENTS_AGENT_ID"):
        return failure("unknown", "orphan removal is available only to the host user")
    if not isinstance(network_id, str) or not re.fullmatch(r"[0-9a-f]{64}", network_id):
        return failure("invalid_id", "an exact full network id is required")
    from ..config import load
    paths = ProjectPaths(project_root)
    store = Store(project_root)
    try:
        config = load(paths, seed=False)

        async def operation():
            journal, nodes = _snapshot(store)
            targets, _ = _backends(paths, config, journal)
            targets = [target for target in targets if callable(getattr(target, "_cli", None))]
            if not targets:
                return failure("unknown", "no network discovery backend is available")
            present = []
            for target in targets:
                metadata = await _network_metadata(target, network_id)
                if isinstance(metadata, Absent):
                    continue
                if metadata["Labels"].get("multiagents.project") != paths.slug:
                    return failure("wrong_project", "network belongs to another project")
                present.append(target)
            if not present:
                return success()
            _, owners = await _network_orphans(paths, targets, journal, nodes)
            if network_id not in owners:
                return failure("not_orphan", "network is not a confirmed attempt orphan")
            target, attempt = owners[network_id]

            def authority():
                try:
                    with (paths.scheduler / "gateways" / "authority.json").open() as source:
                        return json.load(source)
                except FileNotFoundError:
                    return {}

            async def members(metadata, current_attempt):
                allowed = []
                gateway_id = (authority().get("gateway") or {}).get("id")
                for member in metadata["Containers"]:
                    if not re.fullmatch(r"[0-9a-f]{64}", member):
                        raise ValueError("network has invalid member identities")
                    value = await outcome(target.metadata, ResourceIdentity(member))
                    if not isinstance(value, dict):
                        raise ValueError("network member ownership is uncertain")
                    labels = value.get("Config", {}).get("Labels") or {}
                    if labels.get("multiagents.project") != paths.slug:
                        return None
                    if member == gateway_id and labels.get("multiagents.gateway"):
                        allowed.append(member)
                        continue
                    if not current_attempt or labels.get("multiagents.owner") != current_attempt["attempt_id"]:
                        return None
                    evidence = current_attempt.get("launch_evidence", {}).get("executor", {})
                    identity = evidence.get("identity")
                    if not identity and evidence.get("creation_key"):
                        resolved = await outcome(target.find, evidence["creation_key"])
                        identity = resolved.id if isinstance(resolved, ResourceIdentity) else None
                    if (identity != member or not isinstance(
                            await outcome(target.inspect, ResourceIdentity(member)), (Created, Exited))):
                        return None
                    allowed.append(member)
                return allowed

            metadata = await _network_metadata(target, network_id)
            if isinstance(metadata, Absent):
                return success()
            if await members(metadata, attempt) is None:
                return failure("not_orphan", "network contains an unexpected or live member")
            stale = [aid for aid, record in authority().get("attempts", {}).items()
                     if record.get("network_id") == network_id]
            if attempt and attempt["attempt_id"] not in stale:
                stale.append(attempt["attempt_id"])
            if stale:
                from .gateways import GatewayRegistry
                registry = GatewayRegistry(paths, config=config, providers=config.providers)
                for attempt_id in stale:
                    result = await registry.revoke(attempt_id)
                    if result is not None and not isinstance(result, Absent):
                        return failure("unknown", "network grant revocation is unconfirmed")

            # Re-read after revocation. Avoid holding a scheduler transaction
            # while waiting for the authority lock or a Docker command.
            journal, nodes = _snapshot(store)
            _, owners = await _network_orphans(paths, targets, journal, nodes)
            if network_id not in owners:
                if isinstance(await _network_metadata(target, network_id), Absent):
                    return success()
                return failure("not_orphan", "network is no longer a confirmed attempt orphan")
            target, attempt = owners[network_id]
            metadata = await _network_metadata(target, network_id)
            if isinstance(metadata, Absent):
                return success()
            allowed = await members(metadata, attempt)
            if allowed is None:
                return failure("not_orphan", "network contains an unexpected or live member")
            for member in allowed:
                await target._cli("network", "disconnect", network_id, member)
                current = await _network_metadata(target, network_id)
                if isinstance(current, Absent):
                    return success()
                if member in current["Containers"]:
                    return failure("unknown", "network member disconnection is unconfirmed")
            current = await _network_metadata(target, network_id)
            if isinstance(current, Absent):
                return success()
            if current["Containers"]:
                return failure("not_orphan", "network has unexpected members")
            await target._cli("network", "rm", network_id)
            if not isinstance(await _network_metadata(target, network_id), Absent):
                return failure("unknown", "network removal is unconfirmed")
            return success()

        return _run(operation())
    except Exception:
        # Daemon diagnostics can contain authority or credential material.
        return failure("unknown", "network ownership or daemon state could not be confirmed")


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
