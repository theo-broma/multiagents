"""Host-issued network and gateway authority, separate from RPC capabilities.

The journal owns attempt identity. Private receipts own Docker identity; labels
only recover a create whose response was lost. Nothing in a guest request can
create or select an authority record.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import fcntl
import hashlib
import importlib
import inspect
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
from types import MappingProxyType
import uuid

from ..executor.docker import DockerExecutor
from ..executor.lifecycle import (Absent, Exited, IdentityConflict, InvalidSpec,
                                 ResourceIdentity, Running, Unknown, Unsupported,
                                 _read, _write)
from .egress import serve_egress
from .auth_gateway import serve_auth
from .store import Store, private_directory

_ID = re.compile(r"[0-9a-f]{64}\Z")


@dataclasses.dataclass(frozen=True)
class NetworkHandle:
    id: str


@dataclasses.dataclass(frozen=True)
class GatewayGrant:
    id: str
    environment: object = dataclasses.field(repr=False)


def redaction_values(environment):
    """Literals needed by the trusted logging producer, never public evidence."""
    from urllib.parse import urlsplit
    values = set()
    bearer = environment.get("ANTHROPIC_AUTH_TOKEN")
    if bearer:
        values.update((bearer, "Bearer " + bearer))
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        url = environment.get(name)
        if not url:
            continue
        parsed = urlsplit(url)
        if parsed.password:
            values.update((url, parsed.password,
                           base64.b64encode(f"{parsed.username}:{parsed.password}".encode()).decode()))
    return sorted(values, key=lambda value: (-len(value), value))


class GatewayRegistry:
    def __init__(self, paths, *, config, providers, readiness_probe=None, service="egress"):
        from ..executor.docker_lifecycle import DockerBackend
        if service not in {"egress", "auth"}:
            raise ValueError("unknown gateway service")
        self.service = service
        self.paths, self.config, self.providers = paths, config, providers
        if any(isinstance(provider, dict) for provider in providers.values()):
            from ..providers import load_providers
            self.providers = load_providers(providers, getattr(config, "provider_sources", None))
        self.root = paths.scheduler / "gateways"
        if service == "auth":
            self.root /= "auth"
        project = config.project if hasattr(config, "project") else config
        self.settings = dict(project.get("executor", {}).get("isolated", {}))
        docker = self.settings.get("docker") or {}
        self.image = docker.get("gateway_image", "multiagents/workspace:latest")
        self.backend = DockerBackend(paths.data / "isolated", cli=docker.get("cli", "docker"), image=self.image)
        self.allowlist, self.diagnostics = DockerExecutor(project.get("executor", {}).get("docker", {})).allowlist_entries()
        files = [Path(__file__), Path(__file__).with_name("egress.py")]
        if service == "auth":
            files += [Path(__file__).with_name("auth_gateway.py"), Path(__file__).parent.parent / "auth_upstream.py"]
        self.policy_digest = hashlib.sha256(json.dumps({"allowlist": self.allowlist, "image": self.image,
            "service": service, "code": [hashlib.sha256(p.read_bytes()).hexdigest() for p in files]},
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.test_backend = os.environ.get("MULTIAGENTS_TEST_GATEWAY_BACKEND") if service == "auth" else None
        self.test_backend_file = None
        self.readiness_probe = readiness_probe
        target = os.environ.get("MULTIAGENTS_TEST_GATEWAY_PROBE")
        if readiness_probe is None and target:
            module, factory = target.split(":", 1)
            self.readiness_probe = getattr(importlib.import_module(module), factory)()

    @contextlib.asynccontextmanager
    async def _locked(self):
        private_directory(self.root)
        with (self.root / "lock").open("a") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(.01)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _load(self):
        try:
            return _read(self.root / "authority.json")
        except FileNotFoundError:
            return {"attempts": {}, "generation": 0, "gateway": None}

    def _save(self, state):
        _write(self.root / "authority.json", state)

    def _gateway_ids(self):
        ids = set()
        root = self.paths.scheduler / "gateways"
        for path in (root / "authority.json", root / "auth" / "authority.json"):
            try:
                receipt = _read(path).get("gateway") or {}
                if receipt.get("id"):
                    ids.add(receipt["id"])
            except FileNotFoundError:
                pass
        return ids

    def _uses_auth(self, attempt):
        provider = self.providers.get(attempt.get("provider"))
        return (provider is not None and getattr(provider, "gateway_auth", "none") == "anthropic"
                and provider.needs.get("network"))

    def _check_backend(self):
        if self.test_backend:
            module, factory = self.test_backend.split(":", 1)
            loaded = importlib.import_module(module)
            path = Path(loaded.__file__).resolve(strict=True)
            adapter = getattr(loaded, factory)(self.root, project=self.paths.slug)
            if not callable(getattr(adapter, "open", None)):
                raise ValueError("gateway backend has no open operation")
            self.test_backend_file = path

    def _journal(self, attempt_id):
        with Store(self.paths.root).transaction(write=False) as db:
            row = db.execute("SELECT record FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def _evidence(self, attempt_id, record):
        from .engine import save_attempt
        with Store(self.paths.root).transaction() as db:
            row = db.execute("SELECT record FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row:
                attempt = json.loads(row[0])
                attempt.setdefault("launch_evidence", {}).setdefault("executor", {})["network"] = dict(record)
                save_attempt(db, attempt)

    async def probe(self):
        if self.settings.get("network", "allowlist") == "none":
            return ["executor.isolated.network is none"]
        if os.getuid() == 0:
            return ["gateway logging requires a non-root host UID"]
        return await self.backend.probe()

    async def network_metadata(self, network_id):
        result = await self.backend._cli("network", "inspect", network_id)
        missing = result[2].decode(errors="replace").lower()
        if result[0] and _ID.fullmatch(network_id or "") and (
                "network " + network_id + " not found" in missing or "no such network: " + network_id in missing):
            return Absent()
        if result[0]:
            return Unknown("network inspection unavailable")
        try:
            rows = json.loads(result[1])
            if len(rows) != 1 or not _ID.fullmatch(rows[0]["Id"]) or not isinstance(rows[0].get("Containers"), dict):
                raise ValueError()
            return rows[0]
        except (ValueError, TypeError, KeyError):
            return Unknown("invalid network inspection")

    async def _find(self, key):
        result = await self.backend._cli("network", "ls", "-q", "--no-trunc", "--filter",
            "label=multiagents.creation_key=" + key)
        if result[0]:
            return Unknown("network discovery unavailable")
        ids = result[1].decode(errors="replace").split()
        if len(ids) > 1 or any(not _ID.fullmatch(value) for value in ids):
            return Unknown("ambiguous network creation key")
        return await self.network_metadata(ids[0]) if ids else Absent()

    def _owns_network(self, network, attempt_id, record):
        labels = network.get("Labels") or {}
        return (network.get("Internal") is True and labels.get("multiagents.project") == self.paths.slug
                and labels.get("multiagents.owner") == attempt_id
                and labels.get("multiagents.creation_key") == record["key"])

    async def prepare(self, attempt_id, run_id):
        attempt = self._journal(attempt_id)
        if not attempt:
            return InvalidSpec("attempt is outside the host journal")
        if self.service == "auth":
            if not self._uses_auth(attempt):
                return None
            egress = GatewayRegistry(self.paths, config=self.config, providers=self.providers,
                                     readiness_probe=self.readiness_probe)
            handle = await egress.prepare(attempt_id, run_id)
            if not isinstance(handle, NetworkHandle):
                return handle
            async with self._locked():
                state = self._load()
                if attempt_id not in state["attempts"]:
                    network = egress._load()["attempts"][attempt_id]
                    state["attempts"][attempt_id] = {key: network[key] for key in
                        ("handle", "key", "run_id", "provider", "project", "network_id")}
                    state["attempts"][attempt_id]["account"] = attempt.get("account", network.get("account", ""))
                    state["attempts"][attempt_id]["revoked"] = False
                    self._save(state)
            return handle
        async with self._locked():
            state = self._load()
            record = state["attempts"].get(attempt_id)
            if attempt.get("run_id") != run_id or (record and record["run_id"] != run_id):
                return IdentityConflict()
            if record and record.get("network_id"):
                network = await self.network_metadata(record["network_id"])
                if isinstance(network, dict) and self._owns_network(network, attempt_id, record):
                    return NetworkHandle(record["handle"])
                return network if isinstance(network, Unknown) else InvalidSpec("recorded network is absent or foreign")
            if attempt.get("ended_at") or attempt.get("cancel_requested") or attempt.get("state") not in {"claimed", "launched"}:
                return InvalidSpec("attempt is not eligible for a network")
            problems = await self.probe()
            if problems:
                return Unsupported("network: " + "; ".join(problems))
            if record is None:
                evidence = attempt.get("launch_evidence", {}).get("executor", {}).get("network") or {}
                record = {"handle": "nh-" + uuid.uuid4().hex,
                          "key": evidence.get("creation_key") or "network-" + uuid.uuid4().hex,
                          "run_id": run_id, "provider": attempt.get("provider", ""),
                          "account": attempt.get("account", ""),
                          "project": self.paths.slug, "revoked": False}
                state["attempts"][attempt_id] = record
                self._save(state)
            evidence = {"creation_key": record["key"]}
            self._evidence(attempt_id, evidence)
            network = await self._find(record["key"])
            if isinstance(network, Absent):
                args = ["network", "create", "--internal"]
                labels = {"multiagents.project": self.paths.slug, "multiagents.owner": attempt_id,
                          "multiagents.creation_key": record["key"]}
                # Preserve explicit operator test labels, never infer a test daemon.
                labels.update(attempt.get("launch_evidence", {}).get("executor", {}).get("network_labels", {}))
                for key, value in labels.items():
                    args += ["--label", key + "=" + value]
                args += ["multiagents-net-" + record["key"].removeprefix("network-")]
                result = await self.backend._cli(*args)
                network = await self._find(record["key"])
                if isinstance(network, Absent):
                    return Unknown("network creation failed" if result[0] else "created network cannot be confirmed")
            if not isinstance(network, dict):
                return network
            if not self._owns_network(network, attempt_id, record):
                return IdentityConflict()
            record["network_id"] = network["Id"]
            self._save(state)
            self._evidence(attempt_id, dict(evidence, id=network["Id"]))
            return NetworkHandle(record["handle"])

    def resolve(self, handle, owner):
        """Backend host adapter: a reference is not a Docker name or guest input."""
        if not isinstance(handle, str):
            return InvalidSpec("network_handle must be host-issued")
        state = self._load()
        record = state["attempts"].get(owner)
        if not record or record.get("handle") != handle or record.get("revoked") or not record.get("network_id"):
            return InvalidSpec("network_handle is outside the host authority")
        return record["network_id"]

    def _password(self, attempt_id, *, create=False):
        private_directory(self.root / "secrets")
        path = self.root / "secrets" / attempt_id
        if not path.exists() and create:
            with path.open("x") as output:
                output.write(("ma-auth-v1." if self.service == "auth" else "") + secrets.token_hex(32))
        return path.read_text()

    def _environment(self, attempt_id, password):
        if self.service == "auth":
            return {"ANTHROPIC_BASE_URL": "http://ma-auth:8930", "ANTHROPIC_AUTH_TOKEN": password}
        url = f"http://ma1-{attempt_id}:{password}@ma-egress:8888"
        return {**{name: url for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")},
                "NO_PROXY": "localhost,127.0.0.1,::1,ma-auth", "no_proxy": "localhost,127.0.0.1,::1,ma-auth"}

    async def _desired(self, state, *, preparing=None):
        desired = {}
        for attempt_id, record in state["attempts"].items():
            if not record.get("network_id") or record.get("revoked"):
                continue
            attempt = self._journal(attempt_id)
            if not attempt or attempt.get("ended_at") or attempt.get("cancel_requested"):
                continue
            if attempt_id != preparing:
                # The host-recorded, unrevoked grant is the authority (IC-C30):
                # a Created or briefly Unknown container keeps it, and only a
                # confirmed exit or removal withdraws it.
                if not record.get("container_id") or not record.get("grant_id"):
                    continue
                live = await self.backend.inspect(ResourceIdentity(record["container_id"]))
                if isinstance(live, (Absent, Exited)):
                    continue
            network = await self.network_metadata(record["network_id"])
            if isinstance(network, Unknown) and attempt_id != preparing:
                # Keep the grant on its recorded sources; attach nothing.
                desired[attempt_id] = None
                continue
            if not isinstance(network, dict) or not self._owns_network(network, attempt_id, record):
                if attempt_id == preparing:
                    return Unknown("attempt network unavailable")
                continue
            known = {record.get("container_id"), *self._gateway_ids()}
            if set(network["Containers"]) - known:
                if attempt_id == preparing:
                    return Unknown("unexpected network member")
                continue
            desired[attempt_id] = network
        return desired

    def _publish(self, state, desired):
        grants = {}
        forbidden = set(_host_addresses())
        recorded = False
        for attempt_id, network in desired.items():
            record = state["attempts"][attempt_id]
            if network is None:
                if record.get("grant_id") and not record.get("revoked"):
                    grants[record["grant_id"]] = self._published_grant(attempt_id, record, list(record.get("sources", [])))
                continue
            for entry in network.get("IPAM", {}).get("Config", []):
                if entry.get("Subnet"):
                    forbidden.add(entry["Subnet"])
                if entry.get("Gateway"):
                    forbidden.add(entry["Gateway"])
            sources = _sources(network, record)
            if sources and record.get("sources") != sources:
                record["sources"] = sources
                recorded = True
            if record.get("grant_id") and not record.get("revoked"):
                grants[record["grant_id"]] = self._published_grant(attempt_id, record, sources)
        if recorded:
            self._save(state)
        private_directory(self.root / "runtime")
        config = {"allowlist": self.allowlist, "grants": grants, "forbidden_networks": sorted(forbidden),
                  "generation": state["generation"], "policy_digest": self.policy_digest,
                  "project": self.paths.slug}
        _write(self.root / "runtime" / "policy.json", config)
        management = self.root / "runtime" / "management"
        if not management.exists():
            management.write_text(secrets.token_hex(32))

    def _published_grant(self, attempt_id, record, sources):
        if self.service == "auth":
            return {"verifier": record["verifier"], "networks": sources,
                    "provider": record["provider"], "run_id": record["run_id"]}
        return {"username": "ma1-" + attempt_id, "password": self._password(attempt_id), "networks": sources}

    async def _probe(self, receipt):
        if self.readiness_probe:
            result = self.readiness_probe(registry=self, receipt=dict(receipt),
                container_identity=ResourceIdentity(receipt["id"]),
                generation=receipt["generation"], policy_digest=receipt["policy_digest"],
                network_ids=tuple(receipt["networks"]))
            return bool(await result if inspect.isawaitable(result) else result)
        # Exec only in the trusted gateway; the management listener is loopback
        # and requires its separate host-issued credential.
        code = ("import http.client,json,pathlib; c=http.client.HTTPConnection('127.0.0.1',8889,timeout=2); "
                "c.request('GET','/ready',headers={'Authorization':'Bearer '+pathlib.Path('/authority/management').read_text()}); "
                "r=c.getresponse(); assert r.status==200; print(r.read().decode())")
        for _ in range(30):
            result = await self.backend._cli("exec", receipt["id"], "python3", "-c", code)
            if not result[0]:
                try:
                    ready = json.loads(result[1])
                    if (ready.get("generation") == receipt["generation"] and
                            ready.get("policy_digest") == receipt["policy_digest"]):
                        return True
                except (ValueError, AttributeError):
                    pass
            await asyncio.sleep(.05)
        return False

    async def _ensure_gateway(self, state, desired):
        receipt = state.get("gateway")
        metadata = await self.backend.metadata(ResourceIdentity(receipt["id"])) if receipt else Absent()
        if isinstance(metadata, Unknown):
            return Unknown("gateway inspection unavailable")
        replace = (not isinstance(metadata, dict) or not isinstance(self.backend._state_of(metadata), Running)
                   or receipt.get("policy_digest") != self.policy_digest
                   or metadata["State"].get("StartedAt") != receipt.get("started_at"))
        # Inspect every desired network before attaching or replacing anything.
        known = {r.get("container_id") for r in state["attempts"].values()}
        known.update(self._gateway_ids())
        if receipt:
            known.add(receipt["id"])
        attached = [n for n in desired.values() if n is not None]
        for network in attached:
            if set(network["Containers"]) - known:
                return Unknown("unexpected network member")
        state["ready"] = False
        state["desired_networks"] = sorted(n["Id"] for n in attached)
        self._save(state)
        if replace:
            if isinstance(metadata, dict):
                stopped = await self.backend._cli("stop", "--time", "1", receipt["id"])
                if stopped[0]:
                    return Unknown("gateway stop failed")
                removed = await self.backend._cli("rm", receipt["id"])
                if removed[0]:
                    return Unknown("gateway removal failed")
            private_directory(self.root / "code")
            import shutil
            shutil.copyfile(Path(__file__).with_name("egress.py"), self.root / "code" / "egress.py")
            if self.service == "auth":
                shutil.copyfile(Path(__file__).with_name("auth_gateway.py"), self.root / "code" / "auth_gateway.py")
                shutil.copyfile(Path(__file__).parent.parent / "auth_upstream.py", self.root / "code" / "auth_upstream.py")
            (self.root / "code" / "main.py").write_text(_AUTH_GATEWAY_MAIN if self.service == "auth" else _GATEWAY_MAIN)
            state["generation"] += 1
            state["gateway"] = None
            self._save(state)
            self._publish(state, desired)
            key = "gateway-" + uuid.uuid4().hex
            state["gateway_key"] = key
            self._save(state)
            args = ["create", "--name", "multiagents-" + self.service + "-" + uuid.uuid4().hex,
                "--pull", "never", "--restart", "unless-stopped", "--cap-drop", "ALL",
                "--user", f"{os.getuid()}:{os.getgid()}",
                "--security-opt", "no-new-privileges=true", "--sysctl", "net.ipv4.ip_forward=0",
                "--label", "multiagents.project=" + self.paths.slug,
                "--label", "multiagents.gateway=" + self.service, "--label", "multiagents.creation_key=" + key,
                "--mount", f"type=bind,source={self.root / 'code'},target=/gateway,readonly",
                "--mount", f"type=bind,source={self.root / 'runtime'},target=/authority,readonly"]
            if self.test_backend:
                if self.test_backend_file is None:
                    try:
                        self._check_backend()
                    except Exception:
                        return Unknown("auth gateway backend factory failed")
                args += ["--mount", f"type=bind,source={self.test_backend_file},target=/gateway-backend.py,readonly",
                         "--env", "MULTIAGENTS_TEST_GATEWAY_BACKEND=" + self.test_backend]
            args += ["--entrypoint", "python3", self.image, "/gateway/main.py"]
            result = await self.backend._cli(*args)
            found = await self.backend.find(key)
            if not isinstance(found, ResourceIdentity):
                return Unknown("gateway creation failed")
            receipt = {"id": found.id, "policy_digest": self.policy_digest,
                       "generation": state["generation"], "networks": []}
            state["gateway"] = receipt
            self._save(state)
        else:
            self._publish(state, desired)
        if replace:
            result = await self.backend._cli("start", receipt["id"])
            if result[0]:
                return Unknown("gateway start failed")
        for network in attached:
            if receipt["id"] not in network["Containers"]:
                result = await self.backend._cli("network", "connect", "--alias", "ma-" + self.service, network["Id"], receipt["id"])
                checked = await self.network_metadata(network["Id"])
                if not isinstance(checked, dict) or receipt["id"] not in checked["Containers"]:
                    return Unknown("gateway attachment failed")
        metadata = await self.backend.metadata(ResourceIdentity(receipt["id"]))
        if not isinstance(metadata, dict) or not isinstance(self.backend._state_of(metadata), Running):
            return Unknown("gateway is not running")
        receipt.update(started_at=metadata["State"].get("StartedAt"),
                       generation=state["generation"], networks=state["desired_networks"])
        if not await self._probe(receipt):
            return Unknown("authenticated gateway readiness unavailable")
        state["ready"] = True
        self._save(state)
        from .engine import save_attempt
        with Store(self.paths.root).transaction() as db:
            for attempt_id in desired:
                row = db.execute("SELECT record FROM attempts WHERE id=?", (attempt_id,)).fetchone()
                if row:
                    attempt = json.loads(row[0])
                    gateways = attempt.setdefault("launch_evidence", {}).setdefault("executor", {}).setdefault("gateways", {})
                    gateways[self.service] = {"id": receipt["id"], "started_at": receipt["started_at"],
                        "policy_digest": receipt["policy_digest"], "gateway_generation": receipt["generation"],
                        "networks": list(receipt["networks"])}
                    save_attempt(db, attempt)
        return None

    async def grant(self, attempt_id, container_identity):
        attempt = self._journal(attempt_id)
        if not attempt:
            return InvalidSpec("attempt is outside the host journal")
        if self.service == "auth":
            if not self._uses_auth(attempt):
                return None
            try:
                self._check_backend()
            except Exception:
                return InvalidSpec("auth gateway backend factory failed")
            if attempt_id not in self._load()["attempts"]:
                prepared = await self.prepare(attempt_id, attempt["run_id"])
                if not isinstance(prepared, NetworkHandle):
                    return prepared
        async with self._locked():
            state = self._load()
            record = state["attempts"].get(attempt_id)
            if not record or record.get("revoked"):
                return InvalidSpec("network authority is absent or revoked")
            if not isinstance(container_identity, ResourceIdentity):
                return InvalidSpec("immutable container identity required")
            if record.get("container_id") and record["container_id"] != container_identity.id:
                return IdentityConflict()
            metadata = await self.backend.metadata(container_identity)
            if not isinstance(metadata, dict):
                return Unknown("container identity unavailable")
            labels = metadata.get("Config", {}).get("Labels", {})
            if labels.get("multiagents.owner") != attempt_id or labels.get("multiagents.project") != self.paths.slug:
                return IdentityConflict()
            if attempt.get("ended_at") or attempt.get("cancel_requested"):
                return InvalidSpec("attempt has ended")
            record["container_id"] = container_identity.id
            if not record.get("grant_id"):
                password = self._password(attempt_id, create=True)
                record.update(grant_id="gg-" + self.service + "-" + uuid.uuid4().hex,
                              verifier=hashlib.sha256(password.encode()).hexdigest(), service=self.service)
                state["generation"] += 1
                record["generation"] = state["generation"]
                self._save(state)
            desired = await self._desired(state, preparing=attempt_id)
            if isinstance(desired, Unknown):
                return desired
            if not desired.get(attempt_id) or not _sources(desired[attempt_id], record):
                # A grant bound to no source would only ever answer 407.
                return Unknown("attempt network address unavailable")
            result = await self._ensure_gateway(state, desired)
            if result is not None:
                return result
            network = {"creation_key": record["key"], "id": record["network_id"],
                "policy_digest": self.policy_digest, "gateway_generation": state["generation"]}
            if self.service == "egress":
                self._evidence(attempt_id, network)
            return GatewayGrant(record["grant_id"], MappingProxyType(self._environment(attempt_id, self._password(attempt_id))))

    async def revoke(self, attempt_id):
        async with self._locked():
            state = self._load()
            record = state["attempts"].get(attempt_id)
            if not record:
                return None
            if not record.get("revoked"):
                record["revoked"] = True
                state["generation"] += 1
                self._save(state)
            if not record.get("network_id"):
                found = await self._find(record["key"])
                if isinstance(found, Absent):
                    return None
                if not isinstance(found, dict) or not self._owns_network(found, attempt_id, record):
                    return Unknown("network recovery unavailable during revocation")
                record["network_id"] = found["Id"]
                self._save(state)
            # Disconnecting ingress proves enforcement even if the gateway
            # management path is down. Existing tunnels lose their interface.
            network = await self.network_metadata(record.get("network_id", ""))
            if isinstance(network, Absent):
                return None
            if not isinstance(network, dict):
                return Unknown("gateway revocation enforcement unknown")
            receipt = state.get("gateway") or {}
            if receipt.get("id") in network["Containers"]:
                await self.backend._cli("network", "disconnect", network["Id"], receipt["id"])
                network = await self.network_metadata(network["Id"])
                if not isinstance(network, dict) or receipt["id"] in network["Containers"]:
                    return Unknown("gateway revocation enforcement unknown")
            # Remove it from the next authenticated policy snapshot as well.
            policy_path = self.root / "runtime" / "policy.json"
            if policy_path.exists():
                policy = _read(policy_path)
                policy.get("grants", {}).pop(record.get("grant_id"), None)
                policy["generation"] = state["generation"]
                _write(policy_path, policy)
            return None

    async def cleanup(self, attempt_id):
        reply = await self.revoke(attempt_id)
        async with self._locked():
            state = self._load()
            record = state["attempts"].get(attempt_id)
            if record:
                record["cleanup"] = True
                self._save(state)
            # A revoked grant cannot be recovered. Its raw credential is no
            # longer needed, even if network deletion fails or already ran.
            (self.root / "secrets" / attempt_id).unlink(missing_ok=True)
            if isinstance(reply, Unknown):
                return reply
            if not record or not record.get("network_id"):
                return Absent()
            network = await self.network_metadata(record["network_id"])
            if isinstance(network, Absent):
                return None
            if not isinstance(network, dict):
                return network
            if not self._owns_network(network, attempt_id, record):
                return InvalidSpec("network ownership cannot be confirmed")
            known = {record.get("container_id"), *self._gateway_ids()}
            if set(network["Containers"]) - known:
                return Unknown("unexpected network member prevents cleanup")
            if record.get("container_id"):
                live = await self.backend.inspect(ResourceIdentity(record["container_id"]))
                from ..executor.lifecycle import Exited, Created
                if not isinstance(live, (Absent, Exited, Created)):
                    return Unknown("attempt termination cannot be confirmed")
            for member in network["Containers"]:
                result = await self.backend._cli("network", "disconnect", network["Id"], member)
                if result[0]:
                    return Unknown("network disconnect failed")
            result = await self.backend._cli("network", "rm", network["Id"])
            after = await self.network_metadata(network["Id"])
            if not isinstance(after, Absent):
                return Unknown("network removal unconfirmed")
            return None

    async def reconcile(self):
        async with self._locked():
            state = self._load()
            rows = []
            for attempt_id, record in state["attempts"].items():
                if not record.get("network_id"):
                    found = await self._find(record["key"])
                    if isinstance(found, dict) and self._owns_network(found, attempt_id, record):
                        record["network_id"] = found["Id"]
                        self._save(state)
                        self._evidence(attempt_id, {"creation_key": record["key"], "id": found["Id"]})
            desired = await self._desired(state)
            if isinstance(desired, Unknown):
                desired = {}
            if not desired:
                self._publish(state, {})
            # Readiness never treats a successful CLI call as authority.
            result = await self._ensure_gateway(state, desired) if desired else None
            for attempt_id, record in sorted(state["attempts"].items()):
                status, reason = "unknown", "authority or liveness unconfirmed"
                network = await self.network_metadata(record.get("network_id", ""))
                if isinstance(network, Absent):
                    status, reason = "absent", "network is absent"
                elif record.get("revoked"):
                    status, reason = "revoked", "authority is revoked"
                elif result is None and desired.get(attempt_id) is not None and state.get("ready"):
                    status, reason = "ready", "authenticated gateway ready"
                rows.append({"attempt_id": attempt_id, "state": status, "reason": reason})
            return rows


def _sources(network, record):
    """The addresses that may present the attempt's credential: its own network.

    Docker allocates an endpoint only when a container starts, so before
    `run` a created container has no address. The attempt network's subnets
    are known from creation, stay the same across the start (narrowing a grant
    later would cut its streams), and the network is internal with unexpected
    members withholding the grant. A member address serves only a network
    that reports no subnet.
    """
    sources = []
    for entry in (network.get("IPAM") or {}).get("Config") or []:
        if entry.get("Subnet"):
            sources.append(str(ipaddress.ip_network(entry["Subnet"], strict=False)))
    if not sources:
        member = network["Containers"].get(record.get("container_id"), {})
        for address in (member.get("IPv4Address"), member.get("IPv6Address")):
            if address:
                ip = ipaddress.ip_interface(address).ip
                sources.append(str(ip) + ("/32" if ip.version == 4 else "/128"))
    return sources


def _host_addresses():
    """Forbid host interfaces even when an operator uses globally routed IPs."""
    addresses = set()
    import struct
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for _, name in socket.if_nameindex():
            try:
                interface = struct.pack('256s', name.encode()[:15])
                value = fcntl.ioctl(sock.fileno(), 0x8915, interface)
                addresses.add(socket.inet_ntoa(value[20:24]))
            except OSError:
                pass
    try:
        for line in Path('/proc/net/if_inet6').read_text().splitlines():
            addresses.add(str(ipaddress.IPv6Address(int(line.split()[0], 16))))
    except OSError:
        pass
    return addresses


_GATEWAY_MAIN = r'''
import hmac, http.server, json, pathlib, socket, threading, time
from egress import serve_egress
root = pathlib.Path('/authority')
lock = threading.Lock()
applied = {}
def resolver(host, port):
    return sorted({a[4][0] for a in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
def connect(address, port):
    return socket.create_connection((address, port), timeout=10)
class Ready(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        token = 'Bearer ' + (root / 'management').read_text()
        if self.path != '/ready' or not hmac.compare_digest(self.headers.get('Authorization', ''), token):
            self.send_error(403); return
        with lock: body = json.dumps(applied).encode()
        self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
server = http.server.ThreadingHTTPServer(('127.0.0.1', 8889), Ready)
threading.Thread(target=server.serve_forever, daemon=True).start()
handle, previous = None, None
while True:
    try:
        raw = (root / 'policy.json').read_bytes()
        if raw != previous:
            value = json.loads(raw)
            with lock: applied = {}
            # Apply in place: a sibling's grant or revoke must not cut other
            # attempts' streams or close the listener (IC-C29).
            if handle: handle.update(value['allowlist'], value['grants'], value['forbidden_networks'])
            else: handle = serve_egress(('0.0.0.0', 8888), value['allowlist'], value['grants'], resolver, connect, value['forbidden_networks'])
            with lock: applied = {k: value[k] for k in ('generation', 'policy_digest')}
            previous = raw
    except Exception:
        with lock: applied = {}
        if handle: handle.close(); handle = None
    time.sleep(.05)
'''

_AUTH_GATEWAY_MAIN = _GATEWAY_MAIN.replace(
    "from egress import serve_egress",
    """import importlib.util, os, sys
from auth_gateway import serve_auth, UpstreamBackend
backend = UpstreamBackend()
target = os.environ.get('MULTIAGENTS_TEST_GATEWAY_BACKEND')
if target:
    module, factory = target.split(':', 1)
    spec = importlib.util.spec_from_file_location(module, '/gateway-backend.py')
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[module] = loaded
    spec.loader.exec_module(loaded)
    project = json.loads(pathlib.Path('/authority/policy.json').read_bytes())['project']
    backend = getattr(loaded, factory)(pathlib.Path('/authority'), project=project)
""").replace(
    "handle.update(value['allowlist'], value['grants'], value['forbidden_networks'])",
    "handle.update(value['grants'])").replace(
    "serve_egress(('0.0.0.0', 8888), value['allowlist'], value['grants'], resolver, connect, value['forbidden_networks'])",
    "serve_auth(('0.0.0.0', 8930), value['grants'], backend)")
