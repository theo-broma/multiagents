"""Three-layer configuration.

``shipped defaults`` (in the package)  →  ``~/.config/multiagents/``  →  ``<project>/.multiagents/config/``

Each layer overrides the one before it, key by key. Maps deep-merge, so a
project can retune one agent's model without restating the roster; lists and
scalars replace wholesale, because a partially-overridden list is never what
anyone means.

The global layer is seeded from the package on first use and the project layer
from the global one at ``multiagents init``, so both are real editable files
rather than invisible built-ins.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import math
import re
import shutil
import threading
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import MISSING, dataclass, field, fields
from functools import lru_cache
from pathlib import Path
from stat import S_ISREG
from typing import Any

import yaml

from .paths import ProjectPaths, global_config_dir, shipped_defaults_dir
from .providers import load_providers
from .instance_strategy import InstanceStrategyError, validate_strategy

# The heading that marks the half of a brief addressed to whoever CALLS the
# agent, rather than to the agent itself. It is authored in the agent's own file
# — nobody knows better than the agent what a task to it must contain — and then
# split by audience: stripped before the agent is prompted, and served to the
# caller through `how_to_call`.
#
# Both halves in one file because the alternative is the drift this exists to
# prevent: the orchestrator's brief saying "give the auditor a diff" long after
# the auditor's brief started needing a bounded context.
CALLING_HEADING = "## Calling this agent"

CONFIG_FILES = ("project.yaml", "providers.yaml", "agents.yaml", "models.yaml")
# Kept for installs that still carry a top-level orchestrator.md from before it
# became a normal agent brief under agents/.
STANDALONE_FILES = ()


def _split_calling(text: str) -> tuple[str, str]:
    """Split a brief into (what the agent reads, what its caller reads).

    The caller half runs from CALLING_HEADING to the next heading of the same
    level, so a brief can still carry sections after it.
    """
    start = text.find(CALLING_HEADING)
    if start == -1:
        return text, ""
    after = text.index("\n", start) + 1 if "\n" in text[start:] else len(text)
    end = len(text)
    for line_start in range(after, len(text)):
        if text.startswith("\n## ", line_start - 1):
            end = line_start
            break
    return (text[:start] + text[end:]).rstrip() + "\n", text[start:end].strip()


def deep_merge(base: dict, override: dict) -> dict:
    """Merge `override` onto `base`. Maps recurse; everything else replaces."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open() as handle:
        return yaml.safe_load(handle) or {}


# BP-R1: one parse per version of a file, shared by every layer reader.
# A budget read used to re-parse the same files per helper per provider, and
# `shipped_limits` kept its own private parse beside them. Everything that
# reads a config layer file goes through `read_yaml_cached` now: a memo
# validated by (mtime_ns, size), so a file is parsed once while it is
# unchanged and re-read the moment it moves (BP-R2). Keyed by the resolved
# path: one file reached through a symlink or two spellings is one entry.
# The version stamp is (dev, ino, mtime_ns, ctime_ns, size), not mtime and
# size alone: a same-size replacement can carry the old mtime (a rename of a
# copy2'd file, a `touch -r`), but a rename brings a new inode and an
# in-place write or utime moves ctime, which nobody can set (BP review r4).
_yaml_cache: dict[Path, tuple[tuple[int, ...], dict, yaml.YAMLError | None]] = {}


def _current_owner() -> tuple[int, Any]:
    """The thread and asyncio task running now: who may use a snapshot."""
    try:
        task = asyncio.current_task()
    except RuntimeError:                      # no event loop in this thread
        task = None
    return threading.get_ident(), task


class _Snapshot:
    """One read's view of the files, valid only while its read is active."""

    __slots__ = ("files", "open", "owner", "lock")

    def __init__(self) -> None:
        self.files: dict[Path, tuple[dict, yaml.YAMLError | None]] = {}
        self.open = True
        self.owner = _current_owner()
        self.lock = threading.RLock()


# The per-CALL view BP-R1 asks for: inside `parse_once()` every read of a
# file — a budget helper's, and `load`'s own — returns the version the call
# first saw, so a file rewritten mid-read is neither parsed twice nor seen
# in two versions by one call. A ContextVar so concurrent reads on other
# threads keep their own. But a thread or asyncio task started with a copy
# of the context inherits the variable, and its read is not this call:
# borrowing the view would lose it mid-read when this call exits (BP review
# r4), and judge a finished call's view after (round 2). So a snapshot
# serves only the thread and task that opened it — anyone else opens their
# own — and is closed on exit all the same.
_snapshot: ContextVar[_Snapshot | None] = ContextVar("config_snapshot", default=None)


_shared_snapshot: ContextVar[tuple[_Snapshot, tuple[int, Any]] | None] = ContextVar("shared_config_snapshot", default=None)


@contextlib.contextmanager
def share_parse_once(snap: _Snapshot):
    """Explicitly lend a call view to workers joined before the call exits."""
    token = _shared_snapshot.set((snap, _current_owner()))
    snapshot_token = _snapshot.set(snap)
    try:
        yield
    finally:
        _snapshot.reset(snapshot_token)
        _shared_snapshot.reset(token)


def _active_snapshot() -> _Snapshot | None:
    shared = _shared_snapshot.get()
    if shared is not None and shared[0].open and shared[1] == _current_owner():
        return shared[0]
    snap = _snapshot.get()
    if snap is None or not snap.open or snap.owner != _current_owner():
        return None
    return snap


@contextlib.contextmanager
def parse_once():
    """Hold one view of the config files for the duration of one read.

    Nested uses share the outer view: a `read_provider` inside a `read_all`
    is part of that call.
    """
    if (active := _active_snapshot()) is not None:
        yield active
        return
    snap = _Snapshot()
    token = _snapshot.set(snap)
    try:
        yield snap
    finally:
        snap.open = False
        _snapshot.reset(token)


def _forget(path: Path) -> None:
    """This process just rewrote `path` (seeding): every later read sees it.

    Evicted from the memo as well as from the call's view — `copy2` gives
    the new file the shipped file's mtime, so the stamp alone is not what
    this process should trust about its own write (BP review r4).
    """
    key = path.resolve()
    _yaml_cache.pop(key, None)
    if (snap := _active_snapshot()) is not None:
        snap.files.pop(key, None)


def _parse_versioned(key: Path) -> tuple[dict, yaml.YAMLError | None]:
    """`key`'s parse — from the memo while its version stamp stands still."""
    try:
        stat = key.stat()
    except (FileNotFoundError, NotADirectoryError):
        return {}, None                       # no file here
    # Any other stat failure (a permission error on the way) propagates:
    # the caller decides whether that is fatal, and nothing is memoised.
    if not S_ISREG(stat.st_mode):
        return {}, None                       # what `_read_yaml`'s is_file said
    stamp = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns,
             stat.st_size)
    hit = _yaml_cache.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1], hit[2]
    try:
        parsed, error = _read_yaml(key), None
    except yaml.YAMLError as exc:
        # Kept without its traceback: the cache must not pin this parse's
        # frames, and every strict read raises a fresh copy (see below).
        parsed, error = {}, exc.with_traceback(None)
    _yaml_cache[key] = (stamp, parsed, error)
    return parsed, error


def _stat_ok(path: Path) -> bool:
    try:
        path.stat()
    except OSError:
        return False
    return True


def read_yaml_cached(path: Path, *, strict: bool = False) -> dict:
    """`_read_yaml`, parsed at most once per version of the file.

    A missing file reads as empty. A malformed one reads as empty too —
    the "skip this layer" the budget's layer readers always did — unless
    `strict`: `load` and the shipped defaults read raised on a broken file
    before and still do, and a strict read also surfaces a file it cannot
    stat (BP review round 3) instead of treating it as absent. The parse
    error is cached beside the parse, so a broken file is parsed once per
    version whichever policy reads it — and raised as a fresh copy each
    time, since re-raising one stored exception grows its traceback by
    every raise and pins every frame it passed through (BP review r4).

    Returns a copy the caller owns: the memo and the call's snapshot are
    shared, and `deep_merge` hands nested lists and maps straight through,
    so a caller mutating a loaded config must not change the next load
    (BP review round 3).
    """
    key = path.resolve()
    snap = _active_snapshot()
    with snap.lock if snap is not None else contextlib.nullcontext():
        if snap is not None and key in snap.files:
            parsed, error = snap.files[key]
        else:
            try:
                parsed, error = _parse_versioned(key)
            except OSError:
                # Unstattable: a strict read surfaces it; a layer read skips the
                # layer, as `_read_yaml`'s is_file() did. Not snapshotted, so a
                # strict read later in the call still sees the error.
                if strict or _stat_ok(key):
                    raise
                return {}
            if snap is not None:
                snap.files[key] = (parsed, error)
    if error is not None and strict:
        raise copy.copy(error)
    return copy.deepcopy(parsed)


@lru_cache(maxsize=1)
def _shipped_section_cached(section: str) -> dict[str, Any]:
    return dict(read_yaml_cached(shipped_defaults_dir() / "project.yaml",
                                 strict=True).get(section) or {})


def shipped_limits() -> dict[str, Any]:
    """The `limits` block of the package's own project.yaml, never a layer's.

    A fresh copy each call: the cached dict underneath is shared across every
    caller, and one caller mutating what it got back must not leak into the
    next.
    """
    return dict(_shipped_section_cached("limits"))


def shipped_budget() -> dict[str, Any]:
    """The `budget` block of the package's own project.yaml, as `shipped_limits`
    reads `limits`. A fresh copy each call, for the same reason."""
    return dict(_shipped_section_cached("budget"))


def _section_number(section: dict, key: str, shipped: dict, zero_ok: bool = False) -> float:
    """A numeric config value, or its shipped default when it is not usable.

    The config is typed by a person: `120k`, `5m`, `inf`, NaN, a negative
    number or a boolean must fall back to what the package ships rather than
    raise out of a driver holding somebody's session — and not to 0, because
    for the thresholds 0 means "off" and a typo must not switch a safety
    feature off (P0-R8f.13). `zero_ok` is for the keys where 0 is a value
    (off, no retry, no wait); elsewhere 0 is malformed too.
    """
    default = shipped.get(key, 0)
    value = section.get(key, default)
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number) or number < 0 or (number == 0 and not zero_ok):
        return default
    return number


def limit_number(limits: dict, key: str, zero_ok: bool = False) -> float:
    """A numeric `limits` value, or its shipped default when it is not usable.

    See `_section_number`; `limits` is a person-typed section like every other.
    """
    return _section_number(limits, key, shipped_limits(), zero_ok=zero_ok)


def budget_number(budget: dict, key: str, zero_ok: bool = False) -> float:
    """A numeric `budget` value, read as safely as `limit_number` reads
    `limits` (P0-R8f.13) — for the burn-rate minimums, bug-c050b0: a malformed
    `burn_min_span_seconds` or `burn_min_samples` must fall back to the
    shipped default rather than raise or silently disable the gate.
    """
    return _section_number(budget, key, shipped_budget(), zero_ok=zero_ok)



# Copies of the shipped defaults are pinned in the global and project layers so
# they can be edited. That has a cost: a pinned copy overrides the newer shipped
# file for every key, including ones the user never touched, so improvements to
# the defaults silently never reach an existing install.
#
# The fix is to know which copies were edited. A manifest records the hash of
# each file as written; a copy still matching its hash was never touched and can
# be refreshed safely, while an edited one is left alone and merely reported.

MANIFEST = ".seeded.json"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_manifest(target: Path) -> dict:
    path = target / MANIFEST
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_manifest(target: Path, data: dict) -> None:
    (target / MANIFEST).write_text(json.dumps(data, indent=2, sort_keys=True))


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Compile one gitignore-style glob into a full-match regex.

    `*` and `?` stay inside a path segment, `**` crosses them, and a pattern
    with no `/` in it matches at any depth — so `conftest.py` covers
    `tests/conftest.py` the way anyone writing it would expect. A trailing `/`
    means the directory and everything under it.
    """
    pat = pattern.strip().removeprefix("./")
    if not pat:
        return re.compile(r"(?!)")                 # matches nothing
    if pat.endswith("/"):
        pat += "**"
    anchored = "/" in pat
    out: list[str] = []
    i = 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append(r"(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(r".*")
            i += 2
        elif pat[i] == "*":
            out.append(r"[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append(r"[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    body = "".join(out)
    if not anchored:
        body = r"(?:.*/)?" + body
    return re.compile(body + r"\Z")


@lru_cache(maxsize=512)
def _cached_regex(pattern: str) -> re.Pattern[str]:
    return _glob_regex(pattern)


def matches_any(patterns: Sequence[str], path: str) -> bool:
    """Is this repo-relative path selected by these globs?

    Gitignore rules, including negation: a leading `!` un-selects, and the LAST
    pattern that matches decides. So `["**", "!tests/**"]` reads as "everything
    except the tests" — which is how an agent whose product is new test files
    says it may not rewrite anything else.

    Order therefore matters, and a negation before the pattern it means to
    carve out does nothing. Write `\\!` for a filename that really begins with
    an exclamation mark.
    """
    candidate = str(path).strip().removeprefix("./")
    selected = False
    for raw in patterns:
        pattern = str(raw).strip()
        if not pattern:
            continue
        negated = pattern.startswith("!")
        if negated:
            pattern = pattern[1:].strip()
        elif pattern.startswith("\\!"):
            pattern = pattern[1:]
        if pattern and _cached_regex(pattern).match(candidate):
            selected = not negated
    return selected


def layer_files(source: Path, scope: str = "global") -> list[str]:
    """Relative names a layer should carry.

    The project layer deliberately carries less. Auth scripts and the
    orchestrator prompt are machine-level, and a stale per-project copy of
    either would be a liability rather than a convenience; they still resolve
    through the global layer, and a project may add its own if it wants.
    """
    names = [n for n in CONFIG_FILES if (source / n).is_file()]
    # Recursive: the briefs are foldered — `team/` for the roster that ships
    # active, `library/` for the predefined agents the initializer draws on.
    # A flat glob copied neither into the global or project layers, so a
    # library agent resolved only for whoever ran from the source checkout.
    agents_dir = source / "agents"
    names += [f"agents/{p.relative_to(agents_dir).as_posix()}"
              for p in sorted(agents_dir.rglob("*.md"))]
    if scope == "global":
        names += [n for n in STANDALONE_FILES if (source / n).is_file()]
        names += [f"providers/{p.name}" for p in sorted((source / "providers").glob("*"))
                  if p.is_file()]
    return names


def sync_layer(source: Path, target: Path, force: bool = False,
               dry_run: bool = False, scope: str = "global") -> dict[str, list[str]]:
    """Copy shipped files into a layer, refreshing only untouched copies."""
    report: dict[str, list[str]] = {"added": [], "updated": [], "customised": []}
    manifest = _read_manifest(target)

    for name in layer_files(source, scope):
        src, dst = source / name, target / name
        shipped = _digest(src)
        if not dst.is_file():
            report["added"].append(name)
            if not dry_run:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                _forget(dst)
                manifest[name] = shipped
            continue
        if _digest(dst) == shipped:
            manifest.setdefault(name, shipped)
            continue                                  # already current
        untouched = manifest.get(name) == _digest(dst)
        if untouched or force:
            report["updated"].append(name)
            if not dry_run:
                # Forcing over an EDITED file destroys work the user did by
                # hand. Keep a copy — the first --force in this codebase silently
                # reverted a project from the docker executor back to local.
                if not untouched:
                    backup = dst.with_suffix(dst.suffix + ".bak")
                    shutil.copy2(dst, backup)
                    report.setdefault("backed_up", []).append(str(backup))
                shutil.copy2(src, dst)
                _forget(dst)
                manifest[name] = shipped
        else:
            report["customised"].append(name)

    if not dry_run:
        # seed_global/seed_project mkdir their target first; a caller that
        # points sync_layer at a layer that does not exist yet — a project
        # whose .multiagents/ was never seeded — must still get its manifest.
        target.mkdir(parents=True, exist_ok=True)
        _write_manifest(target, manifest)
    return report


def seed_global(force: bool = False) -> Path:
    """Copy the package's shipped defaults into the global config dir."""
    target = global_config_dir()
    for sub in ("", "agents", "providers"):
        (target / sub).mkdir(parents=True, exist_ok=True)
    sync_layer(shipped_defaults_dir(), target, force=force)
    return target


def seed_project(paths: ProjectPaths, force: bool = False) -> Path:
    """Copy the global config into a project so it can be edited locally."""
    seed_global()
    target = paths.config
    for sub in ("", "agents"):
        (target / sub).mkdir(parents=True, exist_ok=True)
    sync_layer(global_config_dir(), target, force=force, scope="project")
    return target


@dataclass
class AgentSpec:
    """One entry from ``agents.yaml``."""

    name: str
    provider: str
    model: str
    # One brief, or several concatenated in the order given. A list exists for
    # ONE case: the orchestrator's brief is mostly team-independent (branches,
    # budget, questions, delegation) with a per-team pipeline on top, and
    # copying the common part per team would drift invisibly — the failure the
    # three coder tiers already share one brief to avoid.
    #
    # It is not for small shared sections. Two briefs that share forty lines of
    # craft advice should copy them: making a reader open three files to
    # understand one agent costs more than the duplication does.
    instructions: str | list[str] = ""
    description: str = ""
    effort: str | None = None
    permission: str = "full"          # full | sandbox | readonly
    can_spawn: bool = False
    max_children: int = 2
    timeout: int = 900                # wall-clock seconds
    silence_timeout: int = 180        # seconds with no stream event
    max_steps: int = 0                # 0 = use limits.max_steps
    writes: bool = True               # False -> branch dropped if it stays empty
    conversational: bool = False      # talked to via consult(), keeps context
    executor: str = ""                # "" = project default; else local | docker
    # LAUNCHED as an MCP client rather than spawned as a subagent. Real fields
    # rather than `extra` keys, because extras are coerced into command options
    # and `isinstance(True, int)` is True.
    # Per-provider fallback models: {provider: model_id}. A model id belongs to
    # its provider's namespace, so failing over without one would run
    # `agy --model opencode-go/glm-5.3-flash`. Named here, an agent can move to
    # another provider when its own is exhausted; without one it waits instead.
    # {provider: model_id} or {provider: {model: ..., effort: ...}}. The second
    # form exists because a model id is not the only thing that belongs to a
    # provider's namespace: `effort` does too. An agent pinned effort:high
    # failed over to another vendor, kept its effort, and the CLI refused the
    # combination in eight seconds — "--effort is not supported for model
    # claude-opus-4-6-thinking". The move was right and the options came with
    # it uninvited.
    models: dict[str, Any] = field(default_factory=dict)
    handover: bool = True
    priorities: list | None = None
    quota_handover: dict[str, Any] = field(default_factory=dict)
    launch: bool = False
    # Paths this agent may not MODIFY, as gitignore-style globs. Adding a new
    # file is always allowed; changing, deleting or renaming a matching one is
    # reverted before its branch merges. `None` means "use the project default"
    # (limits.readonly_paths); `[]` opts out, which is how the agent that OWNS
    # those files — the test engineer — is exempted.
    #
    # This is enforced at the merge gate rather than in the filesystem, and it
    # is a deliberate choice rather than a shortcut: the container runs as the
    # invoking uid so an agent can chmod its own files back, and bind mounts
    # are fixed when the container is created, so neither is per-agent. The
    # merge gate runs in the parent's process, outside the worktree, and an
    # agent cannot merge itself — see `_may_act_on`.
    readonly_paths: list[str] | None = None
    # Which command launches it: "orchestrator" for `run`, "initializer" for
    # `init-agent`. Both are launch: true; the role says which door they use.
    role: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    # LM-R1c: the fields the agent's config actually SETS, read off the merged
    # yaml before it becomes a dataclass. Without it `timeout: 900` written by
    # hand and the 900 default are the same value, and a project's `limits:`
    # could never tell which one to override. `None` (a spec built in code)
    # counts every field that differs from its default as set.
    set_fields: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if self.set_fields is None:
            defaults = {f.name: f.default for f in fields(self)
                        if f.default is not MISSING}
            self.set_fields = frozenset(
                name for name, default in defaults.items()
                if name != "set_fields" and getattr(self, name) != default)
        else:
            self.set_fields = frozenset(self.set_fields)

    def sets(self, name: str) -> bool:
        """Whether the agent's config sets `name` rather than inheriting it."""
        return name in (self.set_fields or ())

    def replace(self, **changes: Any) -> AgentSpec:
        """A copy with `changes` applied, which then count as set: a fallback's
        `{model: ..., timeout: 600}` is the agent's own value on that route."""
        return AgentSpec(**{**self.__dict__, **changes,
                            "set_fields": frozenset(self.set_fields or ()) | set(changes)})

    def fallback_for(self, provider: str) -> tuple[str, dict[str, Any]]:
        """What this agent becomes on another provider: `(model, overrides)`.

        A bare string names the model and says nothing about options, so the
        options it was configured with travel unchanged — deliberately, because
        silently dropping `effort: high` from an agent that exists BECAUSE it
        reasons deeply is a slow, quiet failure, and a refused flag is a loud
        one that takes eight seconds. Say `{model: ..., effort: ""}` to mean
        something else.
        """
        entry = (self.models or {}).get(provider)
        if isinstance(entry, dict):
            model = str(entry.get("model") or entry.get("id") or "")
            fields = set(AgentSpec.__dataclass_fields__)
            overrides = {k: v for k, v in entry.items()
                         if k in fields and k not in ("name", "provider", "model",
                                                      "models", "set_fields")}
            return model, overrides
        return str(entry or ""), {}

    def routed(self, provider: str, pinned_model: str = "") -> AgentSpec:
        """The per-run spec on `provider`: the `models.P` entry merged over the
        top-level values, without mutating this spec (FO-R1).

        A dict entry contributes both its dataclass fields (`effort`,
        `permission`, …) and every option it names (`variant`, or any key a
        provider's `spawn.optional` consumes). Options are not dataclass
        fields, so they live in `extra` and reach the command line through the
        launch options. An explicit `pinned_model` — a per-run `model=` pin —
        wins over the entry's model but not over its options. A bare-string
        entry changes only the model (FO-R2).
        """
        entry = (self.models or {}).get(provider)
        if not isinstance(entry, dict):
            model = pinned_model or (str(entry) if entry else "")
            return self.replace(model=model) if model and model != self.model else self
        fields = set(AgentSpec.__dataclass_fields__)
        # `provider` is a dataclass field, but a `models:` entry may not move
        # the run to another provider — the key names the destination. It is
        # excluded here (and reported by `_warn_unknown_entry_keys`).
        changes = {k: v for k, v in entry.items()
                   if k in fields
                   and k not in ("name", "provider", "model", "models",
                                 "set_fields", "extra")}
        extra = {**self.extra,
                 **{k: v for k, v in entry.items()
                    if k not in fields and k not in ("model", "id")}}
        model = (pinned_model
                 or str(entry.get("model") or entry.get("id") or "")
                 or self.model)
        return self.replace(model=model, extra=extra, **changes)

    @classmethod
    def from_dict(cls, name: str, data: dict) -> AgentSpec:
        known = {f for f in cls.__dataclass_fields__ if f not in ("extra", "set_fields")}
        kwargs = {k: v for k, v in data.items() if k in known}
        extra = {k: v for k, v in data.items() if k not in known}
        return cls(name=name, extra=extra, set_fields=frozenset(kwargs) - {"name"},
                   **{k: v for k, v in kwargs.items() if k != "name"})


def priority_entries(spec: AgentSpec, providers: dict) -> list[tuple[list[str], str]]:
    """QH-R23/R30: resolve syntax before strategy orders each family."""
    from .providers import families
    groups = families(providers)
    result = []
    seen = set()
    if not isinstance(spec.priorities, list) or not spec.priorities:
        raise ValueError(f"agent {spec.name}: priorities must be a nonempty list")
    for entry in spec.priorities:
        model = ""
        if isinstance(entry, str):
            name, family = entry, entry in groups
        elif isinstance(entry, dict) and ("family" in entry) != ("instance" in entry):
            family = "family" in entry
            name = entry["family" if family else "instance"]
            model = entry.get("model", "")
            if not isinstance(model, str):
                raise ValueError(f"agent {spec.name}: priorities model must be a string")
        else:
            raise ValueError(f"agent {spec.name}: invalid priorities entry {entry!r}")
        if not isinstance(name, str) or name not in (groups if family else providers):
            raise ValueError(f"agent {spec.name}: unknown priority {'family' if family else 'instance'} {name!r}")
        names = groups[name] if family else [name]
        for instance in names:
            if instance in seen:
                raise ValueError(f"agent {spec.name}: duplicate priority instance {instance!r}")
            seen.add(instance)
        result.append((names, model))
    return result


def validate_promotion_settings(settings):
    if not isinstance(settings, dict):
        raise ValueError("quota_handover must be a mapping")
    for key in ("promote_min_headroom", "promote_min_dwell_seconds",
                "promote_grace_seconds", "promote_check_seconds"):
        if key not in settings:
            continue
        value = settings[key]
        if key == "promote_min_headroom":
            valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                     and math.isfinite(value) and 0 <= value < 1)
        else:
            valid = (not isinstance(value, bool) and isinstance(value, int)
                     and value >= (1 if key == "promote_check_seconds" else 0))
        if not valid:
            raise ValueError(f"quota_handover.{key} has an invalid value")


# LM-R1: the run limits an agent may set for itself, the `limits:` key each
# falls back to, and the built-in value when neither is set.
LIMIT_FIELDS: dict[str, tuple[str, int]] = {
    "timeout": ("default_timeout", 900),
    "max_children": ("max_children", 2),
    "silence_timeout": ("silence_timeout", 180),
}


def _positive(value: Any) -> int | float | None:
    """A usable limit, or None: person-typed, so `5m`, a boolean, NaN or a
    non-positive number is not a value and the next source applies."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return int(number) if number == int(number) else number


@dataclass
class Config:
    project: dict[str, Any]
    providers: dict[str, Any]
    agents: dict[str, AgentSpec]
    models: dict[str, Any]
    instruction_dirs: list[Path]
    # LN-C2: the directories the layers above were read from, lowest first,
    # so a limit notice can name the file and line a value came from. Empty
    # for a Config built in code, which has no file to point at.
    layers: list[Path] = field(default_factory=list)
    # FO-R3: config warnings raised while loading, so a caller can surface
    # them. An unknown option in a `models:` entry is reported here rather
    # than failing the load; dedup, if any, is at display (FO-R3b).
    warnings: list[str] = field(default_factory=list)

    # TG-R1: the layer/file that supplied each provider spawn key.
    provider_sources: dict[str, dict[str, str]] = field(default_factory=dict)

    # --- convenience accessors, all with defaults so a sparse config works ---

    @property
    def remote(self) -> str:
        return self.project.get("git", {}).get("remote", "") or ""

    @property
    def base_branch(self) -> str:
        return self.project.get("git", {}).get("base_branch", "") or ""

    @property
    def branch_prefix(self) -> str:
        return self.project.get("git", {}).get("branch_prefix", "agents") or "agents"

    @property
    def push_agent_branches(self) -> bool:
        return bool(self.project.get("git", {}).get("push_agent_branches", False))

    @property
    def executor(self) -> str:
        return self.project.get("executor", {}).get("kind", "local")

    @property
    def env_passthrough(self) -> list[str]:
        return list(self.project.get("security", {}).get("env_passthrough", []))

    @property
    def env_block(self) -> list[str]:
        return list(self.project.get("security", {}).get("env_block", []))

    @property
    def home_policy(self) -> str:
        return self.project.get("security", {}).get("home_policy", "per-agent")

    @property
    def limits(self) -> dict[str, Any]:
        return self.project.get("limits", {})

    # ------------------------------------------------------------- teams --
    #
    # A team is a pipeline: an orchestrator brief and the roster that runs it.
    # A project is in one team at a time — auditing an existing system and
    # building new work are different jobs and want differently shaped rosters.
    #
    # The roster is listed per TEAM rather than per agent on purpose. "What is
    # the review team" is asked far more often than "which teams is the tester
    # in", and the first question should be answerable by reading one block
    # instead of grepping every agent definition.

    @property
    def teams(self) -> dict[str, Any]:
        return self.project.get("teams", {}) or {}

    @property
    def team(self) -> str:
        """The active team. Shipped defaults carry `team: implement`, so a
        project written before teams existed resolves to today's behaviour
        through the ordinary layer merge rather than needing a migration."""
        return str(self.project.get("team", "") or "")

    def team_spec(self, team: str = "") -> dict[str, Any]:
        return self.teams.get(team or self.team, {}) or {}

    def team_roster(self, team: str = "") -> list[str]:
        """Agent names this team may spawn. Empty list means no restriction."""
        return [str(n) for n in (self.team_spec(team).get("roster") or [])]

    def in_team(self, name: str, team: str = "") -> bool:
        """May this agent be spawned by the active team?

        A team with no roster declared restricts nothing, which is what keeps a
        project that has never heard of teams working exactly as before.
        """
        roster = self.team_roster(team)
        return not roster or name in roster

    def agent(self, name: str) -> AgentSpec:
        if name not in self.agents:
            raise KeyError(f"No agent named {name!r}. Configured: {sorted(self.agents)}")
        return self.agents[name]

    def readonly_paths_for(self, spec: AgentSpec) -> list[str]:
        """Globs this agent may not modify.

        An agent's own list REPLACES the project default rather than adding to
        it — the same rule the config layers use for lists, because a partially
        overridden list is never what anyone means. `[]` is therefore a real
        answer ("this agent may touch them") and not an absent one.
        """
        if spec.readonly_paths is not None:
            return [str(p) for p in spec.readonly_paths]
        default = self.limits.get("readonly_paths") or []
        return [str(p) for p in default]

    def effective_limits(self, spec: AgentSpec | None,
                         timeout: float | None = None) -> dict[str, dict[str, Any]]:
        """LM-R1/R2: each run limit as `{value, source}`, the first of an
        explicit per-call value (`timeout` only), the agent's own setting, the
        project's `limits:` and the built-in default.

        `limits:` here is the merged project, global and shipped layers. A
        nested runner in the container sees the same mounted project config and
        so resolves the same values; a `limits:` override made only in the
        host's global config directory is not mounted and applies on the host
        only (LM-R1c). `spec=None` resolves from `limits:` alone, for a parent
        whose agent is no longer configured.
        """
        out: dict[str, dict[str, Any]] = {}
        for name, (key, builtin) in LIMIT_FIELDS.items():
            call = _positive(timeout) if name == "timeout" else None
            own = _positive(getattr(spec, name)) if spec and spec.sets(name) else None
            project = _positive(self.limits.get(key))
            if call is not None:
                out[name] = {"value": call, "source": "call"}
            elif own is not None:
                out[name] = {"value": own, "source": "agent"}
            elif project is not None:
                out[name] = {"value": project, "source": "project"}
            else:
                out[name] = {"value": builtin, "source": "builtin"}
        return out

    def instruction_parts(self, spec: AgentSpec) -> list[str]:
        """The brief files this agent names, as a list even when there is one."""
        if not spec.instructions:
            return []
        if isinstance(spec.instructions, str):
            return [spec.instructions]
        return [str(part) for part in spec.instructions if str(part).strip()]

    def missing_instructions(self, spec: AgentSpec) -> list[str]:
        """Named brief files that resolve to nothing, in the order given.

        Checked separately from the text because a brief may be composed of
        several files: if one resolves and one does not, the joined result is
        non-empty and a plain truthiness test would call that fine. It is not —
        an orchestrator running on its common core with no pipeline section is
        an agent with no idea what it is doing, which is the expensive kind of
        wrong.
        """
        return [part for part in self.instruction_parts(spec)
                if not self._resolve_instruction(part).strip()]

    def calling_contract(self, spec: AgentSpec) -> str:
        """The part of this agent's brief addressed to its caller, if any."""
        return _split_calling(self.instructions_for(spec))[1]

    def instructions_for(self, spec: AgentSpec) -> str:
        """Resolve an agent's brief across the config layers, in order.

        Project instructions win over global ones, so you can rewrite a shipped
        agent's brief without touching the machine-wide copy.
        """
        parts = [self._resolve_instruction(part) for part in self.instruction_parts(spec)]
        return "\n\n".join(part.strip() for part in parts if part.strip())

    def _resolve_instruction(self, name: str) -> str:
        spec = AgentSpec(name="", provider="", model="", instructions=name)
        return self._resolve_one(spec)

    def _resolve_one(self, spec: AgentSpec) -> str:
        if not spec.instructions:
            return ""
        candidate = Path(spec.instructions).expanduser()
        if candidate.is_absolute() and candidate.is_file():
            return candidate.read_text()
        # The mandatory briefs are named with a leading underscore so they are
        # visibly not yours to delete. A config written before that convention
        # names them without it, and an install can carry both files at once
        # mid-upgrade — so the exact name always wins, and the other spelling
        # is only a fallback.
        stem = Path(spec.instructions)
        other = stem.name[1:] if stem.name.startswith("_") else "_" + stem.name
        names = [spec.instructions, (stem.parent / other).as_posix()]
        for name in names:
            for base in self.instruction_dirs:
                path = base / name
                if path.is_file():
                    return path.read_text()
        # Last resort: match on the basename anywhere under agents/. The briefs
        # moved into team/ and library/ subfolders, and a config written before
        # that says `tester.md` where the file is now `team/tester.md`. Without
        # this an upgrade turns every such entry into a missing brief, which
        # _preflight refuses outright.
        #
        # Only for a BARE filename. A name that already carries a directory
        # means that directory: `teams/nope/pipeline.md` must not quietly
        # resolve to `teams/implement/pipeline.md` because the basenames match.
        # A mistyped team would then load another team's pipeline and run it
        # with complete confidence — which is worse than the missing brief this
        # fallback exists to avoid.
        if stem.parent != Path("."):
            return ""
        for name in names:
            leaf = Path(name).name
            for base in self.instruction_dirs:
                if not base.is_dir():
                    continue
                for path in sorted(base.rglob(leaf)):
                    if path.is_file():
                        return path.read_text()
        return ""


# FO-R3: a `models:` entry may carry structural keys (`model`, `id`) and any
# option the DESTINATION provider consumes via its resolved `spawn.optional`.
# `provider` is a dataclass field but may not be set here — the key names the
# destination, it does not choose one — so it is reported like any other key.
# FO-R3b: the warning is deduplicated at DISPLAY, never here. Every Config
# carries all of its warnings, so a reload (the MCP server reloads on every
# tool call) still surfaces them; there is no log/stderr emitter today for a
# process-global set to protect.
def _warn_unknown_entry_keys(agents_raw: dict, providers: dict,
                             warnings: list[str]) -> None:
    """FO-R3: report a `models:` key nothing can consume.

    Valid: a dataclass field (except `provider`), `model`/`id`, or an option
    the DESTINATION provider consumes via its resolved `spawn.optional`. A
    shipped and a project-defined provider are treated alike: a key is only
    valid where that provider actually renders it. Anything else names the
    agent, the provider and the key, and the load carries on — a mistyped
    option is a warning, not a broken roster.
    """
    for name, data in agents_raw.items():
        spec = AgentSpec.from_dict(name, data or {})
        for provider, entry in (spec.models or {}).items():
            if not isinstance(entry, dict):
                continue
            dest = providers.get(provider)
            consumed = set((dest.spawn.get("optional") or {})) if dest else set()
            for key in entry:
                # FO-R3a: `provider` is always reported, even when P's own
                # `spawn.optional` happens to declare a `provider` placeholder.
                # The key names the destination; it never chooses one.
                if key != "provider" and (
                        key in ("model", "id")
                        or key in AgentSpec.__dataclass_fields__
                        or key in consumed):
                    continue
                warnings.append(
                    f"agent {name!r}: `models.{provider}` names {key!r}, which "
                    f"is neither a field it may set nor an option "
                    f"{provider!r} consumes; it will be ignored")


def layer_dirs(paths: ProjectPaths | None) -> list[Path]:
    layers = [shipped_defaults_dir(), global_config_dir()]
    if paths is not None and paths.config.is_dir():
        layers.append(paths.config)
    return layers


def source_version(paths: ProjectPaths | None) -> tuple:
    """Cheap staleness check for consumers retaining a validated Config."""
    versions = []
    for layer in layer_dirs(paths):
        for name in CONFIG_FILES:
            path = layer / name
            try:
                stat = path.stat()
                stamp = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
            except FileNotFoundError:
                stamp = None
            versions.append((path, stamp))
    return tuple(versions)


def read_project_layer(layer: Path, scheduler_errors: list[str] | None = None) -> dict:
    """Validate scheduler policy at its source; doctor may collect bad keys."""
    data = read_yaml_cached(layer / "project.yaml", strict=True)
    if "scheduler" not in data:
        return data
    from .notices import _node_at
    from .scheduler_config import SchedulerConfigError, validate_setting
    block = data["scheduler"]
    if not isinstance(block, dict):
        location = _node_at(layer / "project.yaml", ["scheduler"])
        error = SchedulerConfigError(
            f"{layer / 'project.yaml'}:{location[0] if location else 1} scheduler: expected mapping")
        if scheduler_errors is None:
            raise error
        scheduler_errors.append(str(error))
        del data["scheduler"]
    else:
        for key, value in list(block.items()):
            try:
                validate_setting(key, value)
            except SchedulerConfigError as exc:
                location = _node_at(layer / "project.yaml", ["scheduler", key])
                line = location[0] if location else 1
                error = SchedulerConfigError(f"{exc} ({layer / 'project.yaml'}:{line})")
                if scheduler_errors is None:
                    raise error from exc
                scheduler_errors.append(str(error))
                del block[key]
    return data


def load_project_section(paths: ProjectPaths | None, section: str) -> dict:
    """Load a project section through the same layers and validation as load.

    Gate checks must not construct providers or validate unrelated settings:
    legacy mount and launch callers did not do that before Phase 7.
    """
    result = {}
    with parse_once():
        for layer in layer_dirs(paths):
            result = deep_merge(result, read_project_layer(layer).get(section, {}))
    return result


def load(paths: ProjectPaths | None, seed: bool = True, *,
         instance_strategy_errors: list[str] | None = None,
         scheduler_errors: list[str] | None = None) -> Config:
    """Load the merged configuration for a project (or the global one alone).

    `seed=False` only reads: a missing layer is an empty one. That is how a
    subagent's server loads — see `server._runner_locked`.
    """
    if seed:
        seed_global()
    layers = layer_dirs(paths)

    merged: dict[str, dict] = {name: {} for name in CONFIG_FILES}
    provider_sources: dict[str, dict[str, str]] = {}
    from .notices import _node_at
    # BP-R1: through the shared parse cache and, inside a budget read, that
    # read's snapshot — so the read's helpers and this load see one version
    # of each file and parse it once between them (BP review rounds 2, 3).
    # Strict: a malformed or unstattable file raised out of load before and
    # still does — only the budget layer reads degrade to {}.
    with parse_once():
        for layer in layers:
            for name in CONFIG_FILES:
                data = (read_project_layer(layer, scheduler_errors) if name == "project.yaml"
                        else read_yaml_cached(layer / name, strict=True))
                strategy_blocks = []
                if name == "project.yaml":
                    strategy_blocks = [(data.get("budget") or {}, ["budget"], False)]
                elif name == "providers.yaml":
                    strategy_blocks = [(block or {}, ["providers", provider_name], True)
                                       for provider_name, block in
                                       (data.get("providers") or {}).items()]
                for block, parts, nullable in strategy_blocks:
                    if "instance_strategy" not in block:
                        continue
                    location = _node_at(layer / name, [*parts, "instance_strategy"])
                    line = location[0] if location else 1
                    try:
                        validate_strategy(block["instance_strategy"], nullable=nullable,
                                          source=f"{layer / name}:{line} {'.'.join(parts)}")
                    except InstanceStrategyError as exc:
                        if instance_strategy_errors is None:
                            raise
                        instance_strategy_errors.append(str(exc))
                        del block["instance_strategy"]
                merged[name] = deep_merge(merged[name], data)
                if name == "providers.yaml":
                    scope = ("shipped" if layer == layers[0] else
                             "global" if layer == layers[1] else "project")
                    for provider_name, block in (data.get("providers") or {}).items():
                        for key in ((block or {}).get("spawn") or {}):
                            location = _node_at(
                                layer / name, ["providers", provider_name, "spawn", key])
                            line = f":{location[0]}" if location else ""
                            provider_sources.setdefault(provider_name, {})[key] = (
                                f"{scope} layer {layer / name}{line}")

    providers_raw = merged["providers.yaml"].get("providers", {}) or {}
    # PS-R1/R5/R6: the providers (and their sharing keys) are built and
    # validated here, so a config error surfaces at load, and the roster's
    # routes are checked against the allowlists of the providers they name.
    providers = load_providers(providers_raw)
    # QH-R1/R2: validate even when disabled, before any admission occurs.
    handover = merged["project.yaml"].get("quota_handover") or {}
    if not isinstance(handover, dict):
        raise ValueError("quota_handover must be a mapping")
    if not isinstance(handover.get("enabled", True), bool):
        raise ValueError("quota_handover.enabled must be a boolean")
    validate_promotion_settings(handover)
    reserved = handover.get("reserved_instance")
    if reserved is not None and reserved not in providers:
        raise ValueError(f"quota_handover.reserved_instance {reserved!r} must name an existing instance")
    fraction = handover.get("reserve_fraction", 0.25)
    if (isinstance(fraction, bool) or not isinstance(fraction, (int, float))
            or not 0 < fraction < 1):
        raise ValueError("quota_handover.reserve_fraction must be in (0, 1)")
    window = handover.get("veto_window_seconds", 120)
    if isinstance(window, bool) or not isinstance(window, int) or window < 0:
        raise ValueError("quota_handover.veto_window_seconds must be an integer >= 0")
    for name, provider in providers.items():
        if (not isinstance(provider.handover_mode, dict)
                or any(context not in ("local", "docker") or mode not in ("shared", "copy", "none")
                       for context, mode in provider.handover_mode.items())):
            raise ValueError(f"provider {name}: invalid handover_mode")
    agents_raw = merged["agents.yaml"].get("agents", {}) or {}
    agents = {
        name: AgentSpec.from_dict(name, data or {})
        for name, data in agents_raw.items()
        if not (data or {}).get("disabled")
    }
    for spec in agents.values():
        validate_promotion_settings(spec.quota_handover)
        if spec.priorities is not None:
            priority_entries(spec, providers)
    _validate_routes(agents_raw, providers)
    warnings: list[str] = []
    _warn_unknown_entry_keys(agents_raw, providers, warnings)
    executor = (merged["project.yaml"].get("executor") or {}).get("kind", "local")
    from .providers import expand_env_value
    for name, provider in providers.items():
        required = provider.home_account_executor
        home = (provider.credential_env or provider.env).get("HOME")
        if required and executor != required and home \
                and expand_env_value(home) != str(Path.home()):
            warnings.append(
                f"provider {name!r}: under the {executor} executor, relocating "
                f"HOME still uses the primary account; distinct HOME profiles "
                f"require the {required} executor")

    return Config(
        project=merged["project.yaml"],
        providers=providers_raw,
        agents=agents,
        models=merged["models.yaml"].get("models", {}) or {},
        instruction_dirs=[layer / "agents" for layer in reversed(layers)],
        layers=layers,
        warnings=warnings,
        provider_sources=provider_sources,
    )


def _validate_routes(agents_raw: dict, providers: dict) -> None:
    """PS-R6: every route a roster names must run on a provider that allows it.

    A route is the agent's own provider+model or one `models:` entry. The
    error names the agent, the route, the model and — when one exists — a
    provider whose allowlist would take the model, so the fix is legible
    without hunting. Disabled agents are checked too: a parked agent with a
    broken route is still config that cannot work (PS-R10).
    """
    for name, data in agents_raw.items():
        spec = AgentSpec.from_dict(name, data or {})
        routes = [(spec.provider, spec.model)]
        routes += [(route, spec.fallback_for(route)[0])
                   for route in (spec.models or {})]
        for route_provider, model in routes:
            if not model or route_provider not in providers:
                continue
            if providers[route_provider].allows_model(model):
                continue
            acceptors = sorted(
                other for other, provider in providers.items()
                if other != route_provider and provider.allows_model(model))
            way_out = (f" {acceptors[0]} allows it" if len(acceptors) == 1
                       else f" These do: {', '.join(acceptors)}"
                       if acceptors else
                       " No declared provider's allowlist accepts it")
            raise ValueError(
                f"agent {name!r}: route {route_provider} names model "
                f"{model!r}, which {route_provider} does not allow."
                f"{way_out}. Fix agents.yaml: give the route a model the "
                f"provider allows, or route it to a provider that does.")
