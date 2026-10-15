"""Bounded, host-owned quarantine for isolated attempt results (IC-C11)."""
from __future__ import annotations

import os
from pathlib import Path
import re
import selectors
import sqlite3
import stat
import subprocess
import tempfile
import time
import zlib

from .. import gitops


class ImportValidation:
    def __init__(self, config):
        limits = config.project.get("isolation", {}).get("import", {})
        self.max_bytes = int(limits.get("max_bytes", 2 * 1024 ** 3))
        self.max_inflated = int(limits.get("max_inflated_bytes", 8 * 1024 ** 3))
        self.deadline = time.monotonic() + float(limits.get("timeout_seconds", 300))
        # None of the caller's Git environment, or the attempt's configuration,
        # is authority for these plumbing commands.
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_SYSTEM="/dev/null",
                        GIT_CONFIG_GLOBAL="/dev/null", GIT_NO_REPLACE_OBJECTS="1",
                        GIT_TERMINAL_PROMPT="0")

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise gitops.GitError("import_bounds: validation timed out")
        return remaining

    def git(self, repo, *args, stdin=None, stdout=None, check=True):
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            try:
                result = subprocess.run(
                    ["git", "--git-dir=" + str(repo), "-c", "core.hooksPath=/dev/null",
                     "-c", "core.fsmonitor=false", "-c", "gc.auto=0",
                     "-c", "protocol.allow=never", *args],
                    stdin=stdin, stdout=stdout if stdout is not None else output,
                    stderr=errors, env=self.env, timeout=self.remaining())
            except subprocess.TimeoutExpired as exc:
                raise gitops.GitError("import_bounds: validation timed out") from exc
            if check and result.returncode:
                errors.seek(0)
                raise gitops.GitError("import_malformed: " + errors.read(4096).decode(errors="replace"))
            self.remaining()
            return result.returncode

    def init(self, repo, object_format):
        self.git(repo, "init", "--bare", "--template=", "--object-format=" + object_format, str(repo))

    def check_tip_header(self, pinned, commit):
        """Refuse an oversized loose tip before even starting Git plumbing."""
        try:
            fd = gitops._open_file_beneath(pinned, (".git", "objects", commit[:2]),
                                          commit[2:], os.O_RDONLY | os.O_NONBLOCK)
        except FileNotFoundError:
            return                  # packed storage is checked after copying
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise gitops.GitError("import_malformed: result object is not a regular file")
            decoder, header = zlib.decompressobj(), b""
            while b"\0" not in header and len(header) < 128:
                self.remaining()
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                header += decoder.decompress(chunk, 128 - len(header))
            fields = header.split(b"\0", 1)[0].split()
            if b"\0" not in header or len(fields) != 2 or fields[0] != b"commit":
                raise gitops.GitError("import_malformed: result must name a commit")
            if int(fields[1]) > self.max_inflated:
                raise gitops.GitError("import_bounds: inflated object bytes exceeded")
        except zlib.error as exc:
            raise gitops.GitError("import_malformed: invalid loose object") from exc
        finally:
            os.close(fd)

    def copy_source(self, pinned, target, oid_length):
        total = 0
        with_fd = gitops._open_beneath(pinned, (".git", "objects"))
        try:
            try:
                info = gitops._open_beneath(pinned, (".git", "objects", "info"))
            except FileNotFoundError:
                info = None
            if info is not None:
                try:
                    for name in ("alternates", "http-alternates"):
                        try:
                            os.stat(name, dir_fd=info, follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        raise gitops.GitError("import_alternates: source has alternates")
                finally:
                    os.close(info)
            for directory in os.listdir(with_fd):
                self.remaining()
                if not re.fullmatch(r"[0-9a-f]{2}|pack", directory):
                    continue
                source_dir = gitops._open_beneath(pinned, (".git", "objects", directory))
                try:
                    for name in os.listdir(source_dir):
                        pattern = (rf"[0-9a-f]{{{oid_length - 2}}}" if directory != "pack"
                                   else rf"pack-[0-9a-f]{{{oid_length}}}\.(pack|idx)")
                        if not re.fullmatch(pattern, name):
                            continue
                        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                                     dir_fd=source_dir)
                        try:
                            entry = os.fstat(fd)
                            if not stat.S_ISREG(entry.st_mode):
                                raise gitops.GitError("import_malformed: result object is not a regular file")
                            if entry.st_size > self.max_bytes - total:
                                raise gitops.GitError("import_bounds: compressed object bytes exceeded")
                            destination = target / "objects" / directory / name
                            destination.parent.mkdir(exist_ok=True)
                            with destination.open("xb") as output:
                                while True:
                                    self.remaining()
                                    data = os.read(fd, min(1024 * 1024, self.max_bytes - total + 1))
                                    if not data:
                                        break
                                    total += len(data)
                                    if total > self.max_bytes:
                                        raise gitops.GitError("import_bounds: compressed object bytes exceeded")
                                    output.write(data)
                        finally:
                            os.close(fd)
                finally:
                    os.close(source_dir)
        finally:
            os.close(with_fd)

    def check_objects(self, repo, commit, workspace):
        """Check each reachable size before asking Git to read its contents.

        rev-list parses commits and trees eagerly. Walk their edges ourselves
        after the size check, with the queue and object bodies kept on disk.
        Gitlinks name another repository and are not traversal edges.
        """
        command = ["git", "--git-dir=" + str(repo), "-c", "core.hooksPath=/dev/null",
                   "cat-file", "--batch-command"]
        with tempfile.TemporaryFile() as errors, sqlite3.connect(workspace / "objects.sqlite3") as db:
            db.execute("CREATE TABLE objects (oid TEXT PRIMARY KEY, checked INTEGER DEFAULT 0)")
            db.execute("CREATE INDEX pending ON objects (checked)")
            db.execute("INSERT INTO objects (oid) VALUES (?)", (commit,))
            self.index_packs(repo, db, len(commit) // 2)
            db.execute("CREATE TABLE inflation (oid TEXT PRIMARY KEY, checked INTEGER DEFAULT 0)")
            db.execute("CREATE INDEX inflation_pending ON inflation (checked)")
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=errors, env=self.env, bufsize=0)
            selector = selectors.DefaultSelector()
            selector.register(proc.stdout, selectors.EVENT_READ)
            buffered = bytearray()

            def read(size):
                while len(buffered) < size:
                    if not selector.select(self.remaining()):
                        raise gitops.GitError("import_bounds: validation timed out")
                    block = os.read(proc.stdout.fileno(), min(65536, size - len(buffered)))
                    if not block:
                        raise gitops.GitError("import_malformed: unreadable result object")
                    buffered.extend(block)
                result = bytes(buffered[:size])
                del buffered[:size]
                return result

            def header():
                line = bytearray()
                while len(line) < 256:
                    value = read(1)
                    if value == b"\n":
                        return line.split()
                    line.extend(value)
                raise gitops.GitError("import_malformed: invalid object header")

            def enqueue(oid):
                if not re.fullmatch(rf"[0-9a-f]{{{len(commit)}}}", oid):
                    raise gitops.GitError("import_malformed: invalid object id")
                db.execute("INSERT OR IGNORE INTO objects (oid) VALUES (?)", (oid,))

            inflated = 0
            try:
                while row := db.execute("SELECT oid FROM objects WHERE checked=0 LIMIT 1").fetchone():
                    self.remaining()
                    oid = row[0]
                    # Even a tiny object's contents may require huge delta
                    # bases. Read storage headers before Git touches either.
                    inflated = self.check_storage(repo, db, oid, inflated)
                    proc.stdin.write(("info " + oid + "\n").encode())
                    fields = header()
                    if (len(fields) != 3 or fields[0].decode() != oid
                            or fields[1] not in (b"commit", b"tree", b"blob")):
                        raise gitops.GitError("import_malformed: missing or invalid result object")
                    if oid == commit and fields[1] != b"commit":
                        raise gitops.GitError("import_malformed: result must name a commit")
                    size = int(fields[2])
                    if size < 0 or size > self.max_inflated:
                        raise gitops.GitError("import_bounds: inflated object bytes exceeded")
                    db.execute("UPDATE objects SET checked=1 WHERE oid=?", (oid,))
                    if fields[1] == b"blob":
                        continue
                    proc.stdin.write(("contents " + oid + "\n").encode())
                    if header() != fields:
                        raise gitops.GitError("import_malformed: result object changed")
                    with tempfile.TemporaryFile() as body:
                        remaining = size
                        while remaining:
                            block = read(min(65536, remaining))
                            body.write(block)
                            remaining -= len(block)
                        if read(1) != b"\n":
                            raise gitops.GitError("import_malformed: invalid object framing")
                        body.seek(0)
                        if fields[1] == b"commit":
                            # Only the short tree/parent headers are edges.
                            # Long signatures and messages never enter memory.
                            while line := body.readline(65536):
                                self.remaining()
                                if line == b"\n":
                                    break
                                start = line
                                while not line.endswith(b"\n"):
                                    line = body.readline(65536)
                                    self.remaining()
                                    if not line:
                                        break
                                for prefix in (b"tree ", b"parent "):
                                    if start.startswith(prefix):
                                        enqueue(start[len(prefix):].strip().decode("ascii"))
                        else:
                            while mode := body.read(1):
                                self.remaining()
                                while mode[-1:] != b" ":
                                    value = body.read(1)
                                    if not value or len(mode) > 7:
                                        raise gitops.GitError("import_malformed: invalid tree mode")
                                    mode += value
                                # Filenames can be arbitrarily long. Skip in
                                # bounded pieces, retaining no path names.
                                while True:
                                    part = body.read(65536)
                                    self.remaining()
                                    end = part.find(b"\0")
                                    if end >= 0:
                                        body.seek(end + 1 - len(part), os.SEEK_CUR)
                                        break
                                    if not part:
                                        raise gitops.GitError("import_malformed: invalid tree entry")
                                child = body.read(len(commit) // 2)
                                if len(child) != len(commit) // 2:
                                    raise gitops.GitError("import_malformed: truncated tree entry")
                                if mode != b"160000 ":
                                    enqueue(child.hex())
            finally:
                selector.close()
                proc.stdin.close()
                proc.stdout.close()
                if proc.poll() is None:
                    proc.kill()
                proc.wait()

    def index_packs(self, repo, db, hash_size):
        """Index storage locations without inflating any pack contents."""
        db.execute("CREATE TABLE packed (pack TEXT, offset INTEGER, oid TEXT, PRIMARY KEY (pack, offset))")
        db.execute("CREATE INDEX packed_oid ON packed (oid)")
        for index in (repo / "objects" / "pack").glob("*.idx"):
            pack = index.with_suffix(".pack")
            pack_size = pack.stat().st_size
            with index.open("rb") as source, tempfile.TemporaryFile() as locations:
                self.git(repo, "show-index", "--object-format=" + ("sha256" if hash_size == 32 else "sha1"),
                         stdin=source, stdout=locations)
                locations.seek(0)
                while line := locations.readline(256):
                    self.remaining()
                    fields = line.split()
                    if (not line.endswith(b"\n") or len(fields) not in (2, 3)
                            or not re.fullmatch(rb"[0-9a-f]{%d}" % (hash_size * 2), fields[1])):
                        raise gitops.GitError("import_malformed: invalid pack index")
                    offset = int(fields[0])
                    if offset < 12 or offset >= pack_size - hash_size:
                        raise gitops.GitError("import_malformed: invalid pack offset")
                    db.execute("INSERT INTO packed VALUES (?, ?, ?)",
                               (str(pack), offset, fields[1].decode("ascii")))

    def check_storage(self, repo, db, oid, inflated):
        """Bound the union of reachable objects and their inflation dependencies.

        OFS deltas resolve within their own pack, even when the same oid has
        another representation elsewhere. Inspect every indexed representation
        so Git's choice of pack or loose copy cannot hide a larger base chain.
        Only headers and the two delta-size varints are decompressed here.
        """
        db.execute("INSERT OR IGNORE INTO inflation (oid) VALUES (?)", (oid,))

        def charge(size):
            nonlocal inflated
            inflated += size
            if size < 0 or inflated > self.max_inflated:
                raise gitops.GitError("import_bounds: inflated object bytes exceeded")

        def byte(source):
            value = source.read(1)
            if not value:
                raise gitops.GitError("import_malformed: truncated pack header")
            return value[0]

        while row := db.execute("SELECT oid FROM inflation WHERE checked=0 LIMIT 1").fetchone():
            self.remaining()
            current, size = row[0], None
            loose = repo / "objects" / current[:2] / current[2:]
            if loose.exists():
                with loose.open("rb") as source:
                    decoder, header = zlib.decompressobj(), b""
                    while b"\0" not in header and len(header) < 128:
                        self.remaining()
                        block = source.read(4096)
                        if not block:
                            break
                        header += decoder.decompress(block, 128 - len(header))
                    fields = header.split(b"\0", 1)[0].split()
                    if b"\0" not in header or len(fields) != 2:
                        raise gitops.GitError("import_malformed: invalid loose object header")
                    size = int(fields[1])
                    charge(size)
            for pack, offset in db.execute("SELECT pack, offset FROM packed WHERE oid=?", (current,)):
                self.remaining()
                with Path(pack).open("rb") as source:
                    if offset < 12 or offset >= os.fstat(source.fileno()).st_size - len(oid) // 2:
                        raise gitops.GitError("import_malformed: invalid pack offset")
                    source.seek(offset)
                    first = value = byte(source)
                    kind, packed_size, shift = (first >> 4) & 7, first & 15, 4
                    while value & 128:
                        value = byte(source)
                        packed_size |= (value & 127) << shift
                        shift += 7
                        if shift > 64:
                            raise gitops.GitError("import_malformed: invalid pack object size")
                    base = None
                    if kind == 6:
                        value = byte(source)
                        distance = value & 127
                        while value & 128:
                            value = byte(source)
                            distance = ((distance + 1) << 7) | (value & 127)
                            if distance > offset:
                                raise gitops.GitError("import_malformed: invalid delta offset")
                        base = db.execute("SELECT oid FROM packed WHERE pack=? AND offset=?",
                                          (pack, offset - distance)).fetchone()
                        if not distance or base is None:
                            raise gitops.GitError("import_malformed: missing delta base")
                        base = base[0]
                    elif kind == 7:
                        base = source.read(len(oid) // 2).hex()
                        if len(base) != len(oid):
                            raise gitops.GitError("import_malformed: truncated delta base")
                    elif kind not in (1, 2, 3, 4):
                        raise gitops.GitError("import_malformed: invalid packed object type")
                    object_size = packed_size
                    if base is not None:
                        # Git also allocates the inflated delta instructions.
                        charge(packed_size)
                        decoder = zlib.decompressobj()
                        sizes, value, shift = [], 0, 0
                        while len(sizes) < 2:
                            self.remaining()
                            block = source.read(4096)
                            if not block:
                                raise gitops.GitError("import_malformed: truncated delta sizes")
                            prefix = decoder.decompress(block, 20)
                            for part in prefix:
                                value |= (part & 127) << shift
                                shift += 7
                                if shift > 70:
                                    raise gitops.GitError("import_malformed: invalid delta size")
                                if not part & 128:
                                    sizes.append(value)
                                    value, shift = 0, 0
                                    if len(sizes) == 2:
                                        break
                            if decoder.eof and len(sizes) < 2:
                                raise gitops.GitError("import_malformed: truncated delta sizes")
                        object_size = sizes[1]
                        db.execute("INSERT OR IGNORE INTO inflation (oid) VALUES (?)", (base,))
                    if size is None:
                        charge(object_size)
                        size = object_size
                    elif object_size != size:
                        raise gitops.GitError("import_malformed: inconsistent object sizes")
            if size is None:
                raise gitops.GitError("import_malformed: missing result object")
            db.execute("UPDATE inflation SET checked=1 WHERE oid=?", (current,))
        return inflated

    def validate(self, pinned, workspace, commit, input_commit):
        raw, validated = workspace / "source.git", workspace / "validated.git"
        object_format = "sha256" if len(commit) == 64 else "sha1"
        self.check_tip_header(pinned, commit)
        self.init(raw, object_format)
        self.copy_source(pinned, raw, len(commit))
        self.check_objects(raw, commit, workspace)
        ancestry = self.git(raw, "merge-base", "--is-ancestor", input_commit, commit, check=False)
        if ancestry == 1:
            raise gitops.GitError("import_ancestry: result does not descend from input commit")
        if ancestry:
            raise gitops.GitError("import_malformed: result ancestry is unreadable")
        # Repack only the designated result's closure. Unreachable objects,
        # including those sharing a source pack, never enter the host store.
        self.init(validated, object_format)
        with tempfile.TemporaryFile() as revisions, tempfile.TemporaryFile() as pack:
            revisions.write((commit + "\n").encode())
            revisions.seek(0)
            self.git(raw, "pack-objects", "--stdout", "--revs", stdin=revisions, stdout=pack)
            if pack.tell() > self.max_bytes:
                raise gitops.GitError("import_bounds: result pack bytes exceeded")
            pack.seek(0)
            self.git(validated, "index-pack", "--stdin", "--strict", stdin=pack)
        self.git(validated, "fsck", "--full", "--strict", "--no-reflogs", "--no-dangling", commit)
        return validated


def import_isolated(pinned, workspace, root, commit, input_commit, config):
    validation = ImportValidation(config)
    try:
        verified = validation.validate(pinned, workspace, commit, input_commit)
    except (OSError, ValueError, zlib.error, sqlite3.Error) as exc:
        raise gitops.GitError("import_malformed: " + str(exc)) from exc
    # No host writes occur before all validation succeeds. If promotion is
    # interrupted, only validated, unreferenced objects can have been copied.
    for source in sorted((verified / "objects" / "pack").iterdir()):
        if source.suffix not in {".pack", ".idx"}:
            continue
        validation.remaining()
        target = root / ".git" / "objects" / "pack" / source.name
        if not target.exists():
            gitops._write_beneath(root / ".git", ("objects", "pack"), source.name,
                                 source.read_bytes(), create=True)
