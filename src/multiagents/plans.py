"""Read-only discovery of plans and user notes, and isolated plan commits."""
from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import time
from pathlib import Path

import yaml

MAX_BYTES = 1024 * 1024
MAX_FILES = 500
SECTIONS = ("Apply now", "Next phase", "Config changes", "Notes considered")
STATUSES = ("draft", "ready", "applied", "imported")
NOTE_LINE = re.compile(r"^- (context/notes/[^/]+\.md) sha256:([0-9a-fA-F]{64}) — .+$")


def _read(directory: int, name: str) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not a regular file")
        if info.st_size > MAX_BYTES:
            raise ValueError("file exceeds size limit of 1 MiB")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("file exceeds size limit of 1 MiB")
        return data
    finally:
        os.close(fd)


def _files(root: Path, kind: str, excluded: str):
    """Yield paths and bounded bytes or refusal reasons, without following links."""
    descriptors = []
    rel = f"context/{kind}"
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(fd)
        for part in ("context", kind):
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                         dir_fd=fd)
            descriptors.append(fd)
        names = sorted(n for n in os.listdir(fd) if n.endswith(".md") and n != excluded)
        for index, name in enumerate(names):
            path = f"{rel}/{name}"
            if index >= MAX_FILES:
                yield path, None, "directory exceeds limit of 500 files"
                break
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise ValueError("symlink refused")
                if stat.S_ISDIR(info.st_mode):
                    continue
                yield path, _read(fd, name), None
            except (OSError, ValueError) as exc:
                yield path, None, str(exc)
    except FileNotFoundError:
        pass
    except OSError as exc:
        yield rel, None, f"directory refused (symlink or unreadable): {exc}"
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _parse(path: str, data: bytes | None, reason: str | None):
    entry = {"path": path, "status": None, "title": None, "applied_in": None,
             "imported_in": None, "sections": {s: False for s in SECTIONS},
             "malformed": False}
    considered = set()
    try:
        if reason:
            raise ValueError(reason)
        text = data.decode("utf-8")
        lines = text.splitlines()
        if not lines or lines[0] != "---":
            raise ValueError("missing YAML front matter")
        try:
            end = lines.index("---", 1)
        except ValueError:
            raise ValueError("unterminated YAML front matter") from None
        front_matter = "\n".join(lines[1:end])
        # Reject references before constructing YAML objects. Byte bounds alone
        # do not bound the graph an alias can describe.
        for token in yaml.scan(front_matter):
            if isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken)):
                raise ValueError("YAML front matter anchors and aliases are refused")
        header = yaml.safe_load(front_matter)
        if not isinstance(header, dict) or not isinstance(header.get("status"), str) or header["status"] not in STATUSES:
            raise ValueError("missing or unknown status")
        for key in ("status", "applied_in", "imported_in"):
            value = header.get(key)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"invalid header field {key}")
            entry[key] = value
        section = None
        for line in lines[end + 1:]:
            heading = re.fullmatch(r"(#{1,6}) (.+)", line)
            if heading:
                if entry["title"] is None:
                    entry["title"] = heading[2]
                if len(heading[1]) <= 2:
                    section = heading[2] if heading[1] == "##" and heading[2] in SECTIONS else None
                if section:
                    entry["sections"][section] = True
            if section == "Notes considered":
                match = NOTE_LINE.fullmatch(line)
                if match:
                    considered.add((match[1], match[2].lower()))
    except (ValueError, UnicodeError, yaml.YAMLError, RecursionError) as exc:
        entry.update(status=None, malformed=True, reason=f"YAML front matter: {exc}" if isinstance(exc, (yaml.YAMLError, RecursionError)) else str(exc))
        considered.clear()
    if entry["status"] not in ("ready", "applied", "imported"):
        considered.clear()
    return entry, considered


def list_plans(root: Path) -> dict:
    plans = []
    considered = set()
    for path, data, reason in _files(root, "plans", "TEMPLATE.md"):
        entry, hashes = _parse(path, data, reason)
        plans.append(entry)
        considered.update(hashes)
    notes = {"total": 0, "unprocessed": 0, "refused": []}
    for path, data, reason in _files(root, "notes", "README.md"):
        if reason:
            notes["refused"].append({"path": path, "reason": reason})
            continue
        notes["total"] += 1
        if (path, hashlib.sha256(data).hexdigest()) not in considered:
            notes["unprocessed"] += 1
    return {"plans": plans, "notes": notes}


def scaffold(root: Path) -> None:
    source = Path(__file__).parent / "defaults" / "context"
    for relative in ("plans/TEMPLATE.md", "notes/README.md"):
        target = root / "context" / relative
        # Refuse symlinked parents, and create exclusively even under --force.
        if any(p.is_symlink() for p in (root / "context", target.parent)):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as stream:
                stream.write((source / relative).read_bytes())
        except FileExistsError:
            pass


def commit(root: Path, paths: list[str], wait: float = 30) -> subprocess.CompletedProcess:
    given_root = root.absolute()
    root = root.resolve()

    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)

    def validate():
        for marker in ("MERGE_HEAD", "rebase-merge", "rebase-apply"):
            result = git("rev-parse", "--git-path", marker)
            if result.returncode:
                raise ValueError(result.stderr.strip())
            location = Path(result.stdout.strip())
            if not location.is_absolute():
                location = root / location
            if location.exists():
                raise ValueError("refusing plan commit during merge or rebase")
        conflicts = git("ls-files", "--unmerged")
        if conflicts.returncode or conflicts.stdout:
            raise ValueError("refusing plan commit with conflicts")
        for path in exact:
            if path.startswith("context/specs/") and git("cat-file", "-e", f"HEAD:{path}").returncode == 0:
                raise ValueError(f"existing spec cannot be modified: {path}")

    if not paths:
        raise ValueError("give at least one plan or new spec path")
    exact = []
    for raw in paths:
        relative = Path(raw)
        if relative.is_absolute():
            try:
                relative = relative.relative_to(given_root)
            except ValueError:
                try:
                    relative = relative.relative_to(root)
                except ValueError:
                    raise ValueError(f"path outside plans and specs: {raw}") from None
        if ".." in relative.parts or relative.parts[:2] not in (("context", "plans"), ("context", "specs")) or len(relative.parts) < 3:
            raise ValueError(f"path outside plans and specs: {raw}")
        candidate = root / relative
        # Workspace ancestors may be symlinks; only the repository's own
        # components govern whether a named path is safe to commit.
        components = (root.joinpath(*relative.parts[:i]) for i in range(len(relative.parts) + 1))
        if any(p.is_symlink() for p in components) or not candidate.is_file():
            raise ValueError(f"missing file or symlink refused: {raw}")
        exact.append(relative.as_posix())
    deadline = time.monotonic() + wait
    while True:
        validate()
        # New files need an index entry before --only can name them. Adding
        # only these paths preserves the rest of the index unchanged.
        result = git("add", "--", *exact)
        if result.returncode == 0:
            result = git("commit", "--only", "-m", "Record plans and new specifications", "--", *exact)
        contention = re.search(r"(?:Unable to create|cannot lock).*index\.lock.*File exists", result.stderr, re.I | re.S)
        remaining = deadline - time.monotonic()
        if result.returncode == 0 or not contention or remaining <= 0:
            return result
        time.sleep(min(1, remaining))
