"""``multiagents`` — setup and housekeeping outside the MCP layer.

Some things are not agent work: first-time setup, refreshing the model list
after a subscription change, checking that the CLIs are installed and
authenticated, cleaning up worktrees after a crash. Doing those through an MCP
tool would mean starting a Claude session for housekeeping — and would leave you
with no way to diagnose the system when the MCP layer itself is what is broken.
"""

from __future__ import annotations

import signal
import asyncio
import argparse
import contextlib
import dataclasses
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import auth as auth_mod
from . import bugs
from . import catalog as catalog_mod
from . import driver
from . import procs
from .executor import executor_for
from . import scripts
from . import gitops
from . import plans
from .budget import read_all, reset_label
from .config import load as load_config
from .config import seed_global, seed_project, sync_layer
from .drift import find_shadowing
from .models import refresh_models, validate_agent_models
from .paths import (ProjectPaths, find_project_root, global_config_dir,
                    known_projects, register_project, state_root)
from .providers import billed_rows, load_providers
from .renames import shipped as shipped_renames
from .runner import Runner, reap_pending_branches
from .authority import HostAuthority
from .tree import ACTIVE, Tree

GITIGNORE_LINE = ".multiagents/"


def _explicit_root(explicit: str) -> Path:
    """The project root an explicit `--path` names, or exit 2 saying why not.

    The ONE place an explicit `--path` is interpreted, so no caller can bypass
    the rules: a path that does not exist is refused BEFORE the upward search
    (a typo under a project must not be adopted as its root, and write
    commands must not create it); so is a path that is not a directory, and an
    explicitly empty one; a path inside a project resolves upward to that
    project's root, exactly as the cwd does; and a path naming no project at
    all is a user error, reported like every other one — message naming the
    path on stderr, exit 2. Pure: commands that never use a project still
    call it, so a bad `--path` fails everywhere without writing anything.
    """
    if explicit == "":
        print(f"{explicit!r} does not name a directory.", file=sys.stderr)
        raise SystemExit(2)
    start = Path(explicit).expanduser().resolve()
    if not start.exists():
        print(f"{explicit} does not exist.", file=sys.stderr)
        raise SystemExit(2)
    if not start.is_dir():
        print(f"{explicit} is not a directory.", file=sys.stderr)
        raise SystemExit(2)
    root = find_project_root(start)
    if root is None:
        print(f"No .multiagents/ found at {explicit} or above. "
              "Run `multiagents init` first.", file=sys.stderr)
        raise SystemExit(2)
    return root


def _resolve(explicit: str | None = None) -> ProjectPaths:
    """The project a command runs against: `--path` when given, the cwd otherwise.

    An explicit `--path` goes through `_explicit_root`. `None` (no `--path`)
    keeps the historical behaviour: search upward from the cwd, refuse when
    there is no project.
    """
    if explicit is None:
        root = find_project_root()
        if root is None:
            print("No .multiagents/ found here or above. Run `multiagents init` first.",
                  file=sys.stderr)
            raise SystemExit(2)
    else:
        root = _explicit_root(explicit)
    paths = ProjectPaths(root)
    # Refreshed on every command that names a project rather than only at init,
    # so `docker status --all` can resolve projects created before the registry
    # existed. A no-op once the entry is current.
    register_project(paths.root)
    return paths




def _resolve_if_project(explicit: str | None = None) -> ProjectPaths | None:
    """`_resolve`, or None when there is no project to resolve.

    Every rule about an explicit `--path` lives in `_explicit_root` — this
    only adds the None contract these subcommands need: with no `--path` and
    no project at the cwd they run against global config alone instead of
    refusing.
    """
    if explicit is None:
        return _resolve(None) if find_project_root() else None
    return _resolve(explicit)


# --------------------------------------------------------------------------


def _read_key(stream) -> str:
    """Read and decode exactly one key from a raw terminal stream.

    `stream` may be binary or text — whichever `sys.stdin` is when a caller
    puts the terminal into raw mode. Every call consumes one whole key, so
    nothing left over can register as the next one: an `ESC [` sequence other
    than up/down is read through its final byte (`@`-`~`) and yields `""`
    (unknown, ignored by `_select`); a lone `ESC` — end of stream right after
    it, or anything but `[` — is consumed along with what follows it and
    cancels; end of stream with nothing read at all also cancels.

    Shares its key-decoding table with `_read_key_fd`, which reads the same
    way off a real terminal fd instead of an in-memory stream — see that
    function for why the two aren't just one.
    """
    def get(timeout=None) -> str:
        chunk = stream.read(1)
        return chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk

    return _decode_key(get)


def _decode_key(get, esc_timeout=None) -> str:
    """Shared token table behind `_read_key` and `_read_key_fd`.

    `get(timeout=None)` fetches one decoded character, blocking unless the
    caller gives `timeout` a meaning of its own. Only the byte immediately
    after `ESC` is ever asked for with `esc_timeout` — that is the one place
    a real terminal reader can be left hanging by a lone `Escape` press.
    """
    ch = get()
    if ch == "":
        return "cancel"
    if ch == "\x1b":
        nxt = get(esc_timeout)
        if nxt != "[":
            return "cancel"
        final = ""
        while True:
            final = get()
            if final == "" or "@" <= final <= "~":
                break
        return {"A": "up", "B": "down"}.get(final, "")
    if ch == "k":
        return "up"
    if ch == "j":
        return "down"
    if ch in ("\r", "\n"):
        return "enter"
    if ch in ("q", "\x03"):
        return "cancel"
    return ch


def _read_key_fd(fd, timeout: float = 0.05) -> str:
    """Like `_read_key`, but reads a raw terminal fd directly.

    A lone `ESC` — no `[` arriving within `timeout` seconds — cancels
    immediately instead of leaving the picker waiting for a second key that
    isn't coming (P0-R7.13 amendment). The timeout is implemented with
    `select` on the fd, which only means what it says if the read behind it
    is a raw, unbuffered `os.read` on that same fd — going through
    `sys.stdin`'s buffered text wrapper could report a byte as "not ready"
    when it is in fact already sitting in the wrapper's own buffer.
    """
    import select

    def get(to=None) -> str:
        if to is not None:
            ready, _, _ = select.select([fd], [], [], to)
            if not ready:
                return ""
        chunk = os.read(fd, 1)
        return chunk.decode("utf-8", "replace") if chunk else ""

    return _decode_key(get, timeout)


def _select(options: list[tuple[str, str]], current: str, read_key) -> str | None:
    """Cursor-pick a name out of `(name, description)` pairs.

    `read_key()` drives it: `"up"`/`"down"` move the selection and stop at
    either end (no wrap-around), `"enter"` returns the name under the cursor,
    `"cancel"` returns `None`, and anything else is ignored. `current` is
    selected on entry, or the first option if `current` is not among them.

    No terminal is touched here — that is `_read_key` and its caller's job —
    which is what lets this run against a scripted `read_key` in tests.
    """
    if not options:
        return None
    names = [name for name, _ in options]
    idx = names.index(current) if current in names else 0
    _draw_team_options(options, idx)
    while True:
        key = read_key()
        if key == "up" and idx > 0:
            idx -= 1
            _draw_team_options(options, idx, redraw=True)
        elif key == "down" and idx < len(options) - 1:
            idx += 1
            _draw_team_options(options, idx, redraw=True)
        elif key == "enter":
            return names[idx]
        elif key == "cancel":
            return None
        # any other token is unrecognised and ignored


def _draw_team_options(options: list[tuple[str, str]], idx: int,
                        redraw: bool = False) -> None:
    """Print the team list, with `>` marking the current selection."""
    if redraw:
        sys.stdout.write(f"\x1b[{len(options)}A")
    for i, (name, description) in enumerate(options):
        marker = "> " if i == idx else "  "
        sys.stdout.write(f"\x1b[2K{marker}{name:<16} {description}\n")
    sys.stdout.flush()


def _pick_team(options: list[tuple[str, str]], current: str) -> str | None:
    """Run `_select` against the real keyboard.

    Raw mode is entered lazily, on the first keystroke `_select` actually
    asks for, and restored in a `finally` on every exit path (P0-R7.13). That
    laziness is also what keeps a test that replaces `_select` from ever
    reaching the terminal at all: the real key reader is verified on its own,
    against in-memory streams, never through here.

    `tty.setcbreak` rather than `setraw`: raw mode also turns off output
    post-processing, and once that's off every `\n` `_draw_team_options`
    writes stops returning the carriage, so the redrawn list staircases down
    the screen instead of landing back in place. cbreak leaves OPOST on and
    only takes input out of canonical, echoed, line-buffered mode, which is
    all a key-at-a-time reader needs. It leaves ISIG on too, so Ctrl-C
    surfaces as `KeyboardInterrupt` rather than an `\x03` byte — caught below
    and treated the same as any other cancel.
    """
    saved = None
    fd = None

    def read_key() -> str:
        nonlocal saved, fd
        if saved is None:
            import termios
            import tty
            fd = sys.stdin.fileno()
            saved = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        return _read_key_fd(fd)

    # SIGTERM becomes an exit rather than a sudden death, so the `finally`
    # still puts the terminal back (P0-R8f.10). Not caught below: the process
    # was asked to end, not the picker to cancel.
    def terminated(signum, _frame):
        raise SystemExit(128 + signum)

    try:
        previous = signal.signal(signal.SIGTERM, terminated)
    except (ValueError, OSError):
        previous = None
    try:
        return _select(options, current, read_key)
    except KeyboardInterrupt:
        return None
    finally:
        if saved is not None:
            import termios
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            except Exception:
                pass
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def _choose_team(paths, config, requested: str | None):
    """Resolve, and if needed apply, the team `init-agent` runs as.

    Returns `(config, None)` to carry on with (possibly reloaded) `config`,
    or `(config, code)` to stop with that exit code — so `cmd_init_agent`
    stays a straight line instead of threading early returns through checks
    that must never run after a cancel (P0-R7.4) or an unknown `--team`.
    """
    teams = config.teams
    if requested is not None:
        if requested not in teams:
            print(f"unknown team {requested!r}; configured: "
                  f"{', '.join(teams) or '(none configured)'}", file=sys.stderr)
            return config, 2
        chosen, prompted = requested, False
    elif sys.stdin.isatty() and len(teams) >= 2:
        options = [(name, str((spec or {}).get("description", "")))
                   for name, spec in teams.items()]
        chosen = _pick_team(options, config.team)
        if chosen is None:
            print("             nothing changed")
            return config, 130
        prompted = True
    else:
        chosen, prompted = config.team, False

    if chosen != config.team:
        if not _set_team(paths, chosen):
            print(f"             no project.yaml to write the team into: "
                  f"{paths.config / 'project.yaml'}")
            return config, 2
        config = load_config(paths)

    suffix = "" if (prompted or requested is not None) else " (configured)"
    print(f"team         {config.team}{suffix}")
    return config, None


def cmd_init_agent(args: argparse.Namespace) -> int:
    """Shape the project with the initializer. Resumable by re-running."""
    paths = _resolve(args.path)
    config = load_config(paths)

    brief = paths.root / "BRIEF.md"
    context_dir = paths.root / "context"
    print(f"brief        {brief}{'' if brief.is_file() else '  (not written yet)'}")
    print(f"context      {context_dir}"
          f"{'' if context_dir.is_dir() else '  (not created yet)'}")
    if brief.is_file():
        print("             resuming — the initializer will re-read it")
    waiting = Tree(paths.tree_file, paths.events_file).open_questions()
    if waiting:
        print(f"\n{len(waiting)} question(s) still open; answer with `multiagents ask`")

    config, code = _choose_team(paths, config, getattr(args, "team", None))
    if code is not None:
        return code

    print()
    _report_catalog(config)

    # The same two checks `run` makes, for the same reason. The initializer is
    # told to consult the critic and the advisor, and a consult spawns an agent
    # — so on a docker project with no images it fails partway through a
    # conversation with the user, which is a confusing place to learn that
    # `build` has not been run.
    for note in _repair_credential_drift(paths, config):
        print(f"\ncredentials  {note}")
    problems = _executor_problems(paths, config)
    for problem in problems:
        print(f"\nexecutor     {problem}")
    if problems:
        print("\nnot ready: the initializer consults other agents, and they run "
              "in a container\n           this project cannot build yet. Run "
              "`multiagents build` first.")
        return 4

    held = driver._orchestrator_hold(paths, config)
    if held is not None:
        detail, resets_at = held
        print(f"\n{detail}")
        if not getattr(args, "wait", False):
            print("             `multiagents init-agent --wait` blocks until it resets")
            return 3
        if not driver._wait_for_reset(paths, config, resets_at):
            return 3

    print()
    return driver._launch_agent(paths, config, "initializer", resume=args.resume,
                         force=getattr(args, "force", False))


def _credential_owner(name: str, providers: dict) -> tuple[str, object]:
    """PS-R3: `(name, provider)` of whoever holds `name`'s login — its
    `auth_from` owner, or itself."""
    owner = str(getattr(providers.get(name), "auth_from", "") or "")
    return (owner, providers[owner]) if owner in providers \
        else (name, providers.get(name))


def _ensure_authenticated(paths, config, providers, interactive: bool = True) -> int:
    """Check every enabled provider and offer to fix what is broken.

    Returns the number still unauthenticated. Login scripts are run as ordinary
    subprocesses with stdio inherited rather than exec'd, so several can be
    repaired in one pass — exec would replace this process at the first one.
    """
    executor_of = executor_for(paths, config, providers)
    project_config = paths.config if paths else None
    enabled = {n: p for n, p in providers.items() if p.enabled}

    # (name, provider, profile). The profile is carried because under docker a
    # provider has two stored logins and they are repaired by two different
    # commands — offering the container's login for a signed-out host is how
    # `build` could end on "ready: multiagents run" and have run fail at once.
    #
    # PS-R3: what is checked, offered and logged in is the credential OWNER —
    # once, however many enabled dependents borrow it, and even when the
    # owner itself is disabled. Checking the owner and then running the
    # dependent's login repaired nothing (review ag-3644ef, finding 5).
    broken = []
    seen = set()
    for name, _ in sorted(enabled.items()):
        owner, provider = _credential_owner(name, providers)
        if owner in seen:
            continue
        seen.add(owner)
        state = auth_mod.check(owner, provider, executor_of(owner),
                               global_config_dir(), project_config)
        mark = "ok " if state.ok else "!! "
        print(f"  {mark}{owner:10} {state.detail[:70]}")
        if not state.ok:
            broken.append((owner, provider, ""))

    for name, state in sorted(_driver_host_states(
            config, providers, executor_of, project_config).items()):
        mark = "ok " if state.ok else "!! "
        print(f"  {mark}{name:10} {state.detail[:70]}   ← host, where run execs")
        if not state.ok:
            broken.append((*_credential_owner(name, providers), auth_mod.HOST))

    if not broken:
        return 0

    def _fix(name: str, profile: str) -> str:
        return f"multiagents auth login {name}" + (" --host" if profile else "")

    if not interactive:
        for name, _, profile in broken:
            print(f"  fix: {_fix(name, profile)}")
        return len(broken)

    still_broken = 0
    for name, provider, profile in broken:
        where = " (this machine's own profile)" if profile else ""
        print(f"\n{name} needs authenticating{where}.")
        try:
            answer = input(f"  run `{_fix(name, profile)}` now? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            answer = "n"
        if answer in ("n", "no"):
            print(f"  skipped — {'the orchestrator' if profile else f'agents on {name}'}"
                  f" will fail until you run `{_fix(name, profile)}`")
            still_broken += 1
            continue

        built = auth_mod.login_command(name, provider, executor_of(name),
                                       global_config_dir(), project_config,
                                       profile=profile)
        if built is None:
            print(f"  no script for {name!r}")
            still_broken += 1
            continue
        argv, env = built
        sys.stdout.flush()
        # stdio is inherited, so the script gets the terminal it needs.
        subprocess.run(argv, env=env)

        recheck = auth_mod.check(name, provider, executor_of(name),
                                 global_config_dir(), project_config,
                                 profile=profile)
        print(f"  {name}: {recheck.status}")
        if not recheck.ok:
            still_broken += 1
        elif paths is not None and not profile:
            # Only the container's credential is behind an inode-pinned mount.
            # Restarting the container over a HOST login would interrupt agents
            # to re-resolve a file that did not change.
            _refresh_container_after_login(paths, config, name)
    return still_broken


def cmd_build(args: argparse.Namespace) -> int:
    """Prepare everything agents need: the container, then authentication."""
    paths = _resolve(args.path)
    config = load_config(paths)
    providers = load_providers(config.providers)

    if config.executor == "docker":
        ex = _docker_executor(paths)
        source = global_config_dir() / "docker"
        if not source.is_dir():
            shutil.copytree(Path(__file__).parent / "defaults" / "docker", source)
        for dockerfile, tag in (("Dockerfile", ex.image),
                                ("Dockerfile.proxy", ex.proxy_image)):
            if dockerfile == "Dockerfile.proxy" and ex.network_mode != "allowlist":
                continue
            if ex.image_exists(tag) and not args.rebuild:
                print(f"  have  {tag}")
                continue
            print(f"building {tag} ...")
            result = ex.build_image(source / dockerfile, tag)
            if not result["ok"]:
                print(result.get("output") or result.get("error"), file=sys.stderr)
                return 1
            print(f"  ok    {tag}")

        state = ex.ensure_running()
        if not state.get("ok"):
            print(state.get("error"), file=sys.stderr)
            return 1
        print(f"container    {ex.container} running ({ex.network_mode} networking)")
    else:
        print(f"executor     {config.executor} — no container to build")

    # Auth comes AFTER the container: a provider whose credentials live inside
    # it (agy) cannot be checked, let alone repaired, until it exists.
    print("\nauth")
    remaining = _ensure_authenticated(paths, config, providers,
                                      interactive=not args.no_auth)

    if remaining:
        print(f"\n{remaining} provider(s) still unauthenticated. Agents on them "
              f"will fail with an empty response.")
    else:
        print("\nready        multiagents run")
    return 0


def _report_catalog(config, provider: str = "opencode-go") -> int:
    """Print the catalog diff. Returns the count of roster-affecting changes."""
    result = catalog_mod.check(global_config_dir(), provider, config.agents)
    if not result.get("ok"):
        print(f"catalog      unreachable — {result.get('error','')}")
        return 0
    if result.get("first_run"):
        catalog_mod.apply(global_config_dir(), provider)
        print(f"catalog      baseline recorded ({result['models']} {provider} models)")
        return 0

    assessment = result.get("assessment", {})
    affecting = assessment.get("affecting_roster", [])
    if not result.get("changed"):
        print(f"catalog      up to date ({result['models']} {provider} models)")
        return 0

    print(f"catalog      {result['changed']} change(s) since "
          f"{result.get('fetched_at_local','?')}  [{assessment.get('severity')}]")
    for item in affecting:
        print(f"             {item['severity'].upper():8} {item['detail']}")
        print(f"                      used by: {', '.join(item['used_by'])}")
    unrelated = assessment.get("unrelated_changes", 0)
    if unrelated:
        print(f"             {unrelated} other change(s) not touching your roster")
    if affecting:
        print("             -> consult the critic before editing agents.yaml")
    print("             record the new baseline with `multiagents catalog --update`")
    return len(affecting)


# Entries that should almost never enter a first commit: credentials, and the
# generated directories that make a repository unusable. Matched against the
# top-level paths `git status` reports, so `node_modules/` is one entry rather
# than forty thousand.
UNSAFE_FIRST_COMMIT = (
    ".env", ".envrc", ".npmrc", ".netrc", "node_modules/", "venv/", ".venv/",
    "__pycache__/", "dist/", "build/", "target/", ".DS_Store", "secrets/",
    "credentials.json", "id_rsa", ".ssh/", ".aws/", ".gnupg/",
)


def _gitignore_add(root: Path, lines: list[str], header: str = "") -> list[str]:
    """Append the lines not already listed. Returns what it actually wrote."""
    path = root / ".gitignore"
    existing = path.read_text() if path.is_file() else ""
    present = set(existing.split())
    missing = [line for line in lines if line not in present]
    if not missing:
        return []
    with path.open("a") as handle:
        handle.write(("" if not existing or existing.endswith("\n") else "\n")
                     + (f"\n{header}\n" if header else "\n")
                     + "\n".join(missing) + "\n")
    return missing


def _confirm(question: str, default: bool = False) -> bool:
    """Ask a yes/no question, returning `default` when nobody can answer.

    `init` is run by hand, but also by `make init` and by scripts. A prompt that
    blocked on a closed stdin would hang a headless setup, so with no terminal
    every offer declines itself and the manual command is printed instead —
    exactly what this command did before it asked anything.
    """
    if not sys.stdin.isatty():
        return default
    try:
        answer = input(f"{question} {'[Y/n]' if default else '[y/N]'} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return default if not answer else answer in ("y", "yes")


def _offer_git(root: Path) -> None:
    """Ensure the project is a repository with a commit to branch from.

    This is not a convenience. Without a repository ``runner._launch`` silently
    falls back to running every agent in the project directory itself — no
    branch, no worktree, no isolation between concurrent agents and no way to
    discard a bad run. Printing the command and hoping left that arrangement
    reachable by doing nothing, so init offers to close it.

    Called *after* ``.gitignore`` and ``context/`` are written, so a first commit
    made here excludes ``.multiagents/`` and includes the context directory.
    """
    if not gitops.is_repo(root):
        print("\ngit          not a repository")
        print("             agents work on their own branches, so this project needs one")
        if not _confirm("             run `git init` here now?"):
            print(f"             later: git -C {root} init && "
                  f"git -C {root} add -A && git -C {root} commit -m 'initial commit'")
            return
        result = gitops.init_repo(root)
        if not result.ok:
            print(f"             git init failed: {(result.err or result.out)[:200]}")
            return
        print(f"git          initialised {root}")

    if gitops.has_commits(root):
        print(f"git          {gitops.current_branch(root)} @ {gitops.head_sha(root)[:12]}")
        return

    # A worktree cannot be cut from nothing. An *empty* first commit satisfies
    # that and is still wrong for a project that already has files: agents check
    # out their branch and find none of them. So the first commit is the project.
    print("\ngit          repository has no commits")
    entries = gitops.uncommitted_entries(root, root=root)
    unsafe = [e for e in entries if e in UNSAFE_FIRST_COMMIT]
    if entries:
        print(f"             agents see only committed files; "
              f"{len(entries)} path(s) are uncommitted")
    if unsafe:
        print("             these look like they belong in .gitignore first:")
        for entry in unsafe:
            print(f"               {entry}")
        # Default yes, and asked before the commit: committing a .env because a
        # warning scrolled past is precisely the accident this exists to stop.
        # The isatty guard keeps a headless run from silently editing
        # .gitignore — it declines the commit below anyway.
        if sys.stdin.isatty() and _confirm("             add them to .gitignore now?",
                                           default=True):
            _gitignore_add(root, unsafe, "# added by multiagents init")
            print(f"             added to .gitignore: {', '.join(unsafe)}")
            entries = gitops.uncommitted_entries(root, root=root)

    question = ("             make an empty initial commit?" if not entries else
                "             commit them all as the initial commit?")
    if not _confirm(question):
        print(f"             later: git -C {root} add -A && "
              f"git -C {root} commit -m 'initial commit'")
        return
    result = gitops.initial_commit(root)
    if not result.ok:
        # Usually an unconfigured user.email, whose own message explains itself.
        for line in (result.err or result.out).splitlines()[:4]:
            print(f"             {line}")
        return
    print(f"git          {gitops.current_branch(root)} @ {gitops.head_sha(root)[:12]}")


DOCKER_DOCS = "https://docs.docker.com/engine/install/"
DOCKER_DESKTOP_DOCS = "https://docs.docker.com/desktop/"


def _docker_install_hint() -> list[str]:
    """How to install docker on *this* machine, best effort.

    Detected rather than generic: "see the docs" is what someone reads when
    they have already given up. The official page is given regardless, because
    a distro guess can be wrong and the hint has to be checkable.
    """
    import platform

    system = platform.system()
    if system == "Darwin":
        return [f"  brew install --cask docker      # or {DOCKER_DESKTOP_DOCS}"]
    if system == "Windows":
        return [f"  winget install Docker.DockerDesktop    # {DOCKER_DESKTOP_DOCS}"]

    distro = ""
    release = Path("/etc/os-release")
    if release.is_file():
        fields = {}
        for line in release.read_text().splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                fields[key] = value.strip().strip('"')
        distro = (fields.get("ID", "") + " " + fields.get("ID_LIKE", "")).lower()

    if any(name in distro for name in ("ubuntu", "debian", "mint", "pop")):
        return [
            "  curl -fsSL https://get.docker.com | sh      # official convenience script",
            "  sudo usermod -aG docker $USER               # then log out and back in",
        ]
    if any(name in distro for name in ("fedora", "rhel", "centos", "rocky", "alma")):
        return [
            "  sudo dnf install docker-ce docker-ce-cli containerd.io",
            "  sudo systemctl enable --now docker",
            "  sudo usermod -aG docker $USER               # then log out and back in",
        ]
    if "arch" in distro or "manjaro" in distro:
        return [
            "  sudo pacman -S docker",
            "  sudo systemctl enable --now docker",
            "  sudo usermod -aG docker $USER               # then log out and back in",
        ]
    if "suse" in distro:
        return ["  sudo zypper install docker",
                "  sudo systemctl enable --now docker"]
    return []


def _set_executor(paths, kind: str) -> bool:
    """Rewrite `executor.kind` in the project's own config, comments intact.

    A line edit rather than a YAML round trip: the shipped file is mostly
    comments explaining the choices, and dumping it back through a parser would
    throw all of them away.
    """
    import re

    config = paths.config / "project.yaml"
    if not config.is_file():
        return False
    text = config.read_text()
    updated, count = re.subn(r"(?m)^(executor:\n(?:[ \t]*(?:#[^\n]*)?\n)*[ \t]+kind:[ \t]*)\w+",
                             lambda m: m.group(1) + kind, text, count=1)
    if not count:
        return False
    config.write_text(updated)
    return True


def _set_team(paths, team: str) -> bool:
    """Rewrite the top-level `team:` line in the project's own config.

    Same shape as `_set_executor`, same reason: a line edit rather than a
    YAML round trip, because the shipped file is mostly comments explaining
    the choices and a round trip would throw them all away for a one-line
    change. Only a `team:` key at column zero is touched — one nested under
    another mapping, or merely mentioned in a comment, never matches. A
    project that inherits `team:` from the defaults layer has no line to
    substitute, so one is inserted instead, on its own line.
    """
    import re

    config = paths.config / "project.yaml"
    if not config.is_file():
        return False
    text = config.read_text()

    def replace(match: re.Match) -> str:
        comment = re.search(r"\s*#.*$", match.group(1))
        return f"team: {team}{comment.group(0) if comment else ''}"

    updated, count = re.subn(r"(?m)^team:[ \t]*(.*)$", replace, text, count=1)
    if not count:
        body = text
        if body and not body.endswith("\n"):
            body += "\n"
        if body and not body.endswith("\n\n"):
            body += "\n"
        updated = body + f"team: {team}\n"
    config.write_text(updated)
    return True


def _offer_docker(paths) -> str:
    """Offer the container executor. Returns "" or a reason to come back later.

    Agents run with the permission flags that turn approval off — `--auto`,
    `--dangerously-skip-permissions`, `bypassPermissions`. On the local
    executor that is this user account, so which backend a project uses is a
    security decision and belongs to the person, asked once, at the point they
    are setting the project up.
    """
    from .executor.docker import docker_state

    current = load_config(paths).executor
    if current == "docker":
        print("executor     docker (already set)")
        return ""

    state, detail = docker_state()
    print("\nexecutor     local")
    print("             agents run with approval turned off — `--auto`,")
    print("             `--dangerously-skip-permissions`. On the local executor")
    print("             that is your user account, your files and your keys.")
    print("             docker confines them to a container with no route out.")

    if state == "ok":
        print(f"             docker is available here (server {detail})")
        # Asked only when someone is there to answer. `default=True` on a
        # closed stdin would let `make init` switch a project's execution
        # backend with nobody deciding — the opposite of the point, which is
        # that this choice belongs to a person.
        if sys.stdin.isatty() and _confirm(
                "             use the docker executor for this project?", default=True):
            if _set_executor(paths, "docker"):
                print("executor     docker   (run `multiagents build` before `run`)")
            else:
                print("             could not edit project.yaml; set executor.kind by hand")
            return ""
        print("             staying local — set executor.kind: docker in")
        print("             .multiagents/config/project.yaml to change your mind")
        return ""


    print("             docker is NOT usable here: " + detail)
    if state == "no-daemon":
        print("             the binary is installed but the daemon is unreachable —")
        print("             starting it, or adding yourself to the `docker` group,")
        print("             is usually all that is needed.")
    if not sys.stdin.isatty():
        print("             continuing on the local executor (nobody to ask).")
        return ""
    if _confirm("             continue without docker for now?", default=True):
        print("             continuing on the local executor. Agents will have the")
        print("             access you do; treat their tasks accordingly.")
        return ""
    return detail


def _refuse_nesting(root: Path, allow_nested: bool) -> str:
    """Why this directory must not become a project, or "".

    A project is exactly one git repository, rooted at that repository's root.
    Everything here rests on that: `create_worktree` runs
    `git -C <project root> worktree add`, and git resolves it to the
    REPOSITORY. So a project inside another one hands its agents full checkouts
    of the outer repository and a branch namespace shared with the outer
    project's agents, while counting concurrency, budget and watchdogs
    separately. It looks like it works, which is the problem.
    """
    if allow_nested:
        return ""
    outer = find_project_root(root)
    if outer is None or outer == root:
        return ""
    return (
        f"{outer} is already a multiagents project.\n"
        f"Agents branch and merge in one repository, so a second project inside "
        f"it would share that branch namespace while counting its own limits "
        f"separately.\n"
        f"Work on this directory from {outer}, or make it its own git "
        f"repository first. `--nested` overrides this."
    )


def cmd_init(args: argparse.Namespace) -> int:
    # The global `--path` and the positional both name the directory to
    # initialise; they parse into separate dests so neither silently discards
    # the other. Either may name a directory that does not exist yet —
    # creating it is the command's job — but an explicitly EMPTY one names
    # nothing, and silently initialising the cwd is how `or "."` would read it.
    given = [p for p in (args.path, getattr(args, "init_path", None)) if p is not None]
    for explicit in given:
        if explicit == "":
            print(f"{explicit!r} does not name a directory.", file=sys.stderr)
            raise SystemExit(2)
    roots = {Path(p).expanduser().resolve() for p in given}
    if len(roots) > 1:
        print(f"--path {args.path} and the positional path {args.init_path} "
              "name different directories; give one.", file=sys.stderr)
        raise SystemExit(2)
    root = roots.pop() if roots else Path(".").resolve()
    refusal = _refuse_nesting(root, args.nested)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2
    root.mkdir(parents=True, exist_ok=True)
    paths = ProjectPaths(root)
    paths.ensure()
    register_project(root)

    seed_global()
    seed_project(paths, force=args.force)

    print(f"project      {root}")
    print(f"data         {paths.data}")
    print(f"config       {paths.config}")
    print(f"worktrees    {paths.worktrees}")

    context_dir = root / "context"
    if not context_dir.exists():
        context_dir.mkdir(parents=True, exist_ok=True)
        (context_dir / "README.md").write_text(
            "# context\n\n"
            "Reference material agents need that is not code: requirements,\n"
            "specifications, links, API docs, design templates, screenshots,\n"
            "exported tickets, brand assets.\n\n"
            "This directory and `BRIEF.md` must be **committed**. Agents work in\n"
            "git worktrees — separate checkouts of their branch — so anything\n"
            "uncommitted or gitignored does not exist for them.\n\n"
            "`multiagents init-agent` fills both in, with you.\n"
        )
        print(f"context      {context_dir}  (created)")

    plans.scaffold(root)

    if _gitignore_add(root, [GITIGNORE_LINE], "# multiagents runtime state"):
        print(f"gitignore    added {GITIGNORE_LINE}")

    # After the two writes above, so a first commit made here picks them up.
    _offer_git(root)

    # Legitimate — a repository whose root is a parent you do not own — but the
    # user should know that every agent worktree will be a checkout of the
    # larger repository, and that branches land in its namespace.
    top = gitops.repo_root(root)
    if top is not None and top != root:
        print(f"\nnote         this is not the repository root")
        print(f"             repository: {top}")
        print(f"             agent worktrees will be full checkouts of it, and "
              f"branches\n             will land in its namespace")

    deferred = _offer_docker(paths)

    config = load_config(paths)
    providers = load_providers(config.providers)
    result = refresh_models(providers, paths.config / "models.yaml",
                            config_dir=global_config_dir(), project_config=paths.config,
                            executor_for=executor_for(paths, config, providers))
    for name, count in result["counts"].items():
        print(f"models       {name}: {count}")
    for name, problem in result["problems"].items():
        print(f"models       {name}: {problem}")

    _report_catalog(load_config(paths))

    mcp_path = driver._write_mcp_config()
    print(f"mcp config   {mcp_path}")
    print("\nNext:")
    print("  1. multiagents init-agent    shape the project (resumable)")
    print("  2. edit .multiagents/config/agents.yaml if you want a different roster")
    print("  3. multiagents build         container environment, if executor is docker")
    print("  4. multiagents run           launch the orchestrator")

    # Everything above ran; this only reports the state it ends in. Without a
    # repository and a commit no agent can be given a branch, so `run` will
    # refuse at the first spawn — and a scripted setup that read exit 0 here
    # would call this project ready. Non-zero is how CI finds out.
    if deferred:
        print(f"\nnot ready: you chose to wait for docker ({deferred}).")
        hints = _docker_install_hint()
        if hints:
            print("\nOn this system:")
            for line in hints:
                print(line)
        print(f"\nOfficial instructions: {DOCKER_DOCS}")
        print("\nThe project is set up and nothing is lost — re-run "
              "`multiagents init` once\ndocker works and it will offer again.")
        return 3

    if not (gitops.is_repo(root) and gitops.has_commits(root)):
        print("\nnot ready: no git repository with a commit, so no agent can be "
              "given a branch.\n             set one up and re-run `multiagents init`.")
        return 1
    return 0


def _executor_problems(paths, config) -> list[str]:
    """What would stop the FIRST delegation, checked before launching.

    The orchestrator itself runs on the host, so nothing about docker is
    exercised until it delegates — which meant a missing image surfaced as a
    failed spawn several minutes into a session, after the briefing, rather
    than as a refusal to start. `preflight` already knew; nobody asked it.
    """
    if config.executor != "docker":
        return []
    try:
        executor = _docker_executor(paths)
        problems = executor.preflight()
        stale = executor.mount_drift()
        if stale:
            warning = executor.recreation_warning()
            problems.append(
                f"the running container predates the current configuration "
                f"({len(stale)} mount(s) differ, e.g. {stale[0]}). Mounts are "
                f"fixed at CREATION, so `down` and `up` will not change them"
                + (f". {warning}" if warning else
                   " — `multiagents docker rm && multiagents docker up` replaces it."))
        return problems
    except Exception as exc:                 # never block a launch on the check
        return [f"could not check the docker executor: {type(exc).__name__}: {exc}"]




def _reaper(paths):
    """A Runner used only to reach detached processes. Built once, lazily."""
    if not hasattr(_reaper, "_cache") or _reaper._cache[0] != paths.root:
        _reaper._cache = (paths.root, Runner(paths, load_config(paths)))
    return _reaper._cache[1]


def _alive_pid(paths, role: str) -> bool:
    return driver._role_alive(paths, role)


INTERRUPTED_COMMIT = (
    "WIP: {agent} interrupted before it finished ({agent_id})\n\n"
    "Committed by multiagents so the work is not lost, NOT by the agent. The "
    "tree may be mid-edit and syntactically broken through no fault of the "
    "agent — treat it as a checkpoint to inspect, never as a finished result."
)


CHECKPOINT_FAILED = "checkpoint failed"


def _checkpoint_failed(tree: Tree, node_id: str, action: str, detail: str) -> None:
    """HG-R9: record a node's refused or failed host checkpoint.

    The worktree is left as it is, and the node's reason says why, which is
    what keeps `clean --branches` from deleting it without `--force`.
    """
    detail = detail.strip()[:400]
    if "host_authority_mismatch" in detail:
        tree.emit(node_id, "host_authority_mismatch", node=node_id, action=action,
                  fields=["branch"], reason=detail)
    tree.emit(node_id, "checkpoint_failed", action=action, detail=detail)
    node = tree.get(node_id)
    if node is not None:
        note = f"{CHECKPOINT_FAILED}: {detail}"
        tree.update(node_id, reason=f"{node.reason}; {note}" if node.reason else note)
    print(f"  {node_id}: {CHECKPOINT_FAILED}, worktree kept: {detail}", file=sys.stderr)


def _checkpoint_operands(node, authority: HostAuthority, tree: Tree,
                         action: str) -> tuple[object | None, str]:
    """HA-R10: the node with the operands a stop or resume checkpoint may use.

    Taken the way other host actions take them: from the host record when
    the node is recorded, and under HA-R2a when it is not. The worktree has
    to be a checkout inside the worktree root and the branch an `agents/*`
    ref no other record holds, whatever tree.json says, so a checkpoint
    never commits the project root or onto the base branch or another
    node's branch. HA-R12: an operand of the wrong type is refused too.

    Returns `(node, "")` to checkpoint, `(None, "")` when there is nothing to
    checkpoint, and `(None, detail)` when the operands were refused.
    """
    record = authority.get(node.id)
    if record:
        fields = [key for key in ("branch", "worktree", "parent")
                  if getattr(node, key) != record.get(key)]
        if fields:
            tree.emit(node.id, "host_authority_mismatch", node=node.id,
                      action=action, fields=fields)
        node = dataclasses.replace(node, **{key: record.get(key)
                                            for key in ("branch", "worktree", "parent")})

    def refused(fields: list[str], reason: str) -> tuple[None, str]:
        tree.emit(node.id, "host_authority_mismatch", node=node.id,
                  action=action, fields=fields, reason=reason)
        return None, f"operands refused ({', '.join(fields)}): {reason}"

    worktree, branch = node.worktree, node.branch
    bad = [key for key, value in (("worktree", worktree), ("branch", branch))
           if value is not None and not isinstance(value, str)]
    if bad:
        return refused(bad, "not a string")
    if not worktree:
        return None, ""
    try:
        path = Path(worktree)
        inside = (authority.safe_seeded_path(path) if record
                  else authority.safe_nested_path(path))
        if not inside:
            return refused(["worktree"], "worktree is outside the container domain")
        if not path.is_dir():
            return None, ""
    except (OSError, ValueError, RuntimeError):
        return refused(["worktree"], "worktree could not be resolved")
    if record:
        others = {r.get("branch") for key, r in authority.read().items() if key != node.id}
        ok = (bool(branch) and branch.startswith("agents/")
              and bool(branch.removeprefix("agents/")) and branch not in others
              and gitops.run(authority.paths.root, "check-ref-format",
                             f"refs/heads/{branch}").ok)
    else:
        ok = bool(branch) and authority.safe_unrecorded_branch(branch)
    if not ok:
        return refused(["branch"], "branch is not this node's agents/* branch")
    return node, ""


def _save_interrupted(node, root: Path | None = None,
                      authority: HostAuthority | None = None,
                      tree: Tree | None = None, action: str = "resume") -> bool:
    """Commit an interrupted agent's worktree, marked as what it is.

    A cancelled node is TERMINAL, and `clean --branches` removes worktrees for
    terminal nodes — so "leave it dirty, worktrees are disposable" would mean
    the next routine cleanup deletes it with no record it existed.

    The message matters as much as the commit. Without it the orchestrator can
    pick the branch up later, run tests against half-written files, and spend
    tokens debugging syntax errors caused entirely by the termination.

    `root`, the project root, pins the dirtiness check to the repository's
    trusted paths (SG-R4); a worktree it cannot resolve is left alone.
    `action` is the pass it runs in, `resume` or `stop`, for its message
    and its events.
    """
    worktree = Path(node.worktree) if node.worktree else None
    if not worktree or not worktree.is_dir():
        return False
    if (authority and not authority.get(node.id)
            and not authority.safe_unrecorded_branch(node.branch)):
        if tree:
            tree.emit(node.id, "host_authority_mismatch", node=node.id,
                      action=action, fields=["branch"],
                      reason="unrecorded branch is outside the container domain")
        return False
    try:
        pinned = (authority.pinned_worktree(worktree) if authority and node.branch
                  else contextlib.nullcontext(worktree))
        with pinned as checkout:
            if not gitops.is_repo(checkout, root=root):
                return False
            if not gitops.is_dirty(checkout, root=root):   # SG-R4
                return False
            message = (f"{node.agent}: work in progress when stopped ({node.id})"
                       if action == "stop" else
                       INTERRUPTED_COMMIT.format(agent=node.agent, agent_id=node.id))
            result = gitops.commit_all(checkout, message, root=root, branch=node.branch)
            if not result.ok and "filtered paths" in result.err:
                if tree:
                    tree.emit(node.id, "host_git_refused", detail=result.err)
                print(result.err)
            if not result.ok and tree:
                _checkpoint_failed(tree, node.id, action, result.err or result.out)
            return bool(result.ok)
    except (OSError, ValueError):
        if tree:
            tree.emit(node.id, "host_authority_mismatch", node=node.id,
                      action=action, fields=["worktree"],
                      reason="worktree could not be pinned")
        return False
    except gitops.GitError as exc:
        if tree:
            _checkpoint_failed(tree, node.id, action, str(exc))
        return False


def cmd_resume(args: argparse.Namespace) -> int:
    """Reconcile state after a crash or restart, then reopen the session."""
    from . import watchdog          # local, as everywhere else here: cycle

    paths = _resolve(args.path)
    from . import scheduler
    if scheduler.enabled(paths.root):
        result = scheduler.start(paths.root)
        if result.get("error"):
            print(json.dumps(result), file=sys.stderr)
            return 1
    tree = Tree(paths.tree_file, paths.events_file)
    authority = HostAuthority(paths, tree)

    # Agents are started with start_new_session=True, so that stopping one also
    # stops the shells and test runners beneath it — which means they are in
    # their OWN session and do NOT die with the server. On a clean teardown the
    # cancellation handler kills each group; after a crash nothing runs, and
    # they keep going with their output going nowhere. So this reconciles two
    # different states: processes that are gone, and processes that should be.
    orchestrator_live = _alive_pid(paths, "orchestrator")
    # Asked BEFORE the pass below, because the pass is what erases the evidence:
    # once the nodes are reclaimed and the status file is rewritten by the next
    # supervisor, nothing remembers that the last session was cut off.
    unclean = watchdog.ended_uncleanly(paths, "orchestrator")
    reclaimed, reaped, saved = 0, 0, 0
    left_running: list[str] = []      # SV-R6/R10: the next server adopts these
    ended_alone: list[str] = []       # finished with nobody watching; ditto
    for node in tree.active():
        if tree.id_mismatch(node.id):
            # HA-R9: reconciled like any other, but no host git on it.
            tree.emit(node.id, "host_authority_mismatch", node=node.id,
                      action="resume", fields=["id"])
            target, refused = None, ""
        else:
            # HA-R10: operands from the record, or under HA-R2a.
            target, refused = _checkpoint_operands(node, authority, tree, "resume")
        # `procs.alive`, not `os.kill(pid, 0)`. This loop runs after a restart,
        # and after a REBOOT every pid here was issued by a kernel that is gone
        # while the low numbers have already been handed out again — so the
        # bare question "does something hold this number" gets the wrong answer
        # in both directions, and this is the one place where both answers do
        # damage: a false alive sends SIGTERM to a stranger's process group
        # below, and a false alive on the orchestrator skips the whole pass.
        alive = procs.alive(node.pid, node.pid_start)
        run_dir = paths.run_dir(node.id)
        wrapped = (run_dir / "output.ndjson").is_file()
        # SV-R3/R6: an agent under the launch wrapper does not need its
        # server. Its output and exit status are on disk, and the next root
        # server follows it from there — so it is left alone and reported,
        # neither reaped nor written off.
        if not alive and (wrapped or (run_dir / "exit_status").is_file()):
            ended_alone.append(node.id)
            continue
        if alive and wrapped and not orchestrator_live:
            left_running.append(node.id)
            unclean = unclean or node.status != "detached"
            continue
        if alive and not orchestrator_live:
            # An orphan: nobody is reading its stream, so it is spending tokens
            # into a closed pipe. Leaving it running would also let it mutate a
            # worktree the next session is about to work in.
            try:
                _reaper(paths).stop_detached(node)
                reaped += 1
            except Exception as exc:
                print(f"  could not stop {node.id}: {exc}", file=sys.stderr)
        elif alive:
            continue                        # another session owns it
        # SL-R7: a node still `stuck` here never got a terminal status of its
        # own — that classification never ran, since the server that would
        # have run it is what died. Fold the trip into the reconciled reason
        # rather than overwrite it, so the trip that caused the stall is not
        # lost the moment it is healed.
        reason = ("reaped: left running by a server that is gone" if alive
                  else "process gone; server restarted")
        if node.status == "stuck" and node.reason:
            reason = f"{reason} (was stuck: {node.reason})"
        tree.set_status(node.id, "orphaned", reason)
        reclaimed += 1
        # Committed HERE rather than in the cancellation handler: this runs with
        # time, a live loop and full information, where a teardown has none of
        # those and a git call in it can hang for its whole timeout.
        if refused:
            _checkpoint_failed(tree, node.id, "resume", refused)
        elif target and _save_interrupted(target, paths.root, authority, tree):
            saved += 1

    # SG-R2: branches a container could not delete, now that the host can.
    reap_pending_branches(paths.root, tree, authority)

    # Reclaiming a node is itself proof of an ending nobody recorded: a clean
    # teardown marks its agents cancelled, so a node still claiming to run
    # means nothing was left to mark it.
    unclean = unclean or bool(reclaimed or reaped or saved)

    print(tree.render())
    if reclaimed:
        print(f"\nreclaimed    {reclaimed} agent(s) whose process no longer exists")
    if reaped:
        print(f"reaped       {reaped} orphaned agent process(es) still running")
    if saved:
        print(f"committed    interrupted work in {saved} worktree(s)")
    if left_running:
        print(f"still running {len(left_running)} agent(s) left running by the "
              f"previous session, followed again once it starts: "
              f"{', '.join(left_running)}")
        print("             `multiagents stop <id>` ends one, "
              "`multiagents stop --all` ends them all")
    if ended_alone:
        print(f"ended        {len(ended_alone)} agent(s) finished with no session "
              f"attached, recorded once it starts: {', '.join(ended_alone)}")
    if unclean:
        print("\nlast session ended without recording an ending — a crash, a "
              "kill, or\n             a power cut. The orchestrator is told so "
              "on resume, and takes\n             stock before it carries on.")

    data = tree.read()
    stale = [
        n for n in data["nodes"].values()
        if n.get("branch") and n.get("status") in {"orphaned", "stuck", "done", "failed", "refused"}
    ]
    if stale:
        print(f"\n{len(stale)} branch(es) still held by unfinished agents:")
        for node in stale:
            commits = gitops.commits_on(
                paths.root, node["branch"], gitops.current_branch(paths.root),
                root=paths.root,
            )
            print(f"  {node['id']:10} {node['agent']:14} {node['branch']:38} {commits} commit(s)")
        print("  merge with merge_agent, or drop with `multiagents clean --branches`")

    if data.get("deferred"):
        print(f"\n{len(data['deferred'])} task(s) deferred on quota")

    waiting = tree.open_questions()
    if waiting:
        # Reported, never a refusal: the first place a question should be
        # answered is inside the orchestrator, so refusing to launch it would
        # make the cheaper path unreachable.
        print(f"\n{len(waiting)} agent(s) waiting on a decision:")
        for q in waiting[:5]:
            print(f"  {q['id']}  {q['agent']} · {q['topic']}: {q['question'][:70]}")
        print("  the orchestrator can answer these, or use `multiagents ask`")

    pause = tree.pause_state()
    if pause:
        waiting = max(0, int(pause["until"] - time.time()))
        print(f"\npaused       {pause.get('reason', 'no provider available')}")
        print(f"             clears in about {waiting // 60}m; deferred work "
              f"restarts by itself")

    config = load_config(paths)
    requested = getattr(args, "team", "")
    if requested:
        known = set(config.teams)
        if requested not in known:
            print(f"\nno team named {requested!r}. Known: "
                  f"{', '.join(sorted(known)) or 'none'}", file=sys.stderr)
            return 1
        config = dataclasses.replace(
            config, project={**config.project, "team": requested})
    if config.team:
        note = config.team_spec().get("description", "").strip().replace("\n", " ")
        print(f"\nteam         {config.team}"
              + (f" — {' '.join(note.split())}" if note else "")
              + ("  (this run only)" if requested else ""))

    for note in _repair_credential_drift(paths, config):
        print(f"\ncredentials  {note}")
    problems = _executor_problems(paths, config)
    for problem in problems:
        print(f"\nexecutor     {problem}")
    # Reported here so `--no-launch` says it too; the refusal itself lives in
    # driver._launch_agent, which is the path `init-agent` takes as well.
    spec = driver._launched_spec(config, "orchestrator", config.team)
    auth_problem = driver._auth_problem(paths, config, spec) if spec else ""
    if auth_problem:
        print(f"\nauth         {auth_problem}")

    if args.no_launch:
        return 0

    if problems:
        print("\nnot ready: this project runs agents in a container and the "
              "container cannot be\n           built from what is here. The "
              "orchestrator would start fine and fail\n           at its first "
              "delegation.")
        return 4

    # The orchestrator spends the same bucket the user's own session does when
    # it is pinned to `claude`. Launching into an exhausted one produces a CLI
    # error with no reset time in it, which reads as a broken install.
    held = driver._orchestrator_hold(paths, config)
    if held is not None:
        detail, resets_at = held
        print(f"\n{detail}")
        if not args.wait:
            print("             `multiagents run --wait` blocks until it resets")
            return 3
        if not driver._wait_for_reset(paths, config, resets_at):
            return 3

    # The catalog baseline is the initializer's concern; re-checking it on every
    # orchestrator launch spends a network round trip on ground that rarely
    # moves. The orchestrator calls check_model_catalog when something suggests
    # it has.
    return driver._launch_agent(paths, config, "orchestrator", resume=args.resume,
                         force=getattr(args, "force", False),
                         unattended=getattr(args, "unattended", 0),
                         supervise=getattr(args, "supervise", True),
                         unclean=unclean)


def _refresh_container_after_login(paths, config, provider_name: str) -> None:
    """Restart the container so agents stop using the credential just replaced.

    Writing a new token to disk is not enough for a CLI that caches it in a
    running process — `claude` keeps a daemon, and a container started before a
    re-login goes on serving the revoked one. Idle: restarted without asking,
    since it costs nothing but a few seconds. Busy: asked, because a restart
    kills every agent in there, including ones on providers that are perfectly
    fine.
    """
    if config.executor != "docker":
        return
    try:
        executor = _docker_executor(paths)
        if executor.container_state(executor.container) != "running":
            return
    except Exception:
        return

    active = Tree(paths.tree_file, paths.events_file).active()
    if active:
        print(f"    {len(active)} agent(s) are running in the container and may "
              f"cache the old\n    {provider_name} credential until it restarts "
              f"— restarting kills them.")
        if not _confirm("    restart the container now?", default=False):
            print(f"    left running. Agents will keep using the old credential; "
                  f"`multiagents docker down && up` when you are ready.")
            return
    try:
        executor.stop(remove=False)
        executor.ensure_running()
        print(f"    container restarted, so agents pick up the new "
              f"{provider_name} credential")
    except Exception as exc:
        print(f"    could not restart the container: {exc}. Agents will keep "
              f"using the old credential until you do.", file=sys.stderr)


def _repair_credential_drift(paths, config) -> list[str]:
    """Re-bind credential files the container has stopped sharing with us.

    A file bind mount is pinned to an inode, and every CLI here replaces its
    credential by rename — so the moment a token refreshes, the container is
    reading a file the host no longer has. It then presents a token that the
    refresh rotated away, which the server reports as revoked, and `auth status`
    on the host reads the correct file and says everything is fine.

    A restart re-resolves the bind; a rebuild is not needed. Idle, that is a few
    seconds and is simply done. Busy, it would kill running agents — including
    agents on providers that are working — so it is reported instead.
    """
    if config.executor != "docker":
        return []
    try:
        executor = _docker_executor(paths)
        drift = executor.credential_drift()
    except Exception:
        return []
    if not drift:
        return []

    names = ", ".join(Path(entry["path"]).name for entry in drift)
    active = Tree(paths.tree_file, paths.events_file).active()
    if active:
        return [f"the container is holding an OLD copy of {names} — it was "
                f"replaced on the host after the container started, and a file "
                f"mount follows the inode, not the path. Agents in there will "
                f"fail as if the credential were revoked. "
                f"{len(active)} agent(s) are running, so this was not fixed "
                f"automatically: `multiagents docker down && multiagents docker "
                f"up` when they finish."]
    try:
        executor.stop(remove=False)
        executor.ensure_running()
    except Exception as exc:
        return [f"the container holds an old copy of {names} and could not be "
                f"restarted: {type(exc).__name__}: {exc}"]
    return [f"restarted the container: it was holding an old copy of {names} "
            f"(replaced on the host by a token refresh)"]


def _report_agents(config, providers) -> int:
    """The `doctor` roster section. Returns the number of problems found.

    Extracted so the checks can be tested without a whole project on disk:
    every one of them is about a config file the user edits by hand, which is
    exactly where a silent mistake is most likely.
    """
    problems = 0
    for name, spec in sorted(config.agents.items()):
        provider = providers.get(spec.provider)
        mark = " " if provider and provider.available() else "!"
        if spec.launch:
            command = "init-agent" if spec.role == "initializer" else "run"
            tag = f"[launched by `multiagents {command}`]"
        else:
            where = spec.executor or config.executor
            tag = f"[{where}]" + ("*" if spec.executor else "")
        # The composed word count, because a brief is paid for on every launch
        # and nobody had ever measured one: two of the shipped briefs doubled
        # in a month before this line existed. It is reported, not enforced —
        # refusing to launch an agent because its brief grew is a worse failure
        # than the bloat — but it is reported where somebody adding a skill
        # will see what it cost.
        words = len(config.instructions_for(spec).split())
        print(f"  {mark} {name:18} {spec.provider}/{spec.model:30} {words:5}w {tag}")
        instructions = config.instructions_for(spec)
        if spec.instructions and not instructions.strip():
            print(f"    missing instructions file: {spec.instructions}")
            problems += 1
        # A permission name the provider does not define adds NO flags at all,
        # which is not a safe default: agy then auto-denies every tool and
        # returns an empty answer, so the agent looks broken rather than
        # misconfigured. Silent until now, hence its own line.
        profiles = (provider.spawn.get("permission") or {}) if provider else {}
        if profiles and spec.permission not in profiles:
            print(f"    unknown permission {spec.permission!r} for {spec.provider} — "
                  f"choose one of {', '.join(sorted(profiles))}")
            problems += 1

    for warning in validate_agent_models(config):
        print(f"    {warning}")
    if any(spec.executor for spec in config.agents.values()):
        print("    * pinned to an executor in agents.yaml, overriding the project default")
    return problems


def _clear_provider(paths, providers, name: str, force: bool) -> int:
    """Forget what the breaker learned about one provider.

    There was no way to do this. `clear_provider_health` had exactly one caller
    — the runner's own auth probe — so a breaker latched by a bug that has since
    been FIXED could only be cleared by reaching into tree.json by hand. Found
    the obvious way: agy's record still said "3 consecutive failures: print
    timeout" the morning after the print timeout was fixed.

    It matters more than tidiness. A lapsed cooldown leaves `tripped` set, and
    `note_run_outcome` reads that as half-open, where a SINGLE further failure
    re-trips immediately instead of waiting for the threshold. A stale record
    therefore makes the next unrelated hiccup cost a provider.

    Refuses while the cooldown is still running, because then the breaker is
    not stale, it is working, and clearing it is talking past a live outage
    rather than recovering from a dead one. `--force` for when the operator
    knows the cause is gone.
    """
    if paths is None:
        print("not inside a project — run this where the tree is.", file=sys.stderr)
        return 2
    if name not in providers:
        print(f"unknown provider {name!r}. This project has: "
              f"{', '.join(sorted(providers))}", file=sys.stderr)
        return 2

    tree = Tree(paths.tree_file, paths.events_file)
    record = tree.provider_health().get(name) or {}
    cooling = tree.cooldown(name) or {}
    left = cooling.get("until", 0) - time.time()

    if not record and not cooling:
        print(f"{name}: nothing to clear — no failure record, no cooldown.")
        return 0

    if left > 0 and not force:
        print(f"{name} is still cooling for {left / 60:.0f}m: "
              f"{str(cooling.get('reason'))[:90]}\n"
              f"That is the breaker working, not a stale record. Wait for it, "
              f"fix the cause, or `--clear {name} --force` if you know it is "
              f"gone.", file=sys.stderr)
        return 1

    if record.get("tripped"):
        print(f"  cleared  {name} failure record "
              f"({record.get('consecutive_failures', 0)} consecutive: "
              f"{str(record.get('last_reason'))[:60]})")
    elif record:
        print(f"  cleared  {name} failure record")
    tree.clear_provider_health(name)
    if tree.clear_cooldown(name):
        was = "expired" if left <= 0 else f"{left / 60:.0f}m remaining"
        print(f"  cleared  {name} cooldown ({was})")
    # Recorded, because "a person decided this" is a different fact from the
    # auth probe clearing it, and the next reader of the log wants to know which.
    tree.emit("-", "health_cleared", provider=name, by="doctor --clear",
              forced=bool(force and left > 0))
    print(f"{name}: the breaker starts over.")
    return 0


def cmd_git_guard(args: argparse.Namespace) -> int:
    """GG-R3/R4: scan a range for sensitive material, manage the pre-push hook."""
    from . import git_guard as gg

    action = args.git_guard_command
    if action in ("install", "uninstall"):
        try:
            repo = gg.discover_repo(Path.cwd())
        except gg.GitError as exc:
            print(f"git-guard {action}: {exc}", file=sys.stderr)
            return 2
        if action == "install":
            code, message = gg.install_hook(repo, force=args.force)
        else:
            code, message = gg.uninstall_hook(repo)
        print(f"git-guard {action}: {message}",
              file=sys.stderr if code else sys.stdout)
        return code

    paths = _resolve_if_project(args.path)
    try:
        config = load_config(paths)
    except ValueError as exc:
        print(f"git-guard: invalid config: {exc}", file=sys.stderr)
        return 2
    settings = gg.settings_from_git(config.project.get("git", {}))
    if action == "scan":
        try:
            repo = gg.discover_repo(paths.root if paths else Path.cwd())
        except gg.GitError as exc:
            print(f"git-guard scan: {exc}", file=sys.stderr)
            return 2
        try:
            if args.range:
                commits = gg.rev_list(repo, args.range, "--")
            else:
                commits = gg.default_range_commits(repo, settings)
            result = gg.scan_commits(repo, commits, settings)
        except gg.GitError as exc:
            print(f"git-guard scan: {exc}", file=sys.stderr)
            return 2
        except gg.PatternsFileError as exc:
            print(f"git-guard scan: {exc}", file=sys.stderr)
            return 2
        except gg.KeyFileError as exc:
            print(f"git-guard scan: {exc}", file=sys.stderr)
            return 2
        if result.notice:
            print(result.notice)
        for finding in result.findings:
            print(finding.line())
        if result.findings:
            print(gg.paste_block(result.findings))
            return 1
        return 0
    if action == "check-push":
        try:
            repo = gg.discover_repo(paths.root if paths else Path.cwd())
        except gg.GitError as exc:
            print(f"git-guard: {exc}", file=sys.stderr)
            return 1
        lines = (sys.stdin.read() or "").splitlines()
        try:
            result = gg.check_push_input(repo, settings, lines)
        except (gg.GitError, gg.PatternsFileError, gg.KeyFileError) as exc:
            print(f"git-guard: push refused: {exc}", file=sys.stderr)
            return 1
        if result.notice:
            print(f"git-guard: {result.notice}", file=sys.stderr)
        for finding in result.findings:
            print(f"git-guard: {finding.line()}", file=sys.stderr)
        if result.findings:
            print(f"git-guard: push refused: "
                  f"{len(result.findings)} finding(s)", file=sys.stderr)
            return 1
        return 0
    print(f"git-guard: unknown action {action!r}", file=sys.stderr)
    return 2


def cmd_prompt(args) -> int:
    """Print the prompt an agent would actually receive.

    A brief may now be composed of several files, and it is resolved across
    three config layers with a basename fallback on top. That is several places
    a surprise can hide, and "read agents.yaml and guess" stops being viable the
    moment a team supplies half the orchestrator's brief. This prints the
    result — preamble, brief, task — exactly as the agent would be handed it.
    """
    from dataclasses import replace

    from . import driver
    from .runner import Runner
    from .tree import Node

    paths = _resolve_if_project(args.path)
    config = load_config(paths)
    if args.team:
        config = replace(config, project={**config.project, "team": args.team})

    name = args.agent
    spec = config.agents.get(name)
    if spec is None:
        print(f"No agent named {name!r}. Known: {', '.join(sorted(config.agents))}",
              file=sys.stderr)
        return 1
    # The orchestrator's brief is team-dependent, so compose it the way the
    # launcher would rather than the way the roster entry reads.
    if spec.launch and spec.role:
        spec = driver._launched_spec(config, spec.role, config.team) or spec

    missing = config.missing_instructions(spec)
    root = paths.root if paths else Path.cwd()
    node = Node(id="ag-preview", agent=name, provider=spec.provider,
                model=spec.model, parent=None, depth=1,
                branch=f"{config.branch_prefix}/{name}/preview" if spec.writes else "",
                task=args.task)

    header = f"# {name} — {spec.provider}/{spec.model}"
    if config.team:
        header += f" — team {config.team!r}"
        if not spec.launch and not config.in_team(name):
            header += "  [NOT in this team's roster; start_agent would refuse it]"
    print(header)
    print(f"# briefs: {', '.join(config.instruction_parts(spec)) or '(none)'}")
    if missing:
        print(f"# MISSING: {', '.join(missing)} — resolves to nothing in any layer")
    print("#" + "-" * 70)
    if spec.launch:
        # A launched agent is handed its brief as a file, with no preamble and
        # no task: it is an MCP client, not a subagent.
        print(config.instructions_for(spec))
    else:
        print(Runner(_resolve(args.path), config)
              .compose_prompt(spec, args.task, node, root))
    return 1 if missing else 0


def cmd_doctor(args: argparse.Namespace) -> int:
    paths = _resolve_if_project(args.path)
    from .instance_strategy import InstanceStrategyError
    from .scheduler_config import SchedulerConfigError
    strategy_errors: list[str] = []
    scheduler_errors: list[str] = []
    try:
        config = load_config(paths)
    except (InstanceStrategyError, SchedulerConfigError):
        config = load_config(paths, instance_strategy_errors=strategy_errors,
                             scheduler_errors=scheduler_errors)
    providers = (load_providers(config.providers, config.provider_sources)
                 if hasattr(config, "provider_sources")
                 else load_providers(config.providers))
    if getattr(args, "clear", None):
        return _clear_provider(paths, providers, args.clear,
                               getattr(args, "force", False))
    if paths:
        summary = plans.list_plans(paths.root)
        for plan in summary["plans"]:
            if plan["status"] == "ready" and not plan["imported_in"]:
                print(f"ready plan   {plan['path']}")
        notes_dir = paths.root / "context" / "notes"
        if notes_dir.exists() or notes_dir.is_symlink():
            print(f"notes        {summary['notes']['unprocessed']} unprocessed")
    problems = len(strategy_errors) + len(scheduler_errors)
    for error in [*strategy_errors, *scheduler_errors]:
        print(f"  ! {error}")
    for warning in config.warnings:
        print(f"warning: {warning}")

    if paths and not config.project.get("scheduler", {}).get("enabled", False):
        from .scheduler import pending_nodes
        pending = pending_nodes(paths.root)
        if pending:
            print(f"  ! scheduler disabled with pending nodes: {', '.join(pending)}")
            problems += 1
    elif paths:
        # NC-R43: the scheduler and its plan; a dead one is a problem.
        from .scheduler import overview
        info, trouble = overview.doctor_lines(overview.section(paths.root, config))
        print("scheduler")
        for line in info:
            print(f"  {line}")
        for line in trouble:
            print(f"  ! {line}")
        problems += len(trouble)

    print("providers")
    for name, provider in sorted(providers.items()):
        transport_error = None
        if name in config.providers:
            try:
                transport_error = provider.transport_error()
            except ValueError as exc:
                transport_error = str(exc)
        if transport_error:
            print(f"  ! {transport_error}")
            problems += 1
        resolved = provider.resolve_bin()
        path = resolved.path
        state = "" if provider.enabled else "  [disabled in providers.yaml]"
        if path:
            print(f"  {name:12} {path} (via {resolved.via}){state}")
        elif not provider.enabled:
            # Disabled and absent is not a problem worth flagging — the user
            # said they do not want it.
            print(f"  {name:12} not installed{state}")
        else:
            print(f"  {name:12} NOT FOUND ({provider.bin_error(resolved)})")
            problems += 1

    # FA-R4: inspect ownership without constructing mounts or running actions.
    # Invalid mount ownership also makes auth/budget reads ambiguous, so report
    # it before those sections attempt to select a private profile.
    if paths and config.executor == "docker":
        conflicts = _docker_executor(paths).configuration_problems()
        if conflicts:
            print("\ncontainer private state")
            for conflict in conflicts:
                print(f"  !! {conflict}")
            problems += len(conflicts)
            print(f"\n{problems} problem(s)")
            return 1

    from .manifest import cli_dependencies_section

    print("\ncli dependencies")
    problems += cli_dependencies_section(paths, config, providers)

    print("\nagents")
    problems += _report_agents(config, providers)

    print("\nauth")
    providers_map = load_providers(config.providers)
    executor_of = executor_for(paths, config, providers_map)
    project_config = paths.config if paths else None
    for name, state in sorted(auth_mod.check_all(
            providers_map, executor_of, global_config_dir(),
            project_config).items()):
        mark = " " if state.ok else "!"
        print(f"  {mark} {name:10} {state.status:18} {state.detail[:60]}")
        profile_env = scripts.build_env(name, providers_map[name], executor_of(name))
        from .providers import resolved_profile
        profile = resolved_profile(providers_map[name]) or profile_env.get("HOME", str(Path.home()))
        # The check contract exposes identity only in its free-text detail.
        # A successful check or a token's presence alone identifies no account.
        identity = "identity unverified"
        if state.ok and state.detail.startswith("logged in as "):
            if state.detail.removeprefix("logged in as ").strip():
                identity = state.detail
        print(f"    profile {profile} — {identity}")
        if not state.ok:
            problems += 1
    for name, state in sorted(_driver_host_states(
            config, providers_map, executor_of, project_config).items()):
        mark = " " if state.ok else "!"
        print(f"  {mark} {name:10} {state.status:18} {state.detail[:60]}")
        print(f"    host profile — where the orchestrator itself runs")
        if not state.ok:
            problems += 1
            if state.fix:
                print(f"      fix: {state.fix}")

    print("\nbudget")
    _budget = read_all(providers_map, executor_of, global_config_dir(),
                       paths.config if paths else None)
    for name, entry in _budget.items():
        data = entry.to_dict()
        if data.get("known"):
            print(f"  {name:12} {data['used_percent']}% used, "
                  f"resets {reset_label(data.get('resets_at')) or '?'}")
            # Which bucket is the constraint changes what to do about it: a
            # rolling window clears in hours, a monthly one does not.
            for window, detail in sorted((data.get("windows") or {}).items()):
                # A window the provider does not spend against is shown but
                # labelled. agy resells Claude and GPT models from a separate
                # pool, and an unlabelled 100%-used row there reads as this
                # provider being out when it is not.
                aside = "" if detail.get("counted", True) else "  (separate pool)"
                # Wide enough for the longest bucket name any provider has
                # ("gemini-weekly"); short names still line up under it.
                print(f"  {'':12}   {window:13} {detail.get('percent', '?'):>5}%  "
                      f"resets {reset_label(detail.get('resets_at')) or '?'}{aside}")
        else:
            print(f"  {name:12} headroom unknown — {data.get('note','')}")
    if paths and config.executor == "docker":
        try:
            stale_mounts = _docker_executor(paths).mount_drift()
        except Exception:
            stale_mounts = []
        if stale_mounts:
            print(f"  !! the running container was created before the current "
                  f"configuration ({len(stale_mounts)} mount(s) differ):")
            for line in stale_mounts[:4]:
                print(f"  {'':5}{line}")
            try:
                warning = _docker_executor(paths).recreation_warning()
            except Exception:
                warning = ""
            print(f"  {'':5}Mounts are fixed at CREATION — `down` and `up` only "
                  f"stop and start.\n"
                  + (f"  {'':5}{warning}" if warning else
                     f"  {'':5}`multiagents docker rm && multiagents docker up` "
                     f"replaces it."))
            problems += 1
        try:
            drift = _docker_executor(paths).credential_drift()
        except Exception:
            drift = []
        for entry in drift:
            print(f"  !! the container is reading an OLD "
                  f"{Path(entry['path']).name} (inode "
                  f"{entry['container_inode']}, host has {entry['host_inode']}) "
                  f"— a refresh replaced it and a file mount follows the inode. "
                  f"`multiagents docker down && up`, or `run`, which repairs it "
                  f"when nothing is busy.")
            problems += 1
    health = Tree(paths.tree_file, paths.events_file).provider_health() if paths else {}
    for name, record in sorted(health.items()):
        if record.get("tripped"):
            print(f"  !! {name:9} stopped after {record['consecutive_failures']} "
                  f"consecutive failures: {record.get('last_reason','')[:70]}\n"
                  f"       if that cause is fixed: multiagents doctor --clear {name}")
            problems += 1

    if paths:
        print("\nproject")
        print(f"  root       {paths.root}")
        print(f"  executor   {config.executor}")
        print(f"  home       {config.home_policy}")
        print(f"  remote     {config.remote or '(none — local commits only)'}")
        repo_ok = gitops.is_repo(paths.root) and gitops.has_commits(paths.root)
        print(f"  git        {'ok' if repo_ok else 'NOT READY (needs a repo with at least one commit)'}")
        if not repo_ok:
            problems += 1
        blocked = set(config.env_block)
        # Compared on the NAME: an entry may be `KEY=value`, and matching the
        # whole string against the block list would let a blocked name through
        # simply by carrying a literal.
        leaking = [k for k in config.env_passthrough
                   if k.partition("=")[0].strip() not in blocked]
        print(f"  env        passthrough={leaking or 'none'} blocked={len(blocked)} vars")

    # A warning, never a problem: drift does not change the exit status, so
    # a machine with a copied layer is told about it without failing checks.
    print("\nconfig drift")
    drift_items = find_shadowing(paths)
    if not drift_items:
        print("  none")
    else:
        for item in drift_items:
            print(f"  {item.file}  {item.key_path}  [{item.kind}]")
            print(f"    {item.detail}")
            if item.kind == "stale_acknowledgement":
                print("    fix: remove the acknowledgement — it matches no drift")
            else:
                print(f"    fix: remove the key so the shipped value applies, "
                      f"or add {item.key_path} to drift_acknowledged")

    print(f"\n{'ok' if not problems else str(problems) + ' problem(s)'}")
    return 1 if problems else 0


def cmd_refresh_models(args: argparse.Namespace) -> int:
    paths = _resolve_if_project(args.path)
    config = load_config(paths)
    target = (paths.config if paths else global_config_dir()) / "models.yaml"
    providers = load_providers(config.providers)
    result = refresh_models(
        providers, target, config_dir=global_config_dir(),
        project_config=paths.config if paths else None,
        executor_for=executor_for(paths, config, providers) if paths else None)
    print(f"written {result['written']}")
    for name, count in result["counts"].items():
        print(f"  {name:12} {count} models")
    for name, problem in result["problems"].items():
        print(f"  {name:12} {problem}")
    return 0


def cmd_tree(args: argparse.Namespace) -> int:
    paths = _resolve(args.path)
    print(Tree(paths.tree_file, paths.events_file).render())
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Tail the global event log — the out-of-band view of a running tree."""
    from . import notices
    paths = _resolve(args.path)
    path = paths.events_file
    print(f"watching {path}  (ctrl-c to stop)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    with path.open() as handle:
        handle.seek(0, 2)
        try:
            while True:
                line = handle.readline()
                if not line:
                    time.sleep(0.5)
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                print(notices.event_line(event))
        except KeyboardInterrupt:
            return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    """Watch and steer the project, in a browser or in this terminal.

    Two front ends over one snapshot. The web page is the richer view — a tree
    with a transcript beside it, and config forms that know what each setting
    is — and the TUI is the one that works where there is no browser, which
    over ssh is most of the time.
    """
    paths = _resolve(args.path)
    if args.tui:
        from .monitor import tui
        return tui.run(paths)
    from .monitor import server
    return server.serve(paths, port=args.port, open_browser=args.browser,
                        allow_hosts=args.allow_host,
                        persistent_token=args.persistent_token,
                        rotate_token=args.rotate_token)


def cmd_probe(args: argparse.Namespace) -> int:
    """Run a trivial task through a provider and report how its stream parsed.

    The tool for adding an integration: it shows which rules matched and, more
    usefully, which lines fell through to `raw` and still need a rule.
    """
    paths = _resolve_if_project(args.path)
    config = load_config(paths)
    providers = load_providers(config.providers)
    provider = providers.get(args.provider)
    if provider is None:
        print(f"unknown provider {args.provider!r}; known: {sorted(providers)}", file=sys.stderr)
        return 2
    resolved = provider.resolve_bin()
    if resolved.launcher is None:
        print(provider.bin_error(resolved), file=sys.stderr)
        return 2

    argv = provider.build_command(
        prompt=args.prompt, model=args.model, workdir=str(Path.cwd()), permission="readonly",
    )
    if not provider.adapter:
        # H7 PS-R2a: invoke through the launcher, not the realpath.
        argv[0] = str(resolved.launcher)
    print(f"$ {' '.join(argv[:6])} …\n")
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=args.timeout)

    counts: dict[str, int] = {}
    unmatched: list[str] = []
    for line in proc.stdout.splitlines():
        event = provider.parse_line(line)
        if event is None:
            continue
        counts[event.kind] = counts.get(event.kind, 0) + 1
        if event.kind == "raw":
            unmatched.append(line[:200])

    print("parsed events")
    for kind, count in sorted(counts.items()):
        print(f"  {kind:10} {count}")
    if unmatched:
        print(f"\n{len(unmatched)} unmatched line(s) — add rules for these:")
        for line in unmatched[:8]:
            print(f"  {line}")
    else:
        print("\nevery line matched a rule")
    if proc.returncode != 0:
        print(f"\nexit {proc.returncode}: {proc.stderr[-400:]}")
    return 0


def _machine_state() -> list[Path]:
    """The two directories a teardown removes: config layer and state root."""
    return [global_config_dir(), state_root()]


def _worktree_survey() -> tuple[list[dict], set[Path]]:
    """`(worktrees with uncommitted work, repositories that register them)`.

    Both have to be collected before anything is deleted. The dirty check is
    the only thing standing between `rm -rf` and work that exists nowhere else
    — a commit survives in its repository as a branch, an uncommitted edit does
    not. The repository set is needed afterwards, and cannot be recovered once
    the `.git` files that name it are gone.
    """
    root = state_root() / "worktrees"
    dirty, repos = [], set()
    if not root.is_dir():
        return dirty, repos
    for worktree in sorted(root.glob("*/*")):
        if not worktree.is_dir():
            continue
        owner = gitops.owning_repo(worktree)
        if owner is not None:
            repos.add(owner)
        changed = gitops.run(worktree, "status", "--porcelain")
        if changed.ok and changed.out.strip():
            dirty.append({"path": worktree,
                          "files": len(changed.out.strip().splitlines()),
                          "repo": owner})
    return dirty, repos


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Remove this machine's global config and agent state.

    Per-project `.multiagents/` directories, your repositories and the branches
    agents committed to are all left alone — only machine-level state goes.
    """
    # Machine-level, so `--path` is not used — but a bad one is still refused,
    # and without `_resolve`'s registry write, which would itself be state.
    if getattr(args, "path", None) is not None:
        _explicit_root(args.path)
    targets = [p for p in _machine_state() if p.exists()]
    dirty, repos = _worktree_survey()
    # Retain exact registrations before removing machine state. A global Git
    # prune could act on an unrelated forged entry in the same repository.
    orphaned: dict[Path, list[tuple[Path, str]]] = {}
    worktrees_root = state_root() / "worktrees"
    for repo in repos:
        held = []
        if worktrees_root.is_dir():
            for path in worktrees_root.glob("*/*"):
                branch = gitops._registration_branch(repo, path)
                if branch:
                    held.append((path, branch))
        orphaned[repo] = held

    for path in targets:
        print(f"would remove  {path}")
    if not targets:
        print("nothing to remove; this machine has no multiagents state.")
        return 0
    print(f"registered in {len(repos)} repository(ies), which will be pruned")

    if dirty:
        print(f"\n{len(dirty)} worktree(s) hold uncommitted work:")
        for entry in dirty:
            print(f"  {entry['files']:3} file(s)  {entry['path']}")
        print("\nCommitted work survives — it lives in its repository as a branch.")
        print("These changes do not. Commit or copy them, or pass --force.")
        if not args.force:
            return 1

    if args.dry_run:
        print("\ndry run; nothing was removed.")
        return 0
    if not args.force and not _confirm("\nremove them?"):
        print("cancelled.")
        return 0

    for path in targets:
        shutil.rmtree(path, ignore_errors=True)
        print(f"removed  {path}")
    # After deletion, not before: git only drops a registration once the
    # directory is actually gone, so pruning first would be a no-op and leave
    # every repository listing worktrees that no longer exist.
    pruned = 0
    for repo in sorted(repos):
        if gitops.is_repo(repo):
            for path, branch in orphaned[repo]:
                gitops.prune_worktree(repo, path, branch)
            pruned += 1
    print(f"pruned   stale worktree registrations in {pruned} repository(ies)")
    print("\nPer-project .multiagents/ directories are untouched.")
    return 0


def _clean_malformed(node) -> str | None:
    """HA-R12: why `clean` cannot read this tree entry, or None if it can.

    `tree.json` is container-writable; an entry that is not a mapping, or
    whose branch or worktree is not a string, names nothing `clean` may act on.
    """
    if not isinstance(node, dict):
        return f"entry is {type(node).__name__}, not a mapping"
    for key in ("branch", "worktree"):
        if node.get(key) is not None and not isinstance(node[key], str):
            return f"{key} is {type(node[key]).__name__}, not a string"
    return None


def cmd_clean(args: argparse.Namespace) -> int:
    paths = _resolve(args.path)
    tree = Tree(paths.tree_file, paths.events_file)
    authority = HostAuthority(paths, tree)
    data = tree.read()
    removed = 0

    if args.branches:
        base = gitops.current_branch(paths.root)
        # HA-R9: liveness, record and everything derived are the key's. An
        # entry naming another id can neither complete nor unprotect that node.
        for node_id, node in data["nodes"].items():
            reason = _clean_malformed(node)
            if reason:
                tree.emit(node_id, "malformed_entry", node=node_id,
                          action="clean", reason=reason)
                continue
            if node.get("id") != node_id:
                tree.emit(node_id, "host_authority_mismatch", node=node_id,
                          action="clean", fields=["id"])
                continue
            record = authority.get(node_id)
            if record:
                fields = [key for key in ("branch", "worktree", "parent")
                          if node.get(key) != record.get(key)]
                if fields:
                    tree.emit(node_id, "host_authority_mismatch", node=node_id,
                              action="clean", fields=fields)
            branch = record["branch"] if record else node.get("branch")
            if not branch or node.get("status") in {"running", "pending"}:
                continue
            if (not record and not authority.safe_unrecorded_branch(branch)) or (record and not branch.startswith("agents/")):
                tree.emit(node_id, "host_authority_mismatch", node=node_id,
                          action="clean", fields=["branch"])
                continue
            if CHECKPOINT_FAILED in (node.get("reason") or "") and not args.force:
                print(f"keep   {branch} (its checkpoint failed; --force to delete)")
                continue
            commits = gitops.commits_on(paths.root, branch, base, root=paths.root)
            if commits and not args.force:
                print(f"keep   {branch} ({commits} unmerged commit(s); --force to delete)")
                continue
            worktree = record["worktree"] if record else node.get("worktree")
            if worktree and record and record["seeded"] and not authority.safe_seeded_path(Path(worktree)):
                tree.emit(node_id, "host_authority_mismatch", node=node_id,
                          action="clean", fields=["worktree"])
                continue
            if worktree and not record and not authority.safe_nested_path(Path(worktree)):
                tree.emit(node_id, "host_authority_mismatch", node=node_id,
                          action="clean", fields=["worktree"])
                continue
            if worktree:
                try:
                    registered = gitops._registration_branch(paths.root, Path(worktree),
                                                             branch)
                except gitops.GitError:
                    tree.emit(node_id, "host_authority_mismatch", node=node_id,
                              action="clean", fields=["worktree", "branch"])
                    continue
                if registered and registered != branch:
                    tree.emit(node_id, "host_authority_mismatch", node=node_id,
                              action="clean", fields=["worktree", "branch"])
                    continue
            if worktree and (Path(worktree).exists() or Path(worktree).is_symlink()):
                if not authority.remove_worktree(Path(worktree), recorded=bool(record)):
                    tree.emit(node_id, "host_authority_mismatch", node=node_id,
                              action="clean", fields=["worktree"],
                              reason="worktree could not be removed within its authorised path")
                    continue
            if worktree and registered and not Path(worktree).exists():
                gitops.prune_worktree(paths.root, Path(worktree), branch)
            if record:
                authority.complete(node_id, "discarded")
            gitops.delete_branch(paths.root, branch, force=True)
            print(f"delete {branch}")
            removed += 1

    if args.tree:
        from multiagents.tree import TERMINAL
        skipped = {}
        with tree.transaction() as state:
            skipped = {nid: reason for nid, n in state["nodes"].items()
                       if (reason := _clean_malformed(n))}
            drop = [
                nid for nid, n in state["nodes"].items()
                if nid not in skipped and n.get("status") in TERMINAL and not n.get("branch")
                and not n.get("conversation")
            ]
            for nid in drop:
                state["nodes"].pop(nid, None)
            for nid, n in state["nodes"].items():
                if nid not in skipped:
                    n["children"] = [c for c in n.get("children", []) if c not in drop]
        for nid, reason in skipped.items():
            tree.emit(nid, "malformed_entry", node=nid, action="clean", reason=reason)
        print(f"pruned {len(drop)} finished node(s) holding no branch")
        removed += len(drop)

    if args.homes:
        for home in paths.homes.glob("ag-*"):
            node = data["nodes"].get(home.name)
            reason = _clean_malformed(node) if home.name in data["nodes"] else None
            if reason:
                tree.emit(home.name, "malformed_entry", node=home.name,
                          action="clean", reason=reason)
                continue
            if node and node.get("status") in {"running", "pending"}:
                continue
            shutil.rmtree(home, ignore_errors=True)
            removed += 1

    print(f"\n{removed} item(s) removed")
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    paths = _resolve_if_project(args.path)
    config = load_config(paths)
    if args.update:
        result = catalog_mod.apply(global_config_dir(), args.provider)
        print(result.get("written") or result.get("error"))
        return 0 if result.get("ok") else 1
    return 0 if _report_catalog(config, args.provider) == 0 else 0


def _docker_executor(paths):
    from .executor.docker import DockerExecutor
    config = load_config(paths)
    return DockerExecutor(
        config.project.get("executor", {}).get("docker", {}),
        paths,
        load_providers(config.providers),
        global_config_dir(),
    )




def _auth_scope(provider, executor) -> str:
    """Where this provider's credentials actually live.

    Not the same as where its agents run: opencode keeps credentials on the
    host even under docker, because the container mounts its data directory
    rather than masking it. Only a provider that declares
    container_private_home authenticates inside the container.
    """
    if getattr(executor, "kind", "local") == "docker" and \
       getattr(provider, "container_private_home", None):
        return "container"
    return "host"


def _driver_host_states(config, providers, executor_of, project_config):
    """The HOST login for every provider that launches a driver, when that is
    a second profile nobody was checking.

    `run` and `init-agent` exec a CLI on this machine, so a driver's provider
    is authenticated here no matter where its agents run. Under docker that is
    a different stored login from the one `check` reports by default, and it
    was invisible: `multiagents auth` said claude was fine while the profile
    the orchestrator runs on could be signed out entirely.

    Only driver providers, and only under docker. Checking every provider's
    host profile would cost an API call apiece for the ones with no file to
    read, to answer a question about agents that never run here.
    """
    out = {}
    for spec in config.agents.values():
        if not getattr(spec, "launch", False) or spec.provider in out:
            continue
        provider = providers.get(spec.provider)
        if provider is None:
            continue
        executor = executor_of(spec.provider)
        if _auth_scope(provider, executor) != "container":
            continue                  # one profile; the ordinary row IS the host
        out[spec.provider] = auth_mod.check(
            spec.provider, provider, executor, global_config_dir(),
            project_config, profile=auth_mod.HOST)
    return out


def cmd_skills(args: argparse.Namespace) -> int:
    """List the capability fragments an agent can be given.

    A skill is not a mechanism — it is a brief file under `agents/skills/`,
    resolved and attached exactly like any other. What it needs from the CLI is
    therefore not resolution but VISIBILITY: without a listing, a capability
    that ships in the package is one nobody knows to attach, and the roster is
    the wrong place to go looking because the roster is about who exists.
    """
    paths = _resolve_if_project(args.path)
    config = load_config(paths)

    seen: dict[str, Path] = {}
    for base in config.instruction_dirs:             # shipped, global, project
        for path in sorted((base / "skills").glob("*.md")):
            if path.name != "README.md":
                seen[f"skills/{path.name}"] = path   # later layer wins, as ever

    if not seen:
        print("no skills. They live in agents/skills/ in any config layer.")
        return 0

    holders: dict[str, list[str]] = {}
    for name, spec in config.agents.items():
        for part in config.instruction_parts(spec):
            holders.setdefault(part, []).append(name)
    for team, spec in (config.teams or {}).items():
        for part in spec.get("orchestrator") or []:
            if part in seen:
                holders.setdefault(part, []).append(f"orchestrator ({team})")

    for name, path in sorted(seen.items()):
        words = len(path.read_text().split())
        who = ", ".join(sorted(set(holders.get(name, [])))) or "nobody"
        print(f"  {name:28} {words:5}w  {who}")
    print(f"\n{len(seen)} skill(s) in {config.instruction_dirs[-1].parent}")
    print("attach one by naming it in an agent's `instructions` list — and in")
    print("the team's `orchestrator:` list too, which replaces rather than adds.")
    return 0


def cmd_upgrade_config(args: argparse.Namespace) -> int:
    """Refresh config copies that were never edited.

    Pinned copies override the shipped defaults for every key, so without this
    an install silently never receives improvements to files it is not using.
    Edited files are left alone and reported.
    """
    from .paths import shipped_defaults_dir
    paths = _resolve_if_project(args.path)
    verb = "would " if args.dry_run else ""

    targets = [("global", shipped_defaults_dir(), global_config_dir(), "global")]
    if paths is not None:
        targets.append(("project", global_config_dir(), paths.config, "project"))
    if args.layer != "both":
        targets = [t for t in targets if t[0] == args.layer]

    stale = 0
    for label, source, target, scope in targets:
        report = sync_layer(source, target, force=args.force,
                            dry_run=args.dry_run, scope=scope)
        print(f"{label}  {target}")
        for name in report["added"]:
            print(f"  {verb}add     {name}")
        for name in report["updated"]:
            print(f"  {verb}refresh {name}   (unmodified copy)")
        for name in report["customised"]:
            stale += 1
            print(f"  keep    {name}   (you edited it; shipped version has changed)")
        for path in report.get("backed_up", []):
            print(f"  backup  {path}")
        if not any(report.values()):
            print("  up to date")
    if stale:
        print(f"\n{stale} customised file(s) left alone. Compare them against")
        print(f"  {shipped_defaults_dir()}")
        print("and merge by hand, or pass --force to overwrite.")
    return 0


def _alias_provider_arg(name: str) -> str:
    """OG-R2: a provider named on the command line in its pre-rename spelling.

    An argument has no file to point at, so the warning says the sentence
    without one; it is printed on stderr, where every other CLI complaint goes,
    and the caller carries on with the route it meant.
    """
    renames = shipped_renames()
    if not renames.is_alias(name):
        return name
    print(f"warning: {renames.warning(name)}", file=sys.stderr)
    return renames.canonical(name)


def cmd_auth(args: argparse.Namespace) -> int:
    paths = _resolve_if_project(args.path)
    config = load_config(paths)
    providers = load_providers(config.providers)
    project_config = paths.config if paths else None
    executor_of = executor_for(paths, config, providers)
    # `status` lists every provider, but the name was still given, and a
    # pre-rename spelling is worth saying so about whichever action it was.
    given = _alias_provider_arg(getattr(args, "provider", "") or "")

    if args.action == "login":
        name = given
        provider = providers.get(name)
        if provider is None:
            print(f"unknown provider {name!r}; known: {sorted(providers)}", file=sys.stderr)
            return 2
        # PS-R3: a dependent borrows the owner's login action — and says so,
        # because the browser session that follows is the owner's account.
        pinned = (getattr(provider, "container_account", "") and
                  getattr(executor_of(name), "kind", "") == "docker" and
                  not getattr(args, "host", False))
        owner, selected_provider = _credential_owner(name, providers)
        if not pinned:
            provider = selected_provider
        if owner != name:
            print(f"{name} uses {owner}'s login")
            if not pinned:
                name = owner
        extra = {}
        if getattr(args, "account", ""):
            # Passed to the script rather than resolved here: only it knows
            # what a profile for this provider is shaped like.
            extra["MULTIAGENTS_ACCOUNT"] = args.account
        built = auth_mod.login_command(
            name, provider, executor_of(name), global_config_dir(), project_config,
            profile=auth_mod.HOST if getattr(args, "host", False) else "",
            extra_env=extra)
        if built is None:
            print(f"no auth script for {name!r}. Add one to "
                  f"{global_config_dir()}/auth/{name}.sh — see auth/README.md.",
                  file=sys.stderr)
            return 2
        argv, env = built
        sys.stdout.flush()
        return driver._hand_over(argv, env, scripts.resolve(
            name, provider, global_config_dir(), project_config))

    states = auth_mod.check_all(providers, executor_of, global_config_dir(), project_config)
    broken = 0
    for name, state in sorted(states.items()):
        where = _auth_scope(providers[name], executor_of(name))
        mark = "ok " if state.ok else ("!! " if state.status == "not_authenticated" else "?  ")
        print(f"  {mark}{name:10} [{where:9}] {state.detail}")
        if not state.ok:
            broken += 1
            if state.fix:
                print(f"      fix: {state.fix}")
    # A second row rather than a footnote on the first: it is a different
    # login, of a different account potentially, fixed by a different command.
    # Folding it into one line is how it stayed invisible.
    for name, state in sorted(_driver_host_states(
            config, providers, executor_of, project_config).items()):
        mark = "ok " if state.ok else ("!! " if state.status == "not_authenticated" else "?  ")
        print(f"  {mark}{name:10} [host     ] {state.detail}"
              f"   ← the orchestrator runs here")
        if not state.ok:
            broken += 1
            if state.fix:
                print(f"      fix: {state.fix}")
    print()
    print("all providers authenticated" if not broken
          else f"{broken} provider(s) need attention")
    return 1 if broken else 0


def cmd_ask(args: argparse.Namespace) -> int:
    """Answer questions agents have parked on.

    Deliberately WRITE-ONLY. Resuming an agent means owning the asyncio task
    that reads its stdout; this process exits immediately afterwards, which
    would leave the agent running with nobody draining its pipe — it would fill
    and deadlock. So the answer is recorded here, and the agent is resumed by
    whichever process owns a live runner: the orchestrator, next time it calls
    list_questions, answer_question, wait_for_agents or check_agent.
    """
    paths = _resolve(args.path)
    tree = Tree(paths.tree_file, paths.events_file)
    questions = tree.open_questions(args.agent or None)

    if not questions:
        print("Nothing is waiting on a decision.")
        return 0

    if args.list:
        for q in questions:
            waited = (time.time() - q["asked_at"]) / 60
            print(f"  {q['id']}  {q['agent']} · {q['topic']}   asked {waited:.0f}m ago")
            print(f"      {q['question']}")
            if q.get("proposed_default"):
                print(f"      its default: {q['proposed_default']}")
        return 0

    answered = 0
    for q in questions:
        if args.question_id and q["id"] != args.question_id:
            continue
        waited = (time.time() - q["asked_at"]) / 60
        print(f"\n[{q['id']}] {q['agent']} · {q['topic']}   asked {waited:.0f}m ago")
        print(f"  {q['question']}")
        if q.get("proposed_default"):
            print(f"  it would otherwise choose: {q['proposed_default']}")
        try:
            reply = input("  your answer (blank to skip, 'default' to accept its own): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not reply:
            continue
        if reply == "default" and q.get("proposed_default"):
            reply = q["proposed_default"]
        tree.answer_question(q["id"], reply, answered_by="user")
        answered += 1
        print(f"  recorded. {q['agent']} resumes when the orchestrator next checks.")

    if answered:
        print(f"\n{answered} answered. Run `multiagents run` if no orchestrator is live.")
    return 0


def _report_checks(paths) -> int:
    """What was checked, by what, and how the check ended.

    Reported as the graph rather than as a rework *rate*. Whether a review
    found something is in its prose, and deciding that from here would be the
    same mistake as classifying a failure from an agent's own words. What can
    be stated is what checked what, and how each check ended — enough for a
    person, or an orchestrator, to see when work is being redone.
    """
    nodes = Tree(paths.tree_file, paths.events_file).read()["nodes"]
    links = [n for n in nodes.values() if n.get("verifies")]
    if not links:
        print("No checks have been declared.\n"
              "`start_agent(..., verifies=<agent_id>)` records that a run checks "
              "another's work;\nwithout it, what was rechecked cannot be told "
              "from what was merely done next.")
        return 0

    by_target = {}
    for node in links:
        by_target.setdefault(node["verifies"], []).append(node)
    print(f"{'work':34} {'checked by':34} outcome")
    for target, checks in sorted(by_target.items()):
        subject = nodes.get(target, {})
        label = f"{target} {subject.get('agent', '?')}"
        for check in sorted(checks, key=lambda c: c.get("started_at") or 0):
            verdict = check.get("verdict") or ""
            detail = (f"{verdict}"
                      + (f" ({check.get('defects')} defect(s))"
                         if check.get("defects") else "")) if verdict else check["status"]
            print(f"{label:34} {check['id'] + ' ' + check['agent']:34} {detail}")
            label = ""

    rechecked = sum(1 for c in by_target.values() if len(c) > 1)
    rejected = {t for t, cs in by_target.items()
                if any(c.get("verdict") == "rejected" for c in cs)}
    judged = {t for t, cs in by_target.items() if any(c.get("verdict") for c in cs)}
    print(f"\n{len(by_target)} run(s) checked, {len(links)} check(s); "
          f"{rechecked} needed more than one.")
    if judged:
        # A rate at last, and only over the work that was actually judged:
        # counting unjudged runs as approved would flatter it.
        print(f"{len(rejected)} of {len(judged)} judged run(s) were rejected "
              f"({len(rejected) / len(judged) * 100:.0f}% rework) — work that "
              f"finished and had to be redone anyway.")
    if len(judged) < len(by_target):
        print(f"{len(by_target) - len(judged)} checked run(s) got no verdict; "
              f"those cannot be counted either way.")
    return 0


def _report_mcp_overhead(hours: float) -> int:
    """The other direction of spend: what the orchestrator pays to run us.

    `usage` proper reports what the agents cost, from our own stream
    accounting. This reports what attaching our MCP server costs the human's
    Claude subscription, from Claude Code's local transcripts — the two are
    different budgets and only one of them shows up in the agent tree.
    """
    from .transcripts import BIG_CONTEXT, analyse

    report = analyse(window_hours=hours)
    if not report.requests:
        print(f"No Claude Code requests in the last {hours:g}h.")
        return 0

    print(f"last {hours:g}h · {report.requests} requests · "
          f"{report.sessions} sessions · ${report.cost_usd:.2f} weighted")
    print()
    if report.by_server:
        print("MCP servers")
        for name, cost in sorted(report.by_server.items(), key=lambda kv: -kv[1]):
            share = report.share(cost)
            print(f"  {name:22} {share:6.1%}  ${cost:8.2f} {'#' * int(share * 40)}")
        # Stated every time, because the number is a floor and reading it as a
        # total is the one way to be wrong with it.
        print("  (tool definitions are not counted — each server's share is a floor)")
    else:
        print("No MCP server activity in this window.")

    print()
    print("models")
    for name, cost in sorted(report.by_model.items(), key=lambda kv: -kv[1]):
        if cost:
            print(f"  {name:22} {report.share(cost):6.1%}  ${cost:8.2f}")
    print()
    print(f"{report.share(report.big_context_usd):.0%} of it was spent at "
          f">{BIG_CONTEXT // 1000}k context")
    print("Dollars weight one model against another; a subscription is not "
          "billed at API rates.")
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    """Where this project's tokens and dollars actually went.

    Per provider/model rather than per provider, because that is the level at
    which a pin is a decision: no provider reports it, but we parse every
    stream, so it is ours to compute.
    """
    paths = _resolve(args.path)
    if args.checks:
        return _report_checks(paths)
    if getattr(args, "mcp", False):
        return _report_mcp_overhead(args.hours)
    rows = billed_rows(Tree(paths.tree_file, paths.events_file).usage_by_model(),
                       load_providers(load_config(paths).providers))
    if not rows:
        print("No usage recorded yet.")
        return 0

    total_cost = sum(r["cost_usd"] for r in rows)
    total_tokens = sum(r["tokens"] for r in rows)
    print(f"{'provider/model':42} {'runs':>4} {'tokens':>12} {'cost':>9}  share")
    for r in rows:
        share = (100 * r["cost_usd"] / total_cost) if total_cost else 0
        bar = "#" * int(share / 4)
        cost = (f"{'plan':>9}" if r.get("billing") == "plan"
                else f"${r['cost_usd']:>8.4f}")
        print(f"{r['provider'] + '/' + r['model']:42} {r['runs']:>4} "
              f"{r['tokens']:>12,} {cost}  {share:4.0f}% {bar}")
        if args.agents:
            print(f"{'':42}      {', '.join(r['agents'])}")
    print(f"\n{'total':42} {sum(r['runs'] for r in rows):>4} "
          f"{total_tokens:>12,} ${total_cost:>8.4f}")

    # A provider that bills per token and one that does not are not comparable,
    # so say which is which rather than letting a $0.00 row read as free.
    free = [r["provider"] for r in rows
            if r["cost_usd"] == 0 and r.get("billing") != "plan"]
    if free:
        print(f"\n{', '.join(sorted(set(free)))} report no per-token cost "
              f"(subscription); their tokens are real but their dollars are not.")
    return 0


def _refresh_quota_line(name: str, b, cleared: dict, lifted_pause_for: set[str]) -> str:
    """One line: what a forced re-read of `name` found, and what it lifted."""
    if b is None or not b.known:
        note = (b.note if b is not None else "") or "no reading"
        return f"{name:14} unknown      {note}"
    window = None
    if b.windows:
        window = max(b.windows, key=lambda w: b.windows[w].get("percent") or 0)
    window_bit = f" {window}" if window else ""
    when = f", resets {reset_label(b.resets_at)}" if b.resets_at else ""
    source_bit = f" via {b.source}" if b.source else ""
    bits = []
    if name in (cleared.get("cooldowns") or []):
        bits.append("cooldown cleared")
    if name in lifted_pause_for:
        bits.append("pause lifted")
    cleared_bit = f" — {', '.join(bits)}" if bits else ""
    return (f"{name:14} {b.headroom * 100:.0f}% headroom{window_bit}{when}"
            f"{source_bit}{cleared_bit}")


def cmd_refresh_quota(args: argparse.Namespace) -> int:
    """Re-read one or more providers' quotas past every cache, and lift any
    cooldown/pause a fresh reading proves is no longer warranted (QF-R4)."""
    paths = _resolve(args.path)
    config = load_config(paths)
    providers = load_providers(config.providers)
    names = ([_alias_provider_arg(n) for n in args.providers]
             if args.providers else sorted(providers))
    unknown = [n for n in names if n not in providers]
    if unknown:
        print(f"unknown provider(s): {', '.join(unknown)}")
        return 2

    executor_of = executor_for(paths, config, providers)
    to_read = {n: providers[n] for n in names}
    budgets = read_all(to_read, executor_of, global_config_dir(), paths.config, {}, {},
                       use_cache=False, force=True)

    tree = Tree(paths.tree_file, paths.events_file)
    prior_pause_providers = set(tree.pause_state().get("providers") or [])
    usable = {n for n, b in budgets.items() if b is not None and b.known and b.usable}
    cleared = tree.clear_quota(usable) if usable else {"cooldowns": [], "pause": False}
    lifted_pause_for = prior_pause_providers if cleared.get("pause") else set()

    for name in names:
        print(_refresh_quota_line(name, budgets.get(name), cleared, lifted_pause_for))

    spec = driver._launched_spec(config, "orchestrator", config.team)
    if spec is not None and spec.provider in providers:
        if spec.provider in budgets:
            own = budgets[spec.provider]
        else:
            own = read_all({spec.provider: providers[spec.provider]}, executor_of,
                           global_config_dir(), paths.config, {}, {}).get(spec.provider)
        if own is not None and own.known and not own.usable:
            return 3
    return 0


def cmd_tickets(args: argparse.Namespace) -> int:
    """Review and submit bug tickets the agents filed against multiagents.

    Submitting is deliberately a user action by default. The ticket is public
    writing about this machine, and `list`/`show` exist so that the decision is
    made after reading it rather than before.
    """
    paths = _resolve(args.path)
    config = load_config(paths)
    tree = Tree(paths.tree_file, paths.events_file)
    tickets = tree.read().get("tickets", [])

    if args.action == "show":
        ticket = tree.get_ticket(args.ticket_id) if args.ticket_id else None
        if ticket is None:
            print(f"unknown ticket {args.ticket_id!r}", file=sys.stderr)
            return 2
        print(f"{ticket['id']}  [{ticket['severity']}]  {ticket['status']}"
              + (f"  ·  filed against multiagents {ticket['tooling']}"
                 if ticket.get("tooling") else ""))
        print(f"{ticket['title']}\n")
        print(bugs.render(ticket))
        return 0

    if args.action == "submit":
        ticket = tree.get_ticket(args.ticket_id) if args.ticket_id else None
        if ticket is None:
            print(f"unknown ticket {args.ticket_id!r}", file=sys.stderr)
            return 2
        ok, why = bugs.can_submit(config)
        if not ok:
            print(f"cannot submit: {why}")
            print(f"\nthe ticket is readable with `multiagents tickets show {ticket['id']}`")
            return 1
        print(bugs.render(ticket))
        print(f"-> {bugs.settings(config)['repo']}")
        if not _confirm("file this as an issue?"):
            print("not submitted.")
            return 0
        sent, result = bugs.submit(config, ticket)
        if not sent:
            print(f"failed: {result}", file=sys.stderr)
            return 1
        tree.set_ticket_status(ticket["id"], "reported", "submitted by the user", result)
        print(f"reported: {result}")
        return 0

    if args.action == "discard":
        if tree.set_ticket_status(args.ticket_id, "declined", "discarded by the user") is None:
            print(f"unknown ticket {args.ticket_id!r}", file=sys.stderr)
            return 2
        print(f"{args.ticket_id} discarded.")
        return 0

    if args.action == "resolve":
        # `submit` and `discard` could open a ticket's life and end it
        # unread, and nothing could close one you fixed: marking a ticket
        # `fixed` existed only as an MCP tool, which the orchestrator can reach
        # and you cannot. So a ticket you reported and then fixed stayed
        # `reported` for ever unless somebody edited the tree by hand.
        ticket = tree.get_ticket(args.ticket_id) if args.ticket_id else None
        if ticket is None:
            print(f"unknown ticket {args.ticket_id!r}", file=sys.stderr)
            return 2
        outcome = "declined" if args.declined else "fixed"
        record = tree.set_ticket_status(args.ticket_id, outcome, args.note)
        print(f"{args.ticket_id} marked {outcome}"
              f"{f': {args.note}' if args.note else ''}")
        if outcome == "fixed" and record.get("url"):
            # It was filed upstream, so somebody there is still looking at it.
            print(f"             this was reported at {record['url']} — close it "
                  f"there too, or it stays open for everyone else")
        elif outcome == "fixed" and record.get("status") != "reported":
            print("             not reported upstream; the defect is still there "
                  "for everyone else.\n             `tickets submit "
                  f"{args.ticket_id}` if it is worth filing")
        return 0

    shown = [t for t in tickets
             if args.all or t.get("status") in ("open", "awaiting_user")]
    if not shown:
        print("No open tickets." if tickets else "No tickets have been filed.")
        return 0
    for ticket in shown:
        mark = "!" if ticket["severity"] == "blocking" else " "
        age = (time.time() - ticket["filed_at"]) / 60
        print(f" {mark} {ticket['id']}  {ticket['status']:14} filed {age:.0f}m ago"
              f"  {ticket['title'][:60]}")
        if ticket.get("url"):
            print(f"     {ticket['url']}")
    ok, why = bugs.can_submit(config)
    print(f"\n{len(shown)} ticket(s). `tickets show <id>` to read one, "
          f"`tickets submit <id>` to file it.")
    if not ok:
        print(f"note: {why}")
    return 0


def _docker_status_all() -> int:
    """Every project's containers, with the slugs resolved back to paths.

    Project-scoped commands cannot answer "what is running on this machine",
    and `docker ps` answers it in slugs, which are hashes and do not invert.
    The registry written at init is what turns them back into paths.
    """
    from .executor.docker import docker_state, list_containers

    state, detail = docker_state()
    if state != "ok":
        print(f"docker is not usable: {detail}", file=sys.stderr)
        return 1

    rows = list_containers()
    if not rows:
        print("No multiagents containers on this machine.")
        return 0

    known = known_projects()
    by_slug: dict[str, dict] = {}
    for row in rows:
        entry = by_slug.setdefault(row["slug"], {"workspace": None, "proxy": None})
        entry["proxy" if row["proxy"] else "workspace"] = row

    print(f"{'project':44} {'workspace':16} {'proxy':16}")
    running = 0
    for slug in sorted(by_slug):
        entry = by_slug[slug]
        record = known.get(slug)
        if record:
            path = Path(record["path"])
            label = str(path)
            if not path.is_dir():
                label += "  (gone)"
        else:
            # Registered only from `init` onwards, so a container made before
            # that has no path. Say so rather than printing a bare hash.
            label = f"{slug}  (path unknown)"
        home = str(Path.home())
        if label.startswith(home):
            label = "~" + label[len(home):]

        def short(row):
            if row is None:
                return "-"
            return "running" if row["status"].startswith("Up") else "stopped"

        print(f"{label[:44]:44} {short(entry['workspace']):16} {short(entry['proxy']):16}")
        running += sum(1 for r in entry.values()
                       if r and r["status"].startswith("Up"))

    print(f"\n{len(by_slug)} project(s), {running} container(s) running.")
    print("Each running project holds its resource ceiling whether or not agents "
          "are working:\nstop one with `multiagents --path <project> docker down`.")
    return 0


def cmd_supervise(args: argparse.Namespace) -> int:
    """Watch a launched agent from outside it. Started by `run`; rarely typed."""
    from . import watchdog

    paths = _resolve(args.path)
    return watchdog.supervise(paths, load_config(paths), args.role, args.pid,
                              interval=args.interval, max_seconds=args.max_seconds)


def cmd_status(args: argparse.Namespace) -> int:
    """What is driving this project, from outside it.

    Both roles report: `run` launches the orchestrator and `init-agent` the
    initializer, and either may be the one doing the work. Reading only the
    orchestrator's file meant that while init-agent was running, its state was
    printed under the orchestrator's name.
    """
    from . import watchdog

    paths = _resolve(args.path)
    records = watchdog.read_all_status(paths)
    if not records:
        print("No supervisor has reported yet. `multiagents run` starts one.")
        return 0

    for role, record in records.items():
        age = time.time() - record.get("at", 0)
        stale = "  (stale)" if age > 120 else ""
        live = "" if record.get("running") else "  (ended)"
        print(f"{role:14} {record['verdict']} — {record['detail']}{live}")
        print(f"{'':14} observed {age:.0f}s ago{stale}")
        transcript = record.get("transcript") or {}
        if transcript:
            print(f"{'':14} transcript quiet for {transcript.get('quiet_for', '?')}s")
        print(f"{'':14} {record.get('active_agents', 0)} agent(s) running")
        limit = record.get("limit") or {}
        if limit.get("said"):
            print(f"{'':14} it said: {limit['said']}")
        provider = record.get("provider") or {}
        if provider.get("known") and provider.get("headroom") is not None:
            print(f"{'':14} {provider['name']} headroom "
                  f"{provider['headroom'] * 100:.0f}%"
                  f"{', resets ' + reset_label(provider['resets_at']) if provider.get('resets_at') else ''}")
        if len(records) > 1:
            print()
    return 0


def _with_descendants(tree: Tree, active: list, root: str) -> list:
    """`root` and every active node below it, deepest first (SV-R10: the node
    and its children). Children go before their parent, so none is orphaned
    while the parent's own server is still there to cancel it on its way out.
    Parent links come from the whole tree, so a child is still found through
    a parent that has already ended."""
    parent_of = {key: value.get("parent") for key, value in tree.read()["nodes"].items()}

    def depth_under(node_id: str | None) -> int | None:
        depth, seen = 0, set()
        while node_id and node_id not in seen:
            if node_id == root:
                return depth
            seen.add(node_id)
            node_id = parent_of.get(node_id)
            depth += 1
        return None

    ranked = [(depth_under(n.id), n) for n in active]
    return [n for d, n in sorted((pair for pair in ranked if pair[0] is not None),
                                 key=lambda pair: -pair[0])]


def cmd_stop(args: argparse.Namespace) -> int:
    """Bring everything to a halt without losing any of it.

    The counterpart to `run`. Resumable is the requirement, so this stops
    processes and leaves state: branches, worktrees, sessions, the deferred
    queue, open questions and tickets all survive, and an agent that was
    working keeps a session id that `steer_agent` can pick up.
    """
    paths = _resolve(args.path)
    config = load_config(paths)
    tree = Tree(paths.tree_file, paths.events_file)
    authority = HostAuthority(paths, tree)
    # SV-R10: `stop <id>` ends that one agent — wherever it runs, whether or
    # not a session is attached — and leaves the sessions and the rest alone.
    only = getattr(args, "agent_id", "") or ""
    if only and getattr(args, "all", False):
        print("give an agent id or --all, not both", file=sys.stderr)
        return 2
    if only:
        node = tree.get(only)
        if node is None:
            print(f"no agent {only!r} in this project", file=sys.stderr)
            return 1
        if node.status not in ACTIVE:
            print(f"{only} is not running ({node.status})")
            return 0

    # --- whatever is driving the project -----------------------------------
    stopped_drivers = []
    for role in () if only else ("orchestrator", "orchestrator-turn", "initializer",
                                 "initializer-turn"):
        path = driver._pid_file(paths, role)
        if not path.is_file():
            continue
        recorded = driver._read_pid(paths, role)
        if recorded is None:
            path.unlink(missing_ok=True)
            continue
        pid, pid_start = recorded
        if not driver._alive(pid, pid_start):
            path.unlink(missing_ok=True)
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            stopped_drivers.append(f"{role} (pid {pid})")
        except OSError as exc:
            print(f"  could not signal {role} pid {pid}: {exc}", file=sys.stderr)
        path.unlink(missing_ok=True)

    # --- the agents --------------------------------------------------------
    # Detached ones included (they are active): with no server attached,
    # `Runner.stop` ends them through their wrapper, escalating to KILL.
    active = tree.active()
    if only:
        active = _with_descendants(tree, active, only)
    stopped_agents = []
    if active:
        runner = Runner(paths, config)
        for node in active:
            try:
                asyncio.run(runner.stop(node.id))
                stopped_agents.append(node)
            except Exception as exc:                  # never leave the rest unstopped
                print(f"  {node.id} ({node.agent}): {type(exc).__name__}: {exc}",
                      file=sys.stderr)

    # --- nothing an agent wrote may be lost --------------------------------
    # A killed agent never reaches the commit its own run would have made, so
    # its edits sit uncommitted in a worktree nobody will look at again. The
    # branch is what makes the work resumable, so the work has to be on it.
    # HG-R9: each node on its own; one refused checkpoint stops no other.
    saved = 0
    for node in stopped_agents:
        if tree.id_mismatch(node.id):         # HA-R9: no host git on it
            tree.emit(node.id, "host_authority_mismatch", node=node.id,
                      action="stop", fields=["id"])
            continue
        # HA-R10: operands from the record, or under HA-R2a, never tree.json.
        target, refused = _checkpoint_operands(node, authority, tree, "stop")
        if refused:
            _checkpoint_failed(tree, node.id, "stop", refused)
        elif target and _save_interrupted(target, paths.root, authority, tree,
                                          action="stop"):
            saved += 1

    # --- the container, last: the agents were inside it --------------------
    container = ""
    if config.executor == "docker" and not args.keep_containers and not only:
        from .executor.docker import docker_available
        if docker_available():
            container = _docker_executor(paths).stop(remove=False)

    for line in stopped_drivers:
        print(f"stopped      {line}")
    for node in stopped_agents:
        print(f"stopped      {node.id} {node.agent}")
    if saved:
        print(f"committed    work in progress in {saved} worktree(s)")
    if container:
        print(f"container    {container}")
    if not (stopped_drivers or stopped_agents or container):
        print("Nothing was running.")

    waiting = len(tree.open_questions())
    deferred = len(tree.read().get("deferred", []))
    if waiting or deferred:
        print(f"\nkept         {waiting} open question(s), "
              f"{deferred} deferred task(s)")
    print("\nBranches, worktrees and sessions are untouched. `multiagents run` "
          "picks up\nwhere this left off; a stopped agent resumes with "
          "`steer_agent`.")
    return 0


def cmd_docker(args: argparse.Namespace) -> int:
    # An explicit --path is validated before `--all` lists globally: a bad
    # one is a user error whatever else the command was asked to do.
    if args.action == "status" and getattr(args, "all", False):
        if args.path is not None:
            _resolve(args.path)
        return _docker_status_all()
    paths = _resolve(args.path)
    ex = _docker_executor(paths)

    if args.action == "build":
        source = global_config_dir() / "docker"
        if not source.is_dir():
            shutil.copytree(Path(__file__).parent / "defaults" / "docker", source)
        for dockerfile, tag in (("Dockerfile", ex.image), ("Dockerfile.proxy", ex.proxy_image)):
            if dockerfile == "Dockerfile.proxy" and ex.network_mode != "allowlist":
                continue
            print(f"building {tag} from {source / dockerfile} ...")
            result = ex.build_image(source / dockerfile, tag)
            if not result["ok"]:
                print(result.get("output") or result.get("error"), file=sys.stderr)
                return 1
            print(f"  ok  {tag}")
        return 0

    if args.action == "up":
        result = ex.ensure_running()
        if not result.get("ok"):
            print(result.get("error") or result, file=sys.stderr)
            return 1
        print(f"{result['container']} running ({ex.network_mode} networking)")
        return 0

    if args.action == "rm" and not getattr(args, "force", False):
        # SP-R5: removing the container ends every agent in it, and a session
        # it was writing is only resumable if it is stopped first.
        inside = ex.agents_inside()
        if inside:
            print("refusing: removing the container ends the agents still "
                  "inside it:", file=sys.stderr)
            for agent_id, status in inside:
                print(f"  {agent_id}  {status}", file=sys.stderr)
            print("Run `multiagents stop` first, which leaves them resumable, "
                  "or pass --force to end them anyway.", file=sys.stderr)
            return 1

    if args.action in ("down", "rm"):
        print(ex.stop(remove=args.action == "rm"))
        return 0

    if args.action == "login":
        problems = ex.configuration_problems()
        if problems:
            print("; ".join(problems), file=sys.stderr)
            return 1
        provider_name = args.provider
        config = load_config(paths)
        providers = load_providers(config.providers)
        provider = providers.get(provider_name)
        if provider is None:
            print(f"unknown provider {provider_name!r}; known: {sorted(providers)}", file=sys.stderr)
            return 2
        pin = getattr(provider, "container_account", "")
        account = getattr(args, "account", "")
        if pin and account and pin != account:
            print(f"account {account!r} differs from configured pin {pin!r}", file=sys.stderr)
            return 2
        if ex.proxy_provider(provider_name):
            # This login belongs to a host-only vault, never to the writable
            # container placeholder. Use the same browser flow as auth login.
            built = auth_mod.login_command(
                provider_name, provider, ex, global_config_dir(), paths.config,
                extra_env={"MULTIAGENTS_ACCOUNT": account} if account else None)
            if built is None:
                print(f"no auth script for {provider_name!r}", file=sys.stderr)
                return 2
            argv, env = built
            sys.stdout.flush()
            return driver._hand_over(argv, env, scripts.resolve(
                provider_name, provider.auth_owner or provider,
                global_config_dir(), paths.config))
        state = ex.ensure_running()
        if not state.get("ok"):
            print(state.get("error"), file=sys.stderr)
            return 1
        # This provider's profile, not every provider's: with two of them
        # having private homes, the unfiltered version listed both and named
        # the wrong directory as the one about to be written.
        private = ex.private_state(provider_name)
        if not private:
            print(f"{provider_name} has no container_private_home in providers.yaml — "
                  f"it uses the host's credentials directly and needs no separate login.")
            return 0
        print(f"Logging {provider_name} in INSIDE the container.")
        print(f"(`multiagents auth login {provider_name}` is the usual route, and "
              f"for a provider whose\n credentials are plain files it does this "
              f"on the host, with your own browser.)")
        print("Its credentials are stored in a container-private directory:")
        for container_path, host_path in private.items():
            print(f"  {container_path}  ->  {host_path}")
        print("Your host credentials are masked and cannot be touched.\n")
        print("Complete the login it offers, then quit the CLI (ctrl-c or /quit).\n")
        # execvp replaces this process; anything still buffered would be lost.
        sys.stdout.flush()
        # Use the absolute binary path rather than the bare name: the CLIs are
        # bind-mounted at their host paths, which are not on the container
        # image's PATH. Forward the host PATH too, so anything the CLI shells
        # out to during login resolves as it would on the host.
        if ex.config.get("mount_cli_from_host", True):
            resolved = provider.resolve_bin()
            if resolved.path is None:
                print(provider.bin_error(resolved), file=sys.stderr)
                return 2
            binary = ex.native_bin(provider_name, provider, dict(os.environ))
        else:
            binary = provider.bin
        # Use the declared action, including project/plugin overrides. Actions
        # normally run on the host, where BACKING names the host store. Here
        # the same store is visible at PRIVATE_HOME inside the container.
        container_home = str(next(iter(private)))
        built = auth_mod.login_command(
            provider_name, provider, ex, global_config_dir(), paths.config,
            extra_env={"MULTIAGENTS_BIN": binary,
                       "MULTIAGENTS_PRIVATE_HOME": container_home,
                       "MULTIAGENTS_PRIVATE_BACKING": container_home})
        if built is None:
            print(f"no auth script for {provider_name!r}", file=sys.stderr)
            return 2
        argv, env = built
        # Forward the action contract and declared provider environment, rather
        # than every host variable that scripts.build_env makes available.
        keys = {"HOME", "PATH", "TERM"}
        keys.update(key for key in env if key.startswith("MULTIAGENTS_"))
        keys.update(getattr(provider, "credential_env", None) or provider.env)
        keys.discard("MULTIAGENTS_PRIVATE_VAULT")
        keys.discard("MULTIAGENTS_CONTAINER_ACTION")
        command = [
            "docker", "exec", "-it",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--workdir", str(paths.root),
        ]
        env["PATH"] = env.get("PATH") or "/usr/local/bin:/usr/bin:/bin"
        env["TERM"] = env.get("TERM") or "xterm-256color"
        for key in sorted(keys):
            if key in env:
                command += ["--env", f"{key}={env[key]}"]
        # This trusted marker exists only on the exec that enters the container.
        command += ["--env", "MULTIAGENTS_CONTAINER_ACTION=1"]
        os.execvp("docker", [*command, ex.container, *argv])

    if args.action == "shell":
        # HOME and PATH as an AGENT gets them, or the shell is not the thing you
        # came to inspect: the CLIs are bind-mounted at their host paths, which
        # are not on the image's PATH, and `~/.claude` resolves to the image
        # user's empty home rather than to the mounted profile. Both produced
        # confusing answers — "command not found", then "loggedIn: false" from a
        # container that was in fact logged in.
        os.execvp("docker", ["docker", "exec", "-it",
                             "--user", f"{os.getuid()}:{os.getgid()}",
                             "--env", f"HOME={Path.home()}",
                             "--env", f"PATH={os.environ.get('PATH', '')}",
                             "--workdir", str(paths.root), ex.container, "bash"])

    if args.action == "status":
        print(f"image        {ex.image}  {'built' if ex.image_exists(ex.image) else 'MISSING'}")
        if ex.network_mode == "allowlist":
            built = 'built' if ex.image_exists(ex.proxy_image) else 'MISSING'
            print(f"proxy image  {ex.proxy_image}  {built}")
            print(f"proxy        {ex.proxy_container}  {ex.container_state(ex.proxy_container)}")
            print(f"network      {ex.network} (internal)")
        print(f"container    {ex.container}  {ex.container_state(ex.container)}")
        print(f"networking   {ex.network_mode}")
        print(f"resources    cpus={ex.config.get('cpus','-')} "
              f"memory={ex.config.get('memory','-')} pids={ex.config.get('pids_limit','-')}")
        print("mounts:")
        try:
            for path, read_only in ex.mounts():
                print(f"  {'ro' if read_only else 'rw'}  {path}")
        except ValueError as exc:
            print(f"\n{exc}")
            return 1
        problems = ex.preflight()
        print("\n" + ("ready" if not problems else "\n".join(problems)))
        return 1 if problems else 0

    if args.action == "check":
        # Prove the boundary rather than assuming it: one allowed host should
        # resolve, one denied host should not.
        state = ex.ensure_running()
        if not state.get("ok"):
            print(state.get("error"), file=sys.stderr)
            return 1
        allowed = (ex.config.get("egress_allowlist") or ["opencode.ai"])[0]
        for host, expect in ((allowed, "allow"), ("example.com", "deny")):
            probe = subprocess.run(
                ["docker", "exec", ex.container, "curl", "-sS", "-o", "/dev/null",
                 "-m", "20", "-w", "%{http_code}", f"https://{host}/"],
                capture_output=True, text=True, timeout=60,
            )
            code = probe.stdout.strip() or "-"
            reached = probe.returncode == 0 and code not in ("", "-", "000", "403")
            verdict = "OK" if (reached == (expect == "allow")) else "UNEXPECTED"
            print(f"  {expect:5} {host:24} http={code:4} reached={reached}  {verdict}")
        return 0

    return 2


def cmd_mcp_config(args: argparse.Namespace) -> int:
    if getattr(args, "path", None) is not None:
        _explicit_root(args.path)   # unused here, but a bad one is still refused
    path = driver._write_mcp_config()
    print(path)
    print(path.read_text())
    print("Registered automatically for the configured orchestrator by "
          "`multiagents run`. Point another MCP client at this file to give it "
          "the same tools.")
    return 0


# --------------------------------------------------------------------------



def cmd_view(args) -> int:
    from .viewer import view_stream
    paths = _resolve(args.path)
    view_stream(paths, args.id, args.follow)
    return 0

def cmd_tmux(args) -> int:
    from .tmux import cmd_tmux_open, cmd_tmux_attach_cmd, cmd_tmux_close, cmd_tmux_kill
    paths = _resolve(args.path)
    if args.action == "open":
        cmd_tmux_open(paths, args.id)
    elif args.action == "attach-cmd":
        cmd_tmux_attach_cmd(paths, args.id)
    elif args.action == "close":
        cmd_tmux_close(paths, args.id)
    elif args.action == "kill":
        cmd_tmux_kill(paths)
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    paths = _resolve(args.path)
    try:
        wait = float(os.environ.get("MULTIAGENTS_PLAN_COMMIT_LOCK_WAIT", "30"))
        if not math.isfinite(wait) or wait < 0:
            raise ValueError("plan commit lock wait must be a finite nonnegative number of seconds")
        result = plans.commit(paths.root, args.plan_paths, wait)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def cmd_notify(args: argparse.Namespace) -> int:
    """`multiagents notify test|status` (NT-R8, first round).

    `test` sends one message with the project's config and exits 0 when the
    server accepts it, non-zero with the reason otherwise. `status` prints
    the sending state — the pending count — and never publishes. Clearing
    the NT-R4 pause on a successful test belongs to the scheduler sender
    (part 2); there is no pause store yet, so there is nothing to clear.
    """
    from . import notify as notify_mod

    paths = _resolve(args.path)
    action = getattr(args, "notify_command", "")
    try:
        config = load_config(paths)
    except Exception as exc:
        print(f"notify {action}: invalid config: {exc}")
        return 1
    section = (config.project or {}).get("notify")
    if action == "status":
        if section is None:
            print("notify: not configured")
        else:
            print(f"notify: {section.get('ntfy_url')}, topic {section.get('topic')}")
        # Part 1 keeps no outbox: nothing is ever pending yet.
        print("pending: 0")
        return 0
    if action == "test":
        if section is None:
            print("notify test: notify is not configured: no notify: section in project.yaml")
            return 1
        result = notify_mod.send(section, "multiagents notify test",
                                 "This is a test notification from multiagents. "
                                 "If you read this, push notifications are working.")
        if result.get("ok") is True:
            print(f"notify test: accepted by {section.get('ntfy_url')} "
                  f"(topic {section.get('topic')})")
            return 0
        print(f"notify test: {result.get('reason', 'failed')}")
        return 1
    print(f"unknown notify action {action!r}", file=sys.stderr)
    return 2


def cmd_scheduler(args: argparse.Namespace) -> int:
    from . import scheduler
    paths = _resolve(args.path)
    if args.scheduler_command == "start":
        result = scheduler.start(paths.root, foreground=args.foreground, clock_file=getattr(args, "clock_file", None))
    elif args.scheduler_command == "stop":
        result = scheduler.stop(paths.root)
    else:
        result = scheduler.status(paths.root)
    print(json.dumps(result))
    return 1 if result.get("error") == "scheduler_unavailable" and args.scheduler_command == "start" else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="multiagents",
        description="Delegate work to other agent CLIs as supervised, git-isolated subagents.",
    )
    parser.add_argument("--path", help="project directory (default: search upward from cwd)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scheduler", help="manage the host plan scheduler")
    p.set_defaults(func=cmd_scheduler)
    scheduler_sub = p.add_subparsers(dest="scheduler_command", required=True)
    for action in ("start", "status", "stop"):
        command = scheduler_sub.add_parser(action)
        command.set_defaults(func=cmd_scheduler, foreground=False)
        if action == "start":
            command.add_argument("--foreground", action="store_true")
            command.add_argument("--clock-file", help=argparse.SUPPRESS)

    p = sub.add_parser("notify", help="test ntfy push notifications and show their state")
    p.set_defaults(func=cmd_notify)
    notify_sub = p.add_subparsers(dest="notify_command", required=True)
    notify_sub.add_parser("test", help="send one test message with the project's config").set_defaults(func=cmd_notify)
    notify_sub.add_parser("status", help="show the notification sending state").set_defaults(func=cmd_notify)

    p = sub.add_parser("plan", help="commit plans and new specifications")
    p.set_defaults(func=cmd_plan)
    plan_sub = p.add_subparsers(dest="plan_command", required=True)
    pc = plan_sub.add_parser("commit", help="commit only the named paths")
    pc.add_argument("plan_paths", nargs="*", metavar="path")
    pc.set_defaults(func=cmd_plan)

    p = sub.add_parser("init", help="set up a project")
    # Its own dest: sharing `path` with the global `--path` let the absent
    # positional overwrite it with None.
    p.add_argument("init_path", nargs="?", metavar="path",
                   help="project directory (default: cwd)")
    p.add_argument("--force", action="store_true", help="overwrite existing config files")
    p.add_argument("--nested", action="store_true",
                   help="allow a project inside another project's directory tree")
    p.set_defaults(func=cmd_init)

    for name, helptext in (
        ("run", "launch the orchestrator (first run and resume are the same)"),
        ("resume", "alias for `run`"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--no-launch", action="store_true",
                       help="report project state without launching")
        p.add_argument("--fresh", dest="resume", action="store_false", default=True,
                       help="start a new session instead of continuing the last one")
        p.add_argument("--wait", action="store_true",
                       help="if the orchestrator's provider is exhausted, block "
                            "until its quota resets instead of exiting")
        p.add_argument("--force", action="store_true",
                       help="start even though the project's other driver "
                            "(orchestrator or initializer) is running")
        p.add_argument("--no-supervise", dest="supervise", action="store_false",
                       default=True,
                       help="hand the terminal over and exit; nothing carries on "
                            "if the session drops")
        p.add_argument("--team", default="",
                       help="run this team instead of the project's current one "
                            "(project.yaml `team:`); the team decides the "
                            "orchestrator's pipeline and which agents it may spawn")
        p.add_argument("--unattended", nargs="?", type=int, const=50, default=0,
                       metavar="TURNS",
                       help="run headless, turn after turn, without a terminal: "
                            "waits out quota resets, retries a crashed turn with "
                            "backoff, and stops when two turns change nothing "
                            "(default 50 turns)")
        p.set_defaults(func=cmd_resume)

    p = sub.add_parser("init-agent",
                       help="shape the project with the initializer (resumable)")
    p.add_argument("--fresh", dest="resume", action="store_false", default=True,
                   help="start a new session instead of continuing the last one")
    p.add_argument("--force", action="store_true",
                   help="start even though the orchestrator is running")
    p.add_argument("--wait", action="store_true",
                   help="if the provider is exhausted, block until its quota resets")
    p.add_argument("--team", default=None,
                   help="use this team, skipping the interactive choice")
    p.set_defaults(func=cmd_init_agent)

    p = sub.add_parser("build", help="build and start the container environment")
    p.add_argument("--rebuild", action="store_true", help="rebuild images that exist")
    p.add_argument("--no-auth", action="store_true",
                   help="report authentication problems without offering to fix them")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("prompt", help="print the exact prompt an agent would be sent")
    p.add_argument("agent", help="agent name from agents.yaml")
    p.add_argument("--task", default="<the task you would pass to start_agent>",
                   help="the task text to compose in")
    p.add_argument("--team", default="",
                   help="compose as this team instead of the project's current one")
    p.set_defaults(func=cmd_prompt)

    p = sub.add_parser("doctor", help="check CLIs, agents, models, budget and git")
    p.add_argument("--clear", metavar="PROVIDER",
                   help="forget a provider's failure record and cooldown, so "
                        "the circuit breaker starts over")
    p.add_argument("--force", action="store_true",
                   help="with --clear, clear even while the cooldown is still "
                        "running")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("refresh-models", help="regenerate models.yaml from the installed CLIs")
    p.set_defaults(func=cmd_refresh_models)

    p = sub.add_parser("tree", help="print the agent tree")
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser("watch", help="tail the global event log")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("monitor",
                       help="live view of the project: agents, usage, config, history")
    p.add_argument("--tui", action="store_true",
                   help="draw it in this terminal instead of serving a page")
    p.add_argument("--port", type=int, default=8787,
                   help="port for the local page (default 8787)")
    p.add_argument("--no-browser", dest="browser", action="store_false", default=True,
                   help="print the URL instead of opening it")
    p.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                   help="also serve this Host, for a reverse proxy that keeps the "
                        "public one (repeatable; a plain name, never an IP — the "
                        "bind stays on 127.0.0.1)")
    p.add_argument("--persistent-token", action="store_true",
                   help="reuse one token across starts, stored in the user's state "
                        "directory, so a bookmark on another device keeps working")
    p.add_argument("--rotate-token", action="store_true",
                   help="replace that stored token and serve the new one "
                        "(implies --persistent-token; an old bookmark stops working)")
    p.set_defaults(func=cmd_monitor)

    p = sub.add_parser("probe", help="verify a provider's stream parsing rules")
    p.add_argument("provider")
    p.add_argument("--model", required=True)
    p.add_argument("--prompt", default="Reply with exactly: PONG")
    p.add_argument("--timeout", type=int, default=180)
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("clean", help="remove finished agents' branches, worktrees and homes")
    p.add_argument("--branches", action="store_true", help="delete agent branches")
    p.add_argument("--homes", action="store_true", help="delete per-agent HOME directories")
    p.add_argument("--tree", action="store_true", help="prune finished nodes that hold no branch")
    p.add_argument("--force", action="store_true", help="delete even with unmerged commits")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("upgrade-config",
                       help="refresh config copies that were never edited")
    p.add_argument("--dry-run", action="store_true", help="show what would change")
    p.add_argument("--force", action="store_true",
                   help="overwrite edited files too (a .bak is kept)")
    p.add_argument("--layer", choices=["global", "project", "both"], default="both",
                   help="limit to one config layer (default: both)")
    p.set_defaults(func=cmd_upgrade_config)

    p = sub.add_parser("skills", help="list capability fragments agents can be given")
    p.set_defaults(func=cmd_skills)

    p = sub.add_parser("auth", help="check or repair provider authentication")
    p.add_argument("action", nargs="?", default="status", choices=["status", "login"])
    p.add_argument("provider", nargs="?", default="", help="provider to log in")
    p.add_argument("--host", action="store_true",
                   help="sign in to THIS machine's profile rather than the "
                        "container's — the one the orchestrator runs on")
    p.add_argument("--account", default="",
                   help="label a second (or third) login for this provider, so "
                        "work moves to a rested one when the first runs out")
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser("ask", help="answer questions agents are parked on")
    p.add_argument("question_id", nargs="?", default="", help="answer just this one")
    p.add_argument("--agent", default="", help="only this agent's questions")
    p.add_argument("--list", action="store_true", help="list without answering")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("uninstall",
                       help="remove this machine's global config and agent state")
    p.add_argument("--dry-run", action="store_true", help="show what would go")
    p.add_argument("--force", action="store_true",
                   help="remove even when a worktree holds uncommitted work")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("status", help="what the orchestrator is doing")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("supervise", help="(internal) watch a launched agent")
    p.add_argument("--role", default="orchestrator")
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--interval", type=float, default=20.0)
    p.add_argument("--max-seconds", dest="max_seconds", type=float, default=0.0)
    p.set_defaults(func=cmd_supervise)

    p = sub.add_parser("stop", help="stop everything for this project, resumably")
    p.add_argument("agent_id", nargs="?", default="",
                   help="stop only this agent, and nothing else")
    p.add_argument("--all", action="store_true",
                   help="everything, including agents left running with no "
                        "session attached (the default when no id is given)")
    p.add_argument("--keep-containers", action="store_true",
                   help="leave the container running")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("usage", help="tokens and cost per provider/model")
    p.add_argument("--agents", action="store_true", help="name the agents behind each row")
    p.add_argument("--checks", action="store_true",
                   help="what checked what, and how each check ended")
    p.add_argument("--mcp", action="store_true",
                   help="what this project costs the orchestrator's own subscription")
    p.add_argument("--hours", type=float, default=24.0,
                   help="window for --mcp, in hours (default 24)")
    p.set_defaults(func=cmd_usage)

    p = sub.add_parser("refresh-quota",
                       help="re-read a provider's quota past every cache, and lift any "
                            "cooldown/pause it disproves")
    p.add_argument("providers", nargs="*", help="providers to re-read (default: all)")
    p.set_defaults(func=cmd_refresh_quota)

    p = sub.add_parser("tickets", help="review bugs agents filed against multiagents")
    p.add_argument("action", nargs="?", default="list",
                   choices=["list", "show", "submit", "resolve", "discard"])
    p.add_argument("ticket_id", nargs="?", default="")
    p.add_argument("--all", action="store_true", help="include resolved tickets")
    p.add_argument("--note", default="", help="with `resolve`: why, or what fixed it")
    p.add_argument("--declined", action="store_true",
                   help="with `resolve`: it was not a bug, rather than fixed")
    p.set_defaults(func=cmd_tickets)

    p = sub.add_parser("docker", help="manage the project's agent container")
    p.add_argument("action",
                   choices=["build", "up", "down", "rm", "status", "shell", "check", "login"])
    p.add_argument("provider", nargs="?", default="agy",
                   help="provider to act on; only used by `login` (default: agy)")
    p.add_argument("--all", action="store_true",
                   help="with `status`: every project's containers on this machine")
    p.add_argument("--force", action="store_true",
                   help="with `rm`: remove it even with agents still inside")
    p.add_argument("--account", default="", help="vault account label for login")
    p.set_defaults(func=cmd_docker)

    p = sub.add_parser("catalog", help="compare the local model catalog against the live one")
    p.add_argument("--provider", default="opencode-go")
    p.add_argument("--update", action="store_true", help="record the live catalog as the baseline")
    p.set_defaults(func=cmd_catalog)

    p = sub.add_parser("git-guard", help="refuse sensitive material before it leaves through a push")
    p.set_defaults(func=cmd_git_guard)
    gg_sub = p.add_subparsers(dest="git_guard_command", required=True)
    s = gg_sub.add_parser("scan", help="scan a range for sensitive material")
    s.add_argument("range", nargs="?", default="",
                   help="commit range (default: <remote>/<base>..<base>)")
    s.set_defaults(func=cmd_git_guard)
    s = gg_sub.add_parser("install", help="install the pre-push hook")
    s.add_argument("--force", action="store_true",
                   help="keep an existing hook as pre-push.local and chain to it")
    s.set_defaults(func=cmd_git_guard)
    s = gg_sub.add_parser("uninstall", help="remove the pre-push hook this tool wrote")
    s.set_defaults(func=cmd_git_guard)
    s = gg_sub.add_parser("check-push", help=argparse.SUPPRESS)
    s.add_argument("remote_name", nargs="?", default="")
    s.add_argument("remote_url", nargs="?", default="")
    s.set_defaults(func=cmd_git_guard)

    
    p = sub.add_parser("view", help="view an agent stream")
    p.add_argument("id")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--follow", action="store_true", default=None)
    group.add_argument("--no-follow", dest="follow", action="store_false")
    p.set_defaults(func=cmd_view)

    p = sub.add_parser("tmux", help="tmux viewer integration")
    p.add_argument("action", choices=["open", "attach-cmd", "close", "kill"])
    p.add_argument("id", nargs="?")
    p.set_defaults(func=cmd_tmux)

    p = sub.add_parser("mcp-config", help="write and print the MCP registration")
    p.set_defaults(func=cmd_mcp_config)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
