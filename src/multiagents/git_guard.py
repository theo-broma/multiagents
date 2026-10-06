"""Deterministic guard: nothing sensitive leaves through a push (GG-R3..R5).

Two layers keep secrets out of pushes: this module is the deterministic one no
model can talk its way past, and the `git` library agent is the judgement on
top of it. Every secret-shaped pattern here is assembled from fragments at
run time, so no literal of the shape sits in this file: the guard scans this
repository's own pushes, and a literal here would block every one of them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# The private pattern list lives outside every repository, on purpose: it is
# read from the file, never pasted into a config anyone could push.
DEFAULT_PATTERNS_FILE = "~/.config/multiagents/sensitive-patterns"

# `git diff <empty> <sha>` for a root commit: the stock empty-tree object.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf537f5479b3d0b4e"

# Marks the pre-push hook `install` writes, so `install`/`uninstall` know it.
HOOK_MARKER = "managed by multiagents git-guard"

_ZEROS = "0" * 40


# --------------------------------------------------------------------------
# patterns, built from fragments so no shaped literal sits in this file
# --------------------------------------------------------------------------

_AT = "@"

_EMAIL_RX = re.compile(r"[\w.+-]+" + _AT + r"[\w.-]+\.\w+")

_OCT = r"\d{1,3}"
_DOT = r"\."
_TAILNET_IP_RX = re.compile(r"\b" + _OCT + _DOT + _OCT + _DOT + _OCT + _DOT + _OCT + r"\b")

_TS = "t" + "s"
_NET = "n" + "e" + "t"
_TAILNET_HOST_RX = re.compile(r"[\w<>.-]*\." + _TS + r"\." + _NET)

_BEGIN = "-----" + "BEGIN "
_PRIV = "PRIVATE " + "KEY" + "-----"
_PRIVATE_KEY_RX = re.compile(_BEGIN + r"[^-]*" + _PRIV)

_GH = "g" + "h"
_TOKEN_RES = [
    re.compile(_GH + "p_" + r"[A-Za-z0-9]{8,}"),
    re.compile(_GH + "o_" + r"[A-Za-z0-9]{8,}"),
    re.compile("github" + "_pat_" + r"[A-Za-z0-9]{8,}"),
    re.compile("sk" + "-ant-" + r"[A-Za-z0-9_-]{8,}"),
    re.compile("sk" + "-" + r"[A-Za-z0-9]{20,}"),
    re.compile("xo" + "x[abpr]-" + r"[A-Za-z0-9-]{8,}"),
    re.compile("AK" + "IA" + r"[A-Z0-9]{16}"),
]

_TRAILER_WORD = "Co" + "-Authored-" + "By"
_TRAILER_RX = re.compile(r"^\s*" + _TRAILER_WORD + r"\s*:(.*)$",
                         re.IGNORECASE)
# Model names for the co-author trailer check. The first entry is assembled
# from fragments so this file gains no provider vocabulary (phase-0 invariant).
_MODEL_C = "Cl" + "aude"
_MODEL_RX = re.compile(r"\b(" + _MODEL_C + r"|Opus|Sonnet|Haiku|Fable|GPT|Codex|Gemini)\b",
                       re.IGNORECASE)

_EXAMPLE_DOMAINS = ("example.com", "example.org", "example.net",
                    "example.invalid")
_AGENT_DOMAINS = ("multiagents.local", "multiagents.invalid")


def mask(match: str) -> str:
    """The reportable form of a match: its first 2 characters, then `…`."""
    return match[:2] + "…"


# --------------------------------------------------------------------------
# the guard key and finding fingerprints (GG-R8)
# --------------------------------------------------------------------------

class KeyFileError(ValueError):
    """The guard key cannot be used: bad directory or file permissions."""


def guard_key_path() -> Path:
    """Where the per-user guard key lives (GG-R8).

    `$XDG_STATE_HOME/multiagents/guard-key`, defaulting to
    `~/.local/state` when `XDG_STATE_HOME` is unset, as in MT-R4.
    """
    raw = os.environ.get("XDG_STATE_HOME")
    base = Path(raw).expanduser() if raw else Path.home() / ".local" / "state"
    return base / "multiagents" / "guard-key"


def _decode_key(raw: bytes) -> bytes | None:
    """Key material from the key file's bytes, or None when it holds none."""
    text = raw.strip()
    if not text:
        return None
    for decode in (bytes.fromhex, base64.b64decode,
                   base64.urlsafe_b64decode):
        try:
            key = decode(text)
        except Exception:
            continue
        if key:
            return bytes(key)
    return bytes(text)


def ensure_guard_key() -> bytes:
    """The per-user guard key, creating it on first use (GG-R8).

    Created with mode 0600 in a 0700 directory, whatever the umask. A
    `multiagents/` directory or key file readable by group or others is
    refused with KeyFileError — modes are never silently changed. The key
    itself is never printed or logged anywhere.
    """
    path = guard_key_path()
    parent = path.parent
    if parent.is_dir():
        mode = stat.S_IMODE(parent.stat().st_mode)
        if mode & 0o077:
            raise KeyFileError(
                f"guard key directory {parent} is accessible by group or "
                f"others (mode {mode:03o}); refusing: fix its permissions")
    else:
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if stat.S_IMODE(parent.stat().st_mode) & 0o077:
            os.chmod(parent, 0o700)
    if path.is_file():
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise KeyFileError(
                f"guard key file {path} is readable by group or others "
                f"(mode {mode:03o}); refusing: fix its permissions")
        key = _decode_key(path.read_bytes())
        if key is not None:
            return key
        key = secrets.token_bytes(32)
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(fd, key.hex().encode("ascii"))
        finally:
            os.close(fd)
        return key
    key = secrets.token_bytes(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return ensure_guard_key()
    try:
        os.write(fd, key.hex().encode("ascii"))
    finally:
        os.close(fd)
    return key


def fingerprint(match: str, key: bytes) -> str:
    """The reportable identity of a match (GG-R8): HMAC-SHA256 of the exact
    match under the guard key, truncated to 16 lowercase hex characters."""
    return hmac.new(key, match.encode("utf-8"),
                    hashlib.sha256).hexdigest()[:16]


@dataclass
class Finding:
    category: str
    commit: str          # full SHA; shortened at display
    where: str           # `path:line`, `path:bin`, `message`, `author`…
    match: str           # the exact matched text (never printed raw)
    fingerprint: str = ""  # GG-R8: HMAC of the match, 16 hex chars

    def masked(self) -> str:
        return mask(self.match)

    def line(self) -> str:
        """One output line with the match masked everywhere it appears."""
        shown = _mask_in(self.where, self.match, self.masked())
        text = (f"{self.category} {self.commit[:7]} {shown} {self.masked()}"
                + (f" {self.fingerprint}" if self.fingerprint else ""))
        return _one_line(text)


def paste_block(findings: list[Finding]) -> str:
    """The block a scan ends with when it found anything (GG-R8): the
    fingerprints to paste into `git.guard.allow_fingerprints`. It holds no
    commit, no location and no match — only fingerprints."""
    fps = sorted({f.fingerprint for f in findings if f.fingerprint})
    listed = ", ".join(f'"{fp}"' for fp in fps)
    return ("To silence these findings, add their fingerprints to\n"
            "git.guard.allow_fingerprints in the project config:\n"
            f"allow_fingerprints: [{listed}]")


def _one_line(text: str) -> str:
    """`text` with the control characters a path may hold made visible.

    A newline in a path name would split one finding over two lines, and an
    escape sequence could rewrite the terminal. Tab stays as it is. A byte of
    a non-UTF-8 name (kept as a surrogate escape) shows as `\\xNN` too.
    """
    def visible(m: re.Match) -> str:
        char = m.group(0)
        if char == "\n":
            return "\\n"
        if char == "\r":
            return "\\r"
        code = ord(char)
        return f"\\x{code - 0xDC00 if code >= 0xDC00 else code:02x}"
    return re.sub("[\x00-\x08\x0a-\x1f\x7f\udc80-\udcff]", visible, text)


def _mask_in(text: str, match: str, masked: str) -> str:
    """Replace the match inside `text` (case-insensitively) with its mask.

    A path name can itself carry the secret; printing it raw would leak the
    very match the mask hides.
    """
    if not match:
        return text
    return re.sub(re.escape(match), masked, text, flags=re.IGNORECASE)


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

@dataclass
class GuardSettings:
    remote: str = ""
    base_branch: str = ""
    coauthor_orchestrator: bool = True
    patterns_file: str = DEFAULT_PATTERNS_FILE
    allowed_emails: list[str] = field(default_factory=list)
    allow: set[str] = field(default_factory=set)
    allow_fingerprints: set[str] = field(default_factory=set)


def settings_from_git(git: dict | None) -> GuardSettings:
    """The guard's view of a `git:` config section, with GG-R1 defaults."""
    git = git or {}
    guard = git.get("guard") or {}
    coauthor = git.get("coauthor_orchestrator", True)
    if not isinstance(coauthor, bool):
        coauthor = True
    patterns_file = guard.get("patterns_file", DEFAULT_PATTERNS_FILE)
    if not isinstance(patterns_file, str) or not patterns_file:
        patterns_file = DEFAULT_PATTERNS_FILE
    raw_allowed = guard.get("allowed_emails", [])
    allowed = [str(e) for e in raw_allowed] if isinstance(raw_allowed, list) else []
    raw_allow = guard.get("allow", [])
    allow = {str(e) for e in raw_allow} if isinstance(raw_allow, list) else set()
    raw_fps = guard.get("allow_fingerprints", [])
    allow_fps = {str(e) for e in raw_fps} if isinstance(raw_fps, list) else set()
    return GuardSettings(
        remote=str(git.get("remote") or ""),
        base_branch=str(git.get("base_branch") or ""),
        coauthor_orchestrator=coauthor,
        patterns_file=patterns_file,
        allowed_emails=allowed,
        allow=allow,
        allow_fingerprints=allow_fps,
    )


def validate_git_section(project: dict) -> None:
    """GG-R1: refuse wrongly typed guard settings at load.

    Raises ValueError naming the key, the way existing invalid settings do.
    """
    git = project.get("git", None)
    if git is None:
        return
    if not isinstance(git, dict):
        raise ValueError("git: expected mapping")
    if "coauthor_orchestrator" in git and type(git["coauthor_orchestrator"]) is not bool:
        raise ValueError(
            "git.coauthor_orchestrator: expected a boolean, "
            f"got {git['coauthor_orchestrator']!r}")
    guard = git.get("guard", None)
    if guard is None:
        return
    if not isinstance(guard, dict):
        raise ValueError(f"git.guard: expected a mapping, got {guard!r}")
    if "patterns_file" in guard and not isinstance(guard["patterns_file"], str):
        raise ValueError(
            f"git.guard.patterns_file: expected a string, "
            f"got {guard['patterns_file']!r}")
    for key in ("allowed_emails", "allow", "allow_fingerprints"):
        if key in guard:
            value = guard[key]
            if (not isinstance(value, list)
                    or any(not isinstance(entry, str) for entry in value)):
                raise ValueError(
                    f"git.guard.{key}: expected a list of strings, "
                    f"got {value!r}")


# --------------------------------------------------------------------------
# the private pattern list
# --------------------------------------------------------------------------

@dataclass
class PrivatePatterns:
    literals: list[str] = field(default_factory=list)
    regexes: list[re.Pattern] = field(default_factory=list)
    present: bool = False


class PatternsFileError(ValueError):
    """The patterns file cannot be used: refused permissions or bad `re:`."""


def load_private_patterns(raw_path: str) -> tuple[PrivatePatterns, str]:
    """Read the private pattern list. Returns (patterns, notice).

    A missing file is not an error: the notice says the private patterns were
    not checked and the caller runs the built-in checks. A file readable by
    group or others, or an invalid `re:` line, raises PatternsFileError — the
    message names the line number, never the pattern.
    """
    path = Path(raw_path).expanduser()
    if not path.is_file():
        return PrivatePatterns(), (
            f"note: private patterns file {raw_path} not found: "
            "private patterns were not checked")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise PatternsFileError(
            f"patterns file {path} is readable by group or others "
            f"(mode {mode:03o}); refusing: fix its permissions")
    literals: list[str] = []
    regexes: list[re.Pattern] = []
    for number, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("re:"):
            try:
                regexes.append(re.compile(line[3:], re.IGNORECASE))
            except re.error:
                raise PatternsFileError(
                    f"patterns file {path} line {number}: "
                    "invalid regular expression")
        else:
            literals.append(line)
    return PrivatePatterns(literals=literals, regexes=regexes, present=True), ""


# --------------------------------------------------------------------------
# detectors
# --------------------------------------------------------------------------

def _email_excluded(addr: str, own: str, allowed: set[str]) -> bool:
    low = addr.lower()
    if low == own.lower() or low in allowed:
        return True
    if "noreply" in low:
        return True
    domain = low.rsplit(_AT, 1)[-1]
    return domain in _EXAMPLE_DOMAINS or domain in _AGENT_DOMAINS


def _tailnet_ip(match: str) -> bool:
    try:
        parts = [int(p) for p in match.split(".")]
    except ValueError:
        return False
    if len(parts) != 4 or any(p < 0 or p > 255 for p in parts):
        return False
    return parts[0] == 100 and 64 <= parts[1] <= 127


def _tailnet_host_placeholder(host: str) -> bool:
    """Whether a `*.example.ts.net` match is a placeholder (GG-R7): it holds `<`, as
    before, or its label just before `ts.net` is `example`, case-insensitively
    (`phone.example.ts.net`, and the bare `example.ts.net` itself)."""
    if "<" in host:
        return True
    low = host.lower()
    suffix = "." + _TS + "." + _NET
    if not low.endswith(suffix):
        return False
    labels = low[: -len(suffix)].split(".")
    return bool(labels) and labels[-1] == "example"


# The CIDR text, not a bare address: exempt from `tailnet-ip` (GG-R7). Built
# from fragments like every other shaped literal in this file.
_CIDR_NETWORK = "100" + ".64.0.0"


def _cidr_exempt(text: str, end: int, match: str) -> bool:
    """Whether this `tailnet-ip` occurrence is the exempt CIDR text (GG-R7):
    the network address followed by exactly `/10`. Any other prefix length —
    `/1`, `/100` — is still a finding, as is the network address bare."""
    if match != _CIDR_NETWORK:
        return False
    after = text[end:end + 3]
    if after != "/10":
        return False
    rest = text[end + 3:end + 4]
    return not rest.isdigit()


def _private_key_placeholder(match: str) -> bool:
    """Whether a `BEGIN … PRIVATE KEY` line names a placeholder key type
    (GG-R7): its key type holds `…` or `<`, which a real armour line never
    contains."""
    return "…" in match or "<" in match


class Scanner:
    """All detectors over one body of text, sharing one config."""

    def __init__(self, own_email: str, settings: GuardSettings,
                 private: PrivatePatterns, key: bytes = b"",
                 allowed_fingerprints: set[str] | frozenset[str] = frozenset()):
        self.own_email = own_email or ""
        self.allowed = {e.lower() for e in settings.allowed_emails}
        self.allow = set(settings.allow)
        self.private = private
        self.check_coauthor = not settings.coauthor_orchestrator
        self.key = key
        self.allowed_fps = {f.lower() for f in allowed_fingerprints}
        self._fp_cache: dict[str, str] = {}

    def _fp(self, match: str) -> str:
        found = self._fp_cache.get(match)
        if found is None:
            found = fingerprint(match, self.key)
            self._fp_cache[match] = found
        return found

    def _kept(self, match: str) -> bool:
        if match in self.allow:
            return False
        if self.allowed_fps and self._fp(match) in self.allowed_fps:
            return False
        return True

    def scan_text(self, text: str, *, message: bool = False
                  ) -> list[tuple[str, str, str]]:
        """(category, match, fingerprint) triples in `text`, detector order."""
        out: list[tuple[str, str, str]] = []

        def keep(match: str) -> str | None:
            if not self._kept(match):
                return None
            return self._fp(match)

        for found in _EMAIL_RX.findall(text):
            if (not _email_excluded(found, self.own_email, self.allowed)):
                fp = keep(found)
                if fp is not None:
                    out.append(("email", found, fp))
        for found in _TAILNET_IP_RX.finditer(text):
            match = found.group(0)
            if not _tailnet_ip(match):
                continue
            if _cidr_exempt(text, found.end(), match):
                continue
            fp = keep(match)
            if fp is not None:
                out.append(("tailnet-ip", match, fp))
        for found in _TAILNET_HOST_RX.findall(text):
            if _tailnet_host_placeholder(found):
                continue
            fp = keep(found)
            if fp is not None:
                out.append(("tailnet-host", found, fp))
        for found in _PRIVATE_KEY_RX.findall(text):
            if _private_key_placeholder(found):
                continue
            fp = keep(found)
            if fp is not None:
                out.append(("private-key", found, fp))
        for rx in _TOKEN_RES:
            for found in rx.findall(text):
                fp = keep(found)
                if fp is not None:
                    out.append(("token", found, fp))
        for literal in self.private.literals:
            for found in re.finditer(re.escape(literal), text, re.IGNORECASE):
                fp = keep(found.group(0))
                if fp is not None:
                    out.append(("private", found.group(0), fp))
        for rx in self.private.regexes:
            for found in rx.finditer(text):
                if found.group(0):
                    fp = keep(found.group(0))
                    if fp is not None:
                        out.append(("private", found.group(0), fp))
        if message and self.check_coauthor:
            for line in text.splitlines():
                trailer = _TRAILER_RX.match(line)
                if trailer and _MODEL_RX.search(trailer.group(1) or ""):
                    match = line.strip()
                    fp = keep(match)
                    if fp is not None:
                        out.append(("coauthor", match, fp))
        return out


# --------------------------------------------------------------------------
# git plumbing
# --------------------------------------------------------------------------

class GitError(RuntimeError):
    pass


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode:
        raise GitError(f"git {' '.join(args)} failed: "
                       f"{(proc.stderr or proc.stdout or '').strip()}")
    return (proc.stdout or "")


def _git_bytes(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, timeout=120)
    if proc.returncode:
        raise GitError(f"git {' '.join(args)} failed")
    return proc.stdout or b""


def discover_repo(start: Path) -> Path:
    """The work-tree root containing `start`, or raise GitError."""
    proc = subprocess.run(["git", "-C", str(start), "rev-parse",
                           "--show-toplevel"],
                          capture_output=True, text=True, timeout=30)
    top = (proc.stdout or "").strip()
    if proc.returncode or not top:
        raise GitError("not a git repository")
    return Path(top)


def hooks_dir(repo: Path) -> Path:
    """Where this repository's hooks live (worktrees share the common dir)."""
    try:
        common = _git(repo, "rev-parse", "--git-common-dir").strip()
    except GitError:
        common = ""
    gitdir = Path(common) if common else Path(
        _git(repo, "rev-parse", "--git-dir").strip())
    if not gitdir.is_absolute():
        gitdir = repo / gitdir
    return gitdir / "hooks"


def repo_own_email(repo: Path) -> str:
    try:
        return _git(repo, "config", "user.email").strip()
    except GitError:
        return ""


def is_remote_name(repo: Path, remote: str) -> bool:
    proc = subprocess.run(["git", "-C", str(repo), "remote", "get-url",
                           remote],
                          capture_output=True, timeout=30)
    return proc.returncode == 0


def remote_tracking_shas(repo: Path, remote: str) -> list[str]:
    out = _git(repo, "for-each-ref", "--format=%(objectname)",
               f"refs/remotes/{remote}/")
    return [ln for ln in out.splitlines() if ln.strip()]


def ls_remote_shas(repo: Path, remote: str) -> list[str]:
    out = _git(repo, "ls-remote", remote)
    shas = []
    for line in out.splitlines():
        sha, _, _ = line.partition("\t")
        if len(sha.strip()) == 40:
            shas.append(sha.strip())
    return shas


def published_shas(repo: Path, remote: str) -> list[str]:
    """What the remote already holds: tracking refs for a name, `ls-remote`
    for a path or URL (GG clarification)."""
    if is_remote_name(repo, remote):
        return remote_tracking_shas(repo, remote)
    return ls_remote_shas(repo, remote)


def rev_list(repo: Path, *args: str) -> list[str]:
    out = _git(repo, "rev-list", *args)
    return [ln for ln in out.splitlines() if ln.strip()]


def commits_for_branch_push(repo: Path, tip: str,
                            published: list[str]) -> list[str]:
    """Commits `tip` would add: everything not reachable from `published`."""
    if published:
        return rev_list(repo, tip, "--not", *published, "--")
    return rev_list(repo, tip, "--")


def default_range_commits(repo: Path, settings: GuardSettings) -> list[str]:
    """Commits of the default range for `scan` and `push_branch`."""
    if not settings.remote or not settings.base_branch:
        raise GitError("no remote or base branch configured "
                       "(git.remote / git.base_branch)")
    try:
        tip = _git(repo, "rev-parse", "--verify",
                   settings.base_branch).strip()
    except GitError:
        raise GitError(f"base branch {settings.base_branch!r} does not exist")
    if is_remote_name(repo, settings.remote):
        upstream = f"{settings.remote}/{settings.base_branch}"
        try:
            _git(repo, "rev-parse", "--verify", upstream)
        except GitError:
            raise GitError(f"remote ref {upstream!r} does not exist")
        return rev_list(repo, f"{upstream}..{settings.base_branch}", "--")
    return commits_for_branch_push(
        repo, tip, ls_remote_shas(repo, settings.remote))


# --------------------------------------------------------------------------
# scanning one commit
# --------------------------------------------------------------------------

@dataclass
class CommitContent:
    sha: str
    message: str
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    added: list[tuple[str, int, str]] = field(default_factory=list)
    binaries: list[tuple[str, str]] = field(default_factory=list)  # (path, blob)
    paths: list[str] = field(default_factory=list)


def read_commit(repo: Path, sha: str) -> CommitContent:
    sep = "\x1f"
    meta = _git(repo, "log", "-1",
                f"--format=%B{sep}%an{sep}%ae{sep}%cn{sep}%ce", sha)
    parts = meta.split(sep)
    while len(parts) < 5:
        parts.append("")
    message, author_name, author_email, cn, ce = parts[0], parts[1], parts[2], parts[3], parts[4]
    committer_name = cn.rstrip("\n")
    committer_email = ce.strip()
    parents = _git(repo, "rev-list", "--parents", "-1", sha).split()
    parent = parents[1] if len(parents) > 1 else EMPTY_TREE
    changes = _changes(repo, parent, sha)
    added, binaries = _added_lines(repo, changes)
    paths = _added_paths(changes)
    return CommitContent(sha=sha, message=parts[0], author_name=author_name,
                         author_email=author_email,
                         committer_name=committer_name,
                         committer_email=committer_email,
                         added=added, binaries=binaries, paths=paths)


_C_QUOTE_SIMPLE = {
    "a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r",
    "t": "\t", "v": "\v", "\\": "\\", '"': '"',
}


def _unquote_git_c_style(inner: str) -> str:
    """Decode git's C-style quoting of a path (without the outer quotes).

    Git quotes a path with spaces or non-ASCII bytes by wrapping it in double
    quotes and escaping each unusual byte as an octal `\\ooo` sequence — those
    bytes are the path's UTF-8 encoding, not code points, so decoding them
    with `unicode_escape` yields mojibake (`Caf\\303\\251` -> `CafÃ©`).
    Collect the raw bytes first, then decode them as UTF-8.
    """
    raw = bytearray()
    index = 0
    while index < len(inner):
        char = inner[index]
        if char != "\\" or index + 1 >= len(inner):
            raw.extend(char.encode("utf-8"))
            index += 1
            continue
        nxt = inner[index + 1]
        if nxt in "01234567":
            end = index + 2
            while end < len(inner) and end - (index + 1) < 3 \
                    and inner[end] in "01234567":
                end += 1
            raw.append(int(inner[index + 1:end], 8) & 0xFF)
            index = end
        elif nxt in _C_QUOTE_SIMPLE:
            raw.extend(_C_QUOTE_SIMPLE[nxt].encode("utf-8"))
            index += 2
        else:
            raw.extend(nxt.encode("utf-8"))
            index += 2
    try:
        return bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        return bytes(raw).decode("latin-1")


def _split_git_path(value: str) -> str:
    """A path as git prints it in a patch header, unquoted and unprefixed.

    The scan no longer reads paths from patch headers (see `_changes`); this
    stays for callers that hold such a header line.
    """
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = _unquote_git_c_style(value[1:-1])
    for prefix in ("b/", "a/"):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


@dataclass
class _Change:
    """One changed path of a commit, read from NUL-delimited git output."""
    status: str          # A, M, D, R, C, T…
    path: str            # new side (old side for a deletion)
    old_blob: str
    new_blob: str
    new_mode: str
    binary: bool = False


def _decode_path(raw: bytes) -> str:
    # Surrogate escapes round-trip a non-UTF-8 name back to its exact bytes
    # when it is handed to git again.
    return raw.decode("utf-8", "surrogateescape")


def _changes(repo: Path, parent: str, sha: str) -> list[_Change]:
    """Every changed path of `sha` against `parent`, with its binary status.

    Paths come only from `--raw -z` and `--numstat -z`, where each path is a
    NUL-terminated field written byte for byte: never from the human-readable
    `diff --git a/X b/Y` header, which is ambiguous for a path holding ` b/`
    and quotes tabs, newlines, quotes and backslashes. Both outputs come from
    one diff, so their entries pair up in order; a mismatch fails the scan
    rather than letting a file escape it.
    """
    if parent == EMPTY_TREE:
        # A root commit: `git diff <empty-tree>` needs the empty-tree object
        # in the store, which a fresh repository may not have.
        argv = ["diff-tree", "--no-commit-id", "--root", "-r", sha]
    else:
        argv = ["diff", parent, sha]
    raw = _git_bytes(repo, *argv, "-M", "--raw", "--no-abbrev", "--numstat",
                     "-z", "--no-ext-diff", "--no-textconv", "--")
    return _parse_changes(raw, sha)


_RAW_MODE = re.compile(rb"[0-7]{6}")
_RAW_BLOB = re.compile(rb"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_RAW_STATUS = re.compile(rb"([ACDMRTUX])([0-9]{0,3})")
_NUMSTAT_COUNT = re.compile(rb"[0-9]+|-")


def _parse_changes(raw: bytes, sha: str) -> list[_Change]:
    """The `--raw -z --numstat -z` stream of one diff, parsed fail-closed.

    Every token must be consumed by exactly the record that expects it: a raw
    record with no numstat partner, a numstat record with no raw partner, a
    field of the wrong shape, a path that differs between the two halves or a
    stream that does not end in its terminating NUL all raise GitError. A
    partial list is never returned, since a file missing from it is a file the
    scan never looks at.
    """
    def fail(what: str) -> GitError:
        return GitError(f"{what} in git diff output for {sha[:7]}")

    if not raw:
        return []
    if not raw.endswith(b"\0"):
        raise fail("truncated record")
    tokens = raw[:-1].split(b"\0")
    index = 0

    def take(what: str) -> bytes:
        nonlocal index
        if index >= len(tokens):
            raise fail(f"missing {what}")
        token = tokens[index]
        index += 1
        return token

    def take_path() -> bytes:
        path = take("path")
        if not path:
            raise fail("empty path")
        return path

    changes: list[_Change] = []
    old_paths: list[bytes | None] = []
    while index < len(tokens) and tokens[index].startswith(b":"):
        meta = take("--raw record")[1:].split(b" ")
        if len(meta) != 5:
            raise fail("malformed --raw record")
        old_mode, new_mode, old_blob, new_blob, status = meta
        status_match = _RAW_STATUS.fullmatch(status)
        if (not _RAW_MODE.fullmatch(old_mode)
                or not _RAW_MODE.fullmatch(new_mode)
                or not _RAW_BLOB.fullmatch(old_blob)
                or not _RAW_BLOB.fullmatch(new_blob)
                or status_match is None):
            raise fail("malformed --raw record")
        letter, score = status_match.groups()
        pair = letter in (b"R", b"C")
        if pair and not score:
            raise fail("rename without a score")
        old_path = take_path() if pair else None
        path = take_path()
        changes.append(_Change(status=letter.decode("ascii"),
                               path=_decode_path(path),
                               old_blob=old_blob.decode("ascii"),
                               new_blob=new_blob.decode("ascii"),
                               new_mode=new_mode.decode("ascii")))
        old_paths.append(old_path)
    if not changes:
        raise fail("no --raw record")
    for change, old_path in zip(changes, old_paths):
        fields = take("--numstat record").split(b"\t", 2)
        if len(fields) != 3:
            raise fail("malformed --numstat record")
        added, deleted, path = fields
        if (not _NUMSTAT_COUNT.fullmatch(added)
                or not _NUMSTAT_COUNT.fullmatch(deleted)
                or (added == b"-") != (deleted == b"-")):
            raise fail("malformed --numstat counts")
        if old_path is None:
            if not path:
                raise fail("--numstat rename for a --raw non-rename")
        else:
            # A rename: `<a>\t<d>\t` NUL old NUL new.
            if path:
                raise fail("--numstat non-rename for a --raw rename")
            if take_path() != old_path:
                raise fail("--raw and --numstat disagree")
            path = take_path()
        if _decode_path(path) != change.path:
            raise fail("--raw and --numstat disagree")
        change.binary = added == b"-"
    if index != len(tokens):
        raise fail("unpaired --numstat record")
    return changes


def _blob_lines(blob: bytes) -> list[str]:
    """`blob` as lines the way git counts them: split on newline only."""
    if not blob:
        return []
    text = blob.decode("utf-8", "replace")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _added_in_blob_diff(repo: Path, old_blob: str, new_blob: str
                        ) -> list[tuple[int, str]]:
    """(new-line-number, text) for every line `new_blob` adds to `old_blob`.

    Diffing the two blobs directly keeps every path out of the output. `--text`
    because the caller already knows the pair is text (git's own attributes
    said so), and a blob diff sees no attributes.
    """
    out = _git_bytes(repo, "diff", "--no-color", "--no-ext-diff",
                     "--no-textconv", "--text", "-U0", old_blob, new_blob)
    added: list[tuple[int, str]] = []
    lineno = 0
    for raw in out.split(b"\n"):
        line = raw.decode("utf-8", "replace")
        if line.startswith("diff --git "):
            lineno = 0  # headers until the next hunk
        elif line.startswith("@@ "):
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            lineno = int(match.group(1)) if match else 0
        elif lineno and line.startswith("+"):
            added.append((lineno, line[1:]))
            lineno += 1
    return added


def _added_lines(repo: Path, changes: list[_Change]
                 ) -> tuple[list[tuple[str, int, str]],
                            list[tuple[str, str]]]:
    """(path, new-line-number, text) for every added line, plus the (path,
    blob) of every binary file.

    Deleted paths and submodule pointers carry nothing to scan.
    """
    added: list[tuple[str, int, str]] = []
    binaries: list[tuple[str, str]] = []
    for change in changes:
        if change.status == "D" or change.new_mode == "160000":
            continue
        if change.binary:
            binaries.append((change.path, change.new_blob))
        elif not change.old_blob.strip("0") or change.status == "T":
            # A new file adds every line; so does a type change, whose old
            # side is another kind of object.
            blob = _git_bytes(repo, "cat-file", "blob", change.new_blob)
            added.extend((change.path, number, text) for number, text
                         in enumerate(_blob_lines(blob), start=1))
        elif change.old_blob != change.new_blob:
            added.extend((change.path, number, text) for number, text
                         in _added_in_blob_diff(repo, change.old_blob,
                                                change.new_blob))
    return added, binaries


def _added_paths(changes: list[_Change]) -> list[str]:
    """Added or renamed (new-side) paths; deleted paths are not scanned."""
    return [change.path for change in changes
            if change.status in ("A", "R", "C")]


@dataclass
class ScanResult:
    findings: list[Finding] = field(default_factory=list)
    notice: str = ""


def scan_commits(repo: Path, commits: list[str],
                 settings: GuardSettings) -> ScanResult:
    """GG-R3 over an explicit commit list. Never writes to the repository."""
    key = ensure_guard_key()
    private, notice = load_private_patterns(settings.patterns_file)
    scanner = Scanner(repo_own_email(repo), settings, private, key=key,
                      allowed_fingerprints=settings.allow_fingerprints)
    findings: list[Finding] = []
    for sha in commits:
        content = read_commit(repo, sha)
        for category, match, fp in scanner.scan_text(content.message,
                                                     message=True):
            findings.append(Finding(category, sha, "message", match, fp))
        for label, name, email in (
                ("author", content.author_name, content.author_email),
                ("committer", content.committer_name,
                 content.committer_email)):
            for value in (name, email):
                if not value:
                    continue
                for category, match, fp in scanner.scan_text(value):
                    findings.append(Finding(category, sha, label, match, fp))
        for path, lineno, text in content.added:
            for category, match, fp in scanner.scan_text(text):
                findings.append(
                    Finding(category, sha, f"{path}:{lineno}", match, fp))
        for path, blob_id in content.binaries:
            # By object id: `<sha>:<path>` would put the path back into a
            # revision expression git has to parse.
            blob = _git_bytes(repo, "cat-file", "blob", blob_id)
            text = blob.decode("latin-1")
            for category, match, fp in scanner.scan_text(text):
                findings.append(
                    Finding(category, sha, f"{path}:bin", match, fp))
        for path in content.paths:
            for category, match, fp in scanner.scan_text(path):
                findings.append(
                    Finding(category, sha, "path-name", match, fp))
    return ScanResult(findings=findings, notice=notice)


def finding_dict(finding: Finding) -> dict:
    return {"category": finding.category, "commit": finding.commit[:7],
            "where": _mask_in(finding.where, finding.match,
                              finding.masked()),
            "match": finding.masked(),
            "fingerprint": finding.fingerprint}


# --------------------------------------------------------------------------
# the pre-push hook (GG-R4)
# --------------------------------------------------------------------------

def hook_script() -> str:
    python = os.path.abspath(sys.executable or "python3")
    return f"""#!/bin/sh
# {HOOK_MARKER}. Do not edit: `multiagents git-guard install` rewrites it.
# Reads git's pre-push input and refuses the push when the guard finds
# anything. Chains to ./pre-push.local when one is present.
hook_dir=$(dirname "$0")
input=$(mktemp)
trap 'rm -f "$input"' EXIT
cat >"$input"
chain=0
guard=0
if [ -x "$hook_dir/pre-push.local" ]; then
  "$hook_dir/pre-push.local" "$@" <"$input" || chain=$?
fi
"{python}" -m multiagents.cli git-guard check-push "$@" <"$input" || guard=$?
if [ $chain -ne 0 ]; then exit $chain; fi
exit $guard
"""


def hook_is_ours(path: Path) -> bool:
    try:
        return HOOK_MARKER in path.read_text()
    except OSError:
        return False


def install_hook(repo: Path, force: bool = False) -> tuple[int, str]:
    """Write the pre-push hook. Returns (exit code, message)."""
    directory = hooks_dir(repo)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return 2, f"cannot write hooks directory: {exc}"
    hook = directory / "pre-push"
    if hook.exists() and not hook_is_ours(hook):
        if not force:
            return 1, ("a pre-push hook is already installed and it was not "
                       "written by git-guard; refusing: pass --force to keep "
                       "it as pre-push.local and chain to it")
        local = directory / "pre-push.local"
        if not local.exists():
            try:
                hook.rename(local)
            except OSError as exc:
                return 2, f"cannot keep the existing hook: {exc}"
    try:
        hook.write_text(hook_script())
        os.chmod(hook, 0o755)
    except OSError as exc:
        return 2, f"cannot write the pre-push hook: {exc}"
    return 0, "pre-push hook installed"


def uninstall_hook(repo: Path) -> tuple[int, str]:
    """Remove only a hook the guard wrote. Returns (exit code, message)."""
    directory = hooks_dir(repo)
    hook = directory / "pre-push"
    if not hook.exists() or not hook_is_ours(hook):
        return 0, "nothing to remove"
    try:
        hook.unlink()
        local = directory / "pre-push.local"
        if local.exists():
            local.rename(hook)
            return 0, "pre-push hook removed; pre-push.local restored"
    except OSError as exc:
        return 2, f"cannot remove the pre-push hook: {exc}"
    return 0, "pre-push hook removed"


def check_push_input(repo: Path, settings: GuardSettings,
                     lines: list[str]) -> ScanResult:
    """Commits each pushed ref would add, scanned (GG-R4 hook logic)."""
    wanted: list[str] = []
    seen: set[str] = set()

    def take(commits: list[str]) -> None:
        for sha in commits:
            if sha not in seen:
                seen.add(sha)
                wanted.append(sha)

    for raw in lines:
        parts = raw.split()
        if len(parts) < 4:
            continue
        _local_ref, local_sha, _remote_ref, remote_sha = parts[:4]
        if local_sha == _ZEROS:
            continue  # deleting a remote branch: nothing would leave
        if remote_sha == _ZEROS:
            # A new remote ref: what no remote-tracking ref reaches.
            take(rev_list(repo, local_sha, "--not", "--remotes", "--"))
        elif local_sha != remote_sha:
            take(rev_list(repo, f"{remote_sha}..{local_sha}", "--"))
    return scan_commits(repo, wanted, settings)
