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

from . import gitops
from .budget import read_all
from .config import load as load_config
from .config import seed_global, seed_project
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

    # Git is required: agents work on branches.
    if not gitops.is_repo(root):
        print("\ngit          not a repository")
        print("             agents work on their own branches, so this project needs one:")
        print(f"               git -C {root} init && git -C {root} commit --allow-empty -m init")
    elif not gitops.has_commits(root):
        print("\ngit          repository has no commits")
        print("             a worktree cannot be branched from nothing:")
        print(f"               git -C {root} commit --allow-empty -m init")
    else:
        print(f"git          {gitops.current_branch(root)} @ {gitops.head_sha(root)[:12]}")

    gitignore = root / ".gitignore"
    existing = gitignore.read_text() if gitignore.is_file() else ""
    if GITIGNORE_LINE not in existing:
        with gitignore.open("a") as handle:
            handle.write(("" if existing.endswith("\n") or not existing else "\n")
                         + f"\n# multiagents runtime state\n{GITIGNORE_LINE}\n")
        print(f"gitignore    added {GITIGNORE_LINE}")

    providers = load_providers(load_config(paths).providers)
    result = refresh_models(providers, paths.config / "models.yaml")
    for name, count in result["counts"].items():
        print(f"models       {name}: {count}")
    for name, problem in result["problems"].items():
        print(f"models       {name}: {problem}")

    mcp_path = _write_mcp_config()
    print(f"mcp config   {mcp_path}")
    print("\nNext:")
    print(f"  edit  {paths.config}/agents.yaml")
    print(f"  add   alias mao='claude --model sonnet --mcp-config {mcp_path}'")
    print("  then  multiagents doctor")
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

    if args.no_launch:
        return 0

    mcp_path = _mcp_config_path()
    if not mcp_path.is_file():
        _write_mcp_config()
    argv = ["claude", "--continue", "--model", args.model, "--mcp-config", str(mcp_path)]
    print(f"\n$ {' '.join(argv)}\n")
    try:
        os.execvp(argv[0], argv)
    except OSError as exc:
        print(f"could not launch claude: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    paths = _resolve(args.path) if find_project_root() else None
    config = load_config(paths)
    providers = load_providers(config.providers)
    problems = 0

    print("providers")
    for name, provider in sorted(providers.items()):
        path = provider.available()
        if path:
            print(f"  {name:12} {path}")
        else:
            print(f"  {name:12} NOT FOUND ({provider.bin} is not on PATH)")
            problems += 1

    print("\nagents")
    for name, spec in sorted(config.agents.items()):
        provider = providers.get(spec.provider)
        mark = " " if provider and provider.available() else "!"
        print(f"  {mark} {name:12} {spec.provider}/{spec.model}")
        instructions = config.instructions_for(spec)
        if spec.instructions and not instructions.strip():
            print(f"    missing instructions file: {spec.instructions}")
            problems += 1

    for warning in validate_agent_models(config):
        print(f"    {warning}")

    print("\nbudget")
    for name, entry in read_all().items():
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

    if args.homes:
        for home in paths.homes.glob("ag-*"):
            node = data["nodes"].get(home.name)
            if node and node.get("status") in {"running", "pending"}:
                continue
            shutil.rmtree(home, ignore_errors=True)
            removed += 1

    print(f"\n{removed} item(s) removed")
    return 0


def cmd_mcp_config(args: argparse.Namespace) -> int:
    path = _write_mcp_config()
    print(path)
    print(path.read_text())
    print(f"alias mao='claude --model sonnet --mcp-config {path}'")
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

    p = sub.add_parser("resume", help="reconcile state and reopen the orchestrator session")
    p.add_argument("--model", default="sonnet", help="orchestrator model (default: sonnet)")
    p.add_argument("--no-launch", action="store_true", help="report state without starting claude")
    p.set_defaults(func=cmd_resume)

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
    p.add_argument("--force", action="store_true", help="delete even with unmerged commits")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("mcp-config", help="write and print the MCP registration")
    p.set_defaults(func=cmd_mcp_config)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
