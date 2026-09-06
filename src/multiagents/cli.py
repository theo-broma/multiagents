"""``multiagents`` — setup and housekeeping outside the MCP layer.

Some things are not agent work: first-time setup, refreshing the model list
after a subscription change, checking that the CLIs are installed and
authenticated, cleaning up worktrees after a crash. Doing those through an MCP
tool would mean starting a Claude session for housekeeping — and would leave you
with no way to diagnose the system when the MCP layer itself is what is broken.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import auth as auth_mod
from . import bugs
from . import catalog as catalog_mod
from . import scripts
from . import gitops
from .budget import read_all
from .config import load as load_config
from .config import seed_global, seed_project, sync_layer
from .models import refresh_models, validate_agent_models
from .paths import ProjectPaths, find_project_root, global_config_dir, state_root
from .providers import load_providers
from .tree import Tree

GITIGNORE_LINE = ".multiagents/"


def _resolve(explicit: str | None = None) -> ProjectPaths:
    if explicit:
        return ProjectPaths(Path(explicit).expanduser().resolve())
    root = find_project_root()
    if root is None:
        print("No .multiagents/ found here or above. Run `multiagents init` first.", file=sys.stderr)
        raise SystemExit(2)
    return ProjectPaths(root)


def _mcp_config_path() -> Path:
    return global_config_dir() / "mcp.json"


def _write_mcp_config() -> Path:
    """Write the MCP registration the orchestrator alias points at."""
    path = _mcp_config_path()
    project = Path(__file__).resolve().parents[2]
    config = {
        "mcpServers": {
            "multiagents": {
                "type": "stdio",
                "command": "uv",
                "args": ["run", "--project", str(project), "python", "-m", "multiagents.server"],
            }
        }
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n")
    return path


# --------------------------------------------------------------------------


def _launched_spec(config, role: str):
    """The roster entry launched by a given command, or None.

    Both the orchestrator and the initializer are launched rather than spawned;
    the role says which door they come through.
    """
    for spec in config.agents.values():
        if spec.launch and spec.role == role:
            return spec
    # An entry marked launch: true with no role still serves as the orchestrator,
    # so a config written before roles existed keeps working.
    if role == "orchestrator":
        return next((s for s in config.agents.values() if s.launch and not s.role), None)
    return None


def _launch_context(paths, config, spec) -> dict[str, str]:
    """Everything a provider's launch script needs, as environment.

    The orchestrator's brief is a normal agent instruction file, so it resolves
    through the same three config layers as every other agent's — write it out
    fresh each run so edits take effect without any copying step.
    """
    state = paths.data / "launch"
    state.mkdir(parents=True, exist_ok=True)
    prompt_file = state / "orchestrator-prompt.md"
    prompt_file.write_text(config.instructions_for(spec) or "")

    mcp_path = _write_mcp_config()
    server = json.loads(mcp_path.read_text())["mcpServers"]["multiagents"]
    return {
        "MULTIAGENTS_MODEL": spec.model,
        "MULTIAGENTS_PROMPT_FILE": str(prompt_file),
        "MULTIAGENTS_MCP_CONFIG": str(mcp_path),
        "MULTIAGENTS_MCP_COMMAND": server["command"],
        # \x1f so an argument containing spaces survives the round trip.
        "MULTIAGENTS_MCP_ARGS": "\x1f".join(server["args"]),
        "MULTIAGENTS_LAUNCH_STATE": str(state),
        "MULTIAGENTS_PROJECT": str(paths.root),
    }


def _launch_agent(paths, config, role: str, resume: bool) -> int:
    """Launch a roster entry as an interactive MCP client. Does not return."""
    spec = _launched_spec(config, role)
    if spec is None:
        print(f"No agent in agents.yaml is marked `launch: true, role: {role}`.",
              file=sys.stderr)
        return 2
    providers = load_providers(config.providers)
    provider = providers.get(spec.provider)
    if provider is None or not provider.available():
        print(f"{role} provider {spec.provider!r} is unavailable", file=sys.stderr)
        return 2

    executor = _executor_for(paths, config, providers)(spec.provider)
    context = _launch_context(paths, config, spec)
    # Resuming is only possible if this role has been launched here before.
    # Passing --continue on a first run makes the CLI error out with no prior
    # conversation, which would make `run` fail exactly once per project.
    marker = paths.data / "launch" / f"{role}.launched"
    first_time = not marker.is_file()
    context["MULTIAGENTS_RESUME"] = "0" if (first_time or not resume) else "1"
    context["MULTIAGENTS_ROLE"] = role
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(time.time()))

    code, out, err = scripts.run_action(
        spec.provider, provider, executor, "prepare",
        global_config_dir(), paths.config, timeout=60, extra_env=context,
    )
    if code not in (0, scripts.UNIMPLEMENTED):
        print(f"prepare failed for {spec.provider}: {(err or out).strip()[:300]}",
              file=sys.stderr)
        return 1
    if out.strip():
        print(f"prepare      {out.strip()}")

    built = scripts.exec_action(spec.provider, provider, executor, "launch",
                                global_config_dir(), paths.config, extra_env=context)
    if built is None:
        print(f"no script for provider {spec.provider!r}", file=sys.stderr)
        return 2
    argv, env = built
    print(f"{role:12} {spec.provider}/{spec.model}"
          f"{'' if context['MULTIAGENTS_RESUME'] == '0' else ' (resuming)'}\n")
    sys.stdout.flush()
    os.execvpe(argv[0], argv, env)
    return 0


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
    print()
    _report_catalog(config)
    print()
    return _launch_agent(paths, config, "initializer", resume=args.resume)


def _ensure_authenticated(paths, config, providers, interactive: bool = True) -> int:
    """Check every enabled provider and offer to fix what is broken.

    Returns the number still unauthenticated. Login scripts are run as ordinary
    subprocesses with stdio inherited rather than exec'd, so several can be
    repaired in one pass — exec would replace this process at the first one.
    """
    executor_for = _executor_for(paths, config, providers)
    project_config = paths.config if paths else None
    enabled = {n: p for n, p in providers.items() if p.enabled}

    broken = []
    for name, provider in sorted(enabled.items()):
        state = auth_mod.check(name, provider, executor_for(name),
                               global_config_dir(), project_config)
        mark = "ok " if state.ok else "!! "
        print(f"  {mark}{name:10} {state.detail[:70]}")
        if not state.ok:
            broken.append((name, provider))

    if not broken:
        return 0
    if not interactive:
        for name, _ in broken:
            print(f"  fix: multiagents auth login {name}")
        return len(broken)

    still_broken = 0
    for name, provider in broken:
        print(f"\n{name} needs authenticating.")
        try:
            answer = input(f"  run `auth login {name}` now? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            answer = "n"
        if answer in ("n", "no"):
            print(f"  skipped — agents on {name} will fail until you run "
                  f"`multiagents auth login {name}`")
            still_broken += 1
            continue

        built = auth_mod.login_command(name, provider, executor_for(name),
                                       global_config_dir(), project_config)
        if built is None:
            print(f"  no script for {name!r}")
            still_broken += 1
            continue
        argv, env = built
        sys.stdout.flush()
        # stdio is inherited, so the script gets the terminal it needs.
        subprocess.run(argv, env=env)

        recheck = auth_mod.check(name, provider, executor_for(name),
                                 global_config_dir(), project_config)
        print(f"  {name}: {recheck.status}")
        if not recheck.ok:
            still_broken += 1
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
    entries = gitops.uncommitted_entries(root)
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
            entries = gitops.uncommitted_entries(root)

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


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.path or ".").expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    paths = ProjectPaths(root)
    paths.ensure()

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

    if _gitignore_add(root, [GITIGNORE_LINE], "# multiagents runtime state"):
        print(f"gitignore    added {GITIGNORE_LINE}")

    # After the two writes above, so a first commit made here picks them up.
    _offer_git(root)

    providers = load_providers(load_config(paths).providers)
    result = refresh_models(providers, paths.config / "models.yaml")
    for name, count in result["counts"].items():
        print(f"models       {name}: {count}")
    for name, problem in result["problems"].items():
        print(f"models       {name}: {problem}")

    _report_catalog(load_config(paths))

    mcp_path = _write_mcp_config()
    print(f"mcp config   {mcp_path}")
    print("\nNext:")
    print("  1. multiagents init-agent    shape the project (resumable)")
    print("  2. edit .multiagents/config/agents.yaml if you want a different roster")
    print("  3. multiagents build         container environment, if executor is docker")
    print("  4. multiagents run           launch the orchestrator")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Reconcile state after a crash or restart, then reopen the session."""
    paths = _resolve(args.path)
    tree = Tree(paths.tree_file, paths.events_file)

    # A local agent dies with the server that spawned it (own process group), so
    # anything still marked running after a restart is gone, not working.
    reclaimed = 0
    for node in tree.active():
        alive = False
        if node.pid:
            try:
                os.kill(node.pid, 0)
                alive = True
            except (ProcessLookupError, PermissionError):
                alive = False
        if not alive:
            tree.set_status(node.id, "orphaned", "process gone; server restarted")
            reclaimed += 1

    print(tree.render())
    if reclaimed:
        print(f"\nreclaimed    {reclaimed} agent(s) whose process no longer exists")

    data = tree.read()
    stale = [
        n for n in data["nodes"].values()
        if n.get("branch") and n.get("status") in {"orphaned", "stuck", "done", "failed"}
    ]
    if stale:
        print(f"\n{len(stale)} branch(es) still held by unfinished agents:")
        for node in stale:
            commits = gitops.commits_on(
                paths.root, node["branch"], gitops.current_branch(paths.root)
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

    config = load_config(paths)

    if args.no_launch:
        return 0

    # The catalog baseline is the initializer's concern; re-checking it on every
    # orchestrator launch spends a network round trip on ground that rarely
    # moves. The orchestrator calls check_model_catalog when something suggests
    # it has.
    return _launch_agent(paths, config, "orchestrator", resume=args.resume)


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
        print(f"  {mark} {name:12} {spec.provider}/{spec.model:34} {tag}")
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


def cmd_doctor(args: argparse.Namespace) -> int:
    paths = _resolve(args.path) if find_project_root() else None
    config = load_config(paths)
    providers = load_providers(config.providers)
    problems = 0

    print("providers")
    for name, provider in sorted(providers.items()):
        path = provider.available()
        state = "" if provider.enabled else "  [disabled in providers.yaml]"
        if path:
            print(f"  {name:12} {path}{state}")
        elif not provider.enabled:
            # Disabled and absent is not a problem worth flagging — the user
            # said they do not want it.
            print(f"  {name:12} not installed{state}")
        else:
            print(f"  {name:12} NOT FOUND ({provider.bin} is not on PATH)")
            problems += 1

    print("\nagents")
    problems += _report_agents(config, providers)

    print("\nauth")
    providers_map = load_providers(config.providers)
    executor_for = _executor_for(paths, config, providers_map)
    for name, state in sorted(auth_mod.check_all(
            providers_map, executor_for, global_config_dir(),
            paths.config if paths else None).items()):
        mark = " " if state.ok else "!"
        print(f"  {mark} {name:10} {state.status:18} {state.detail[:60]}")
        if not state.ok:
            problems += 1
            if state.fix:
                print(f"      fix: {state.fix}")

    print("\nbudget")
    _budget = read_all(providers_map, executor_for, global_config_dir(),
                       paths.config if paths else None)
    for name, entry in _budget.items():
        data = entry.to_dict()
        if data.get("known"):
            print(f"  {name:12} {data['used_percent']}% used, resets {data.get('resets_at','?')}")
        else:
            print(f"  {name:12} headroom unknown — {data.get('note','')}")

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
        leaking = [k for k in config.env_passthrough if k not in blocked]
        print(f"  env        passthrough={leaking or 'none'} blocked={len(blocked)} vars")

    print(f"\n{'ok' if not problems else str(problems) + ' problem(s)'}")
    return 1 if problems else 0


def cmd_refresh_models(args: argparse.Namespace) -> int:
    paths = _resolve(args.path) if find_project_root() else None
    config = load_config(paths)
    target = (paths.config if paths else global_config_dir()) / "models.yaml"
    result = refresh_models(load_providers(config.providers), target)
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
                stamp = time.strftime("%H:%M:%S", time.localtime(event.get("t", 0)))
                extra = " ".join(
                    f"{k}={v}" for k, v in event.items()
                    if k not in {"t", "agent", "kind"} and v not in (None, "", [])
                )
                print(f"{stamp} {event.get('agent','-'):10} {event.get('kind',''):10} {extra}")
        except KeyboardInterrupt:
            return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """Run a trivial task through a provider and report how its stream parsed.

    The tool for adding an integration: it shows which rules matched and, more
    usefully, which lines fell through to `raw` and still need a rule.
    """
    paths = _resolve(args.path) if find_project_root() else None
    config = load_config(paths)
    providers = load_providers(config.providers)
    provider = providers.get(args.provider)
    if provider is None:
        print(f"unknown provider {args.provider!r}; known: {sorted(providers)}", file=sys.stderr)
        return 2
    if not provider.available():
        print(f"{provider.bin} is not on PATH", file=sys.stderr)
        return 2

    argv = provider.build_command(
        prompt=args.prompt, model=args.model, workdir=str(Path.cwd()), permission="readonly",
    )
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


def cmd_clean(args: argparse.Namespace) -> int:
    paths = _resolve(args.path)
    tree = Tree(paths.tree_file, paths.events_file)
    data = tree.read()
    removed = 0

    gitops.prune_worktrees(paths.root)

    if args.branches:
        base = gitops.current_branch(paths.root)
        for node in data["nodes"].values():
            branch = node.get("branch")
            if not branch or node.get("status") in {"running", "pending"}:
                continue
            commits = gitops.commits_on(paths.root, branch, base)
            if commits and not args.force:
                print(f"keep   {branch} ({commits} unmerged commit(s); --force to delete)")
                continue
            worktree = node.get("worktree")
            if worktree and Path(worktree).is_dir():
                gitops.remove_worktree(paths.root, Path(worktree), force=True)
            gitops.delete_branch(paths.root, branch, force=True)
            print(f"delete {branch}")
            removed += 1

    if args.tree:
        from multiagents.tree import TERMINAL
        with tree.transaction() as state:
            drop = [
                nid for nid, n in state["nodes"].items()
                if n.get("status") in TERMINAL and not n.get("branch")
                and not n.get("conversation")
            ]
            for nid in drop:
                state["nodes"].pop(nid, None)
            for n in state["nodes"].values():
                n["children"] = [c for c in n.get("children", []) if c not in drop]
        print(f"pruned {len(drop)} finished node(s) holding no branch")
        removed += len(drop)

    if args.homes:
        for home in paths.homes.glob("ag-*"):
            node = data["nodes"].get(home.name)
            if node and node.get("status") in {"running", "pending"}:
                continue
            shutil.rmtree(home, ignore_errors=True)
            removed += 1

    print(f"\n{removed} item(s) removed")
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    paths = _resolve(args.path) if find_project_root() else None
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


def _executor_for(paths, config, providers):
    """An executor per provider, honouring any per-agent pin for that provider.

    Auth differs by where the CLI runs: opencode keeps credentials on the host
    even under docker, while agy needs a login inside the container.
    """
    from .executor import get_executor
    from .paths import global_config_dir as _gcd

    def build(provider_name: str):
        kind = config.executor
        for spec in config.agents.values():
            if spec.provider == provider_name and spec.executor:
                kind = spec.executor
                break
        return get_executor(
            kind, config.project.get("executor", {}).get("docker", {}),
            paths=paths, providers=providers, config_dir=_gcd(),
        )
    return build


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


def cmd_upgrade_config(args: argparse.Namespace) -> int:
    """Refresh config copies that were never edited.

    Pinned copies override the shipped defaults for every key, so without this
    an install silently never receives improvements to files it is not using.
    Edited files are left alone and reported.
    """
    from .paths import shipped_defaults_dir
    paths = _resolve(args.path) if find_project_root() else None
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


def cmd_auth(args: argparse.Namespace) -> int:
    paths = _resolve(args.path) if find_project_root() else None
    config = load_config(paths)
    providers = load_providers(config.providers)
    project_config = paths.config if paths else None
    executor_for = _executor_for(paths, config, providers)

    if args.action == "login":
        name = args.provider
        provider = providers.get(name)
        if provider is None:
            print(f"unknown provider {name!r}; known: {sorted(providers)}", file=sys.stderr)
            return 2
        built = auth_mod.login_command(
            name, provider, executor_for(name), global_config_dir(), project_config)
        if built is None:
            print(f"no auth script for {name!r}. Add one to "
                  f"{global_config_dir()}/auth/{name}.sh — see auth/README.md.",
                  file=sys.stderr)
            return 2
        argv, env = built
        sys.stdout.flush()
        os.execvpe(argv[0], argv, env)

    states = auth_mod.check_all(providers, executor_for, global_config_dir(), project_config)
    broken = 0
    for name, state in sorted(states.items()):
        where = _auth_scope(providers[name], executor_for(name))
        mark = "ok " if state.ok else ("!! " if state.status == "not_authenticated" else "?  ")
        print(f"  {mark}{name:10} [{where:9}] {state.detail}")
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
        print(f"{ticket['id']}  [{ticket['severity']}]  {ticket['status']}")
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


def cmd_docker(args: argparse.Namespace) -> int:
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
        print(result if not result.get("ok") else
              f"{result['container']} running ({ex.network_mode} networking)")
        return 0 if result.get("ok") else 1

    if args.action in ("down", "rm"):
        print(ex.stop(remove=args.action == "rm"))
        return 0

    if args.action == "login":
        provider_name = args.provider
        config = load_config(paths)
        providers = load_providers(config.providers)
        provider = providers.get(provider_name)
        if provider is None:
            print(f"unknown provider {provider_name!r}; known: {sorted(providers)}", file=sys.stderr)
            return 2
        state = ex.ensure_running()
        if not state.get("ok"):
            print(state.get("error"), file=sys.stderr)
            return 1
        private = ex.private_state()
        if not private:
            print(f"{provider_name} has no container_private_home in providers.yaml — "
                  f"it uses the host's credentials directly and needs no separate login.")
            return 0
        print(f"Logging {provider_name} in INSIDE the container.")
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
        binary = provider.available() or provider.bin
        os.execvp("docker", [
            "docker", "exec", "-it",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--workdir", str(paths.root),
            "--env", f"HOME={Path.home()}",
            "--env", f"PATH={os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}",
            "--env", "TERM=xterm-256color",
            ex.container, binary,
        ])

    if args.action == "shell":
        os.execvp("docker", ["docker", "exec", "-it",
                             "--user", f"{os.getuid()}:{os.getgid()}",
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
        for path, read_only in ex.mounts():
            print(f"  {'ro' if read_only else 'rw'}  {path}")
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
    path = _write_mcp_config()
    print(path)
    print(path.read_text())
    print("Registered automatically for the configured orchestrator by "
          "`multiagents run`. Point another MCP client at this file to give it "
          "the same tools.")
    return 0


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="multiagents",
        description="Delegate work to other agent CLIs as supervised, git-isolated subagents.",
    )
    parser.add_argument("--path", help="project directory (default: search upward from cwd)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="set up a project")
    p.add_argument("path", nargs="?", help="project directory (default: cwd)")
    p.add_argument("--force", action="store_true", help="overwrite existing config files")
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
        p.set_defaults(func=cmd_resume)

    p = sub.add_parser("init-agent",
                       help="shape the project with the initializer (resumable)")
    p.add_argument("--fresh", dest="resume", action="store_false", default=True,
                   help="start a new session instead of continuing the last one")
    p.set_defaults(func=cmd_init_agent)

    p = sub.add_parser("build", help="build and start the container environment")
    p.add_argument("--rebuild", action="store_true", help="rebuild images that exist")
    p.add_argument("--no-auth", action="store_true",
                   help="report authentication problems without offering to fix them")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("doctor", help="check CLIs, agents, models, budget and git")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("refresh-models", help="regenerate models.yaml from the installed CLIs")
    p.set_defaults(func=cmd_refresh_models)

    p = sub.add_parser("tree", help="print the agent tree")
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser("watch", help="tail the global event log")
    p.set_defaults(func=cmd_watch)

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

    p = sub.add_parser("auth", help="check or repair provider authentication")
    p.add_argument("action", nargs="?", default="status", choices=["status", "login"])
    p.add_argument("provider", nargs="?", default="", help="provider to log in")
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser("ask", help="answer questions agents are parked on")
    p.add_argument("question_id", nargs="?", default="", help="answer just this one")
    p.add_argument("--agent", default="", help="only this agent's questions")
    p.add_argument("--list", action="store_true", help="list without answering")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("tickets", help="review bugs agents filed against multiagents")
    p.add_argument("action", nargs="?", default="list",
                   choices=["list", "show", "submit", "discard"])
    p.add_argument("ticket_id", nargs="?", default="")
    p.add_argument("--all", action="store_true", help="include resolved tickets")
    p.set_defaults(func=cmd_tickets)

    p = sub.add_parser("docker", help="manage the project's agent container")
    p.add_argument("action",
                   choices=["build", "up", "down", "rm", "status", "shell", "check", "login"])
    p.add_argument("provider", nargs="?", default="agy",
                   help="provider to act on; only used by `login` (default: agy)")
    p.set_defaults(func=cmd_docker)

    p = sub.add_parser("catalog", help="compare the local model catalog against the live one")
    p.add_argument("--provider", default="opencode-go")
    p.add_argument("--update", action="store_true", help="record the live catalog as the baseline")
    p.set_defaults(func=cmd_catalog)

    p = sub.add_parser("mcp-config", help="write and print the MCP registration")
    p.set_defaults(func=cmd_mcp_config)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
