"""Attempt-bound authority for verified, self-contained input bundles."""
from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

from ..paths import ProjectPaths
from .git import git_command, git_env


@dataclasses.dataclass(frozen=True)
class SeedRef:
    id: str


class SeedError(Exception):
    def __init__(self, code: str, reason: str):
        self.code = code
        self.reason = reason
        super().__init__(f"{code}: {reason}")


def _git(*args: str, input: bytes | None = None) -> bytes:
    return subprocess.run(git_command(*args), input=input, env=git_env(),
                          capture_output=True, check=True).stdout


def _write(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    try:
        with temporary.open("w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        _sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _sync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class SeedRegistry:
    """Only admission issues handles; cache entries never confer authority."""

    seed_version = 1

    def __init__(self, paths: ProjectPaths):
        self.paths = paths
        self.root = paths.scheduler / "seeds"
        self.cache = self.root / "cache"

    @contextlib.contextmanager
    def _locked(self):
        self.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        with (self.root / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _records(self) -> dict:
        path = self.root / "registry.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def _authorised(self, ref: SeedRef, attempt_id: str) -> dict:
        if not isinstance(ref, SeedRef) or not isinstance(ref.id, str):
            raise SeedError("unauthorised", "a host-issued seed handle is required")
        record = self._records().get(ref.id)
        if not record or record["attempt_id"] != attempt_id:
            raise SeedError("unauthorised", "seed handle does not belong to this attempt")
        return record

    def build(self, attempt_id: str, input_commit: str) -> SeedRef:
        if not isinstance(attempt_id, str) or not attempt_id:
            raise SeedError("unauthorised", "an attempt id is required")
        try:
            with self._locked():
                object_format = _git("-C", str(self.paths.root), "rev-parse", "--show-object-format").decode().strip()
                length = 64 if object_format == "sha256" else 40
                if not isinstance(input_commit, str) or not re.fullmatch(f"[0-9a-f]{{{length}}}", input_commit):
                    raise SeedError("seed_unavailable", "input must name a complete commit id")
                commit = _git("-C", str(self.paths.root), "rev-parse", "--verify", input_commit + "^{commit}").decode().strip()
                if commit != input_commit:
                    raise SeedError("seed_unavailable", "input must be a commit")
                body = {"checkout_commit": commit, "roots": [commit],
                        "object_format": object_format, "seed_version": self.seed_version}
                key_input = {key: body[key] for key in ("roots", "object_format", "seed_version")}
                body["cache_key"] = hashlib.sha256(json.dumps(key_input, sort_keys=True,
                                                             separators=(",", ":")).encode()).hexdigest()
                records = self._records()
                for ref_id, existing in records.items():
                    if existing["attempt_id"] == attempt_id and existing["cache_key"] == body["cache_key"]:
                        self._entry(existing)
                        return SeedRef(ref_id)
                pin = "refs/multiagents/seeds/" + body["cache_key"]
                _git("-C", str(self.paths.root), "update-ref", pin, commit)
                self._entry(body)
                ref = SeedRef(uuid.uuid4().hex)
                records[ref.id] = dict(body, attempt_id=attempt_id, ref=ref.id)
                _write(self.root / "registry.json", records)
                return ref
        except SeedError:
            raise
        except subprocess.CalledProcessError as exc:
            raise SeedError("seed_unavailable", exc.stderr.decode(errors="replace").strip()) from exc

    def record(self, ref: SeedRef, attempt_id: str) -> dict:
        with self._locked():
            record = self._authorised(ref, attempt_id)
            return {key: value for key, value in record.items() if key != "attempt_id"}

    def restore(self, record: dict, attempt_id: str) -> SeedRef:
        with self._locked():
            if not isinstance(record, dict):
                raise SeedError("unauthorised", "a recorded seed is required")
            ref = SeedRef(record.get("ref", ""))
            authorised = self._authorised(ref, attempt_id)
            if record != {key: value for key, value in authorised.items() if key != "attempt_id"}:
                raise SeedError("unauthorised", "seed record differs from issued authority")
            return ref

    @contextlib.contextmanager
    def acquire(self, ref: SeedRef, attempt_id: str):
        """Keep an entry locked against rebuild or eviction while provisioning."""
        with self._locked():
            record = self._authorised(ref, attempt_id)
            yield self._entry(record)

    def resolve(self, ref: SeedRef, attempt_id: str) -> Path:
        with self.acquire(ref, attempt_id) as path:
            return path

    def _entry(self, record: dict) -> Path:
        path = self.cache / (record["cache_key"] + ".bundle")
        if path.is_file() and self._verify(path, record):
            return path
        temporary = self.cache / ("building-" + uuid.uuid4().hex)
        try:
            # Recreate from the recorded root only. Never substitute HEAD or
            # another branch if pinning or the underlying objects were lost.
            pin = "refs/multiagents/seeds/" + record["cache_key"]
            commit = record["checkout_commit"]
            _git("-C", str(self.paths.root), "update-ref", pin, commit)
            _git("-C", str(self.paths.root), "bundle", "create", str(temporary), pin)
            if not self._verify(temporary, record):
                raise SeedError("seed_unavailable", "rebuilt seed failed full-pack verification")
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            temporary.replace(path)
            _sync(self.cache)
            return path
        except subprocess.CalledProcessError as exc:
            raise SeedError("seed_unavailable", exc.stderr.decode(errors="replace").strip()) from exc
        finally:
            temporary.unlink(missing_ok=True)

    def _verify(self, path: Path, record: dict) -> bool:
        try:
            data = path.read_bytes()
            header, pack = data.split(b"\n\n", 1)
            lines = header.decode("ascii").splitlines()
            refs = [line for line in lines[1:] if not line.startswith("@")]
            capabilities = [line for line in lines[1:] if line.startswith("@")]
            expected = record["checkout_commit"] + " refs/multiagents/seeds/" + record["cache_key"]
            if lines[0] not in ("# v2 git bundle", "# v3 git bundle") or refs != [expected]:
                return False
            if lines[0] == "# v2 git bundle":
                if capabilities or record["object_format"] != "sha1":
                    return False
            elif capabilities != ["@object-format=" + record["object_format"]]:
                return False
            digest = hashlib.new(record["object_format"])
            if len(pack) < 12 + digest.digest_size:
                return False
            digest.update(pack[:-digest.digest_size])
            if digest.digest() != pack[-digest.digest_size:]:
                return False
            with tempfile.TemporaryDirectory(prefix="verify-", dir=self.root) as scratch:
                _git("init", "--bare", "--object-format=" + record["object_format"], scratch)
                _git("--git-dir", scratch, "index-pack", "--strict", "--stdin", input=pack)
                if sum(path.stat().st_size for path in Path(scratch, "objects", "pack").glob("*.pack")) != len(pack):
                    return False
                _git("--git-dir", scratch, "fsck", "--strict", "--full", "--no-reflogs", record["checkout_commit"])
                reachable = _git("--git-dir", scratch, "rev-list", "--objects", record["checkout_commit"])
                wanted = {line.split()[0] for line in reachable.splitlines()}
                actual = set(_git("--git-dir", scratch, "cat-file", "--batch-all-objects",
                                  "--batch-check=%(objectname)").splitlines())
                return actual == wanted
        except (OSError, ValueError, IndexError, UnicodeError, subprocess.CalledProcessError):
            return False
