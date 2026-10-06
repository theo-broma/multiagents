"""Throwaway worlds for the git-guard tests (GG-R1..R6).

A world is a working repository, a local bare "remote", a private patterns file
and a project.yaml, all under one `tmp_path`. Nothing here touches the project
repository or the network.

Secret-shaped values are assembled at call time from fragments, so that no
literal of the shape sits in a test file: the guard scans this repository's own
pushes, and a test file that trips it would block every push.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

SRC = str(Path(__file__).resolve().parents[2] / "src")

IDENT = ("tester", "t@example.invalid")        # allowed by the spec, by design


# ---------------------------------------------------------------- shapes ----
def email(local: str = "someone", domain: str = "corp-mail.test") -> str:
    return local + "@" + domain


def tailnet_ip(a: str = "64", b: str = "1", c: str = "2") -> str:
    return ".".join(["100", a, b, c])


def tailnet_host(name: str = "box") -> str:
    return name + ".tail" + "1a2b3c" + ".ts" + ".net"


def private_key_block() -> str:
    head = "-----BEGIN " + "RSA PRIVATE" + " KEY-----"
    tail = "-----END " + "RSA PRIVATE" + " KEY-----"
    return head + "\nMIIBplaceholderplaceholderplaceholder\n" + tail


def private_key_header() -> str:
    return "-----BEGIN " + "OPENSSH PRIVATE" + " KEY-----"


def tokens() -> dict[str, str]:
    """name -> a placeholder string shaped like that credential."""
    return {
        "ghp": "gh" + "p_" + "Ab1Cd2Ef3Gh4" * 3,
        "gho": "gh" + "o_" + "Zy9Xw8Vu7Ts6" * 3,
        "github_pat": "github" + "_pat_" + "Qq1Ww2Ee3Rr4Tt5Yy6Uu7I",
        "anthropic": "sk" + "-ant-" + "api03-" + "Mm1Nn2Bb3Vv4Cc5Xx6",
        "openai": "sk" + "-" + "Ab1Cd2Ef3Gh4Ij5Kl6Mn7",
        "slack_b": "xo" + "xb-" + "1234567890-" + "abcdefghij",
        "slack_p": "xo" + "xp-" + "1234567890-" + "abcdefghij",
        "slack_a": "xo" + "xa-" + "1234567890-" + "abcdefghij",
        "slack_r": "xo" + "xr-" + "1234567890-" + "abcdefghij",
        "aws": "AK" + "IA" + "ABCDEFGH23456789",
    }


# ------------------------------------------------------------------ git -----
def base_env(base: Path) -> dict:
    home = base / "home"
    home.mkdir(exist_ok=True)
    empty = base / "empty.gitconfig"
    empty.write_text("")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MULTIAGENTS_", "GIT_", "XDG_"))}
    env.update({
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": str(empty),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "PYTHONPATH": SRC + (os.pathsep + os.environ["PYTHONPATH"]
                             if os.environ.get("PYTHONPATH") else ""),
        "PYTHONUNBUFFERED": "1",
        "MULTIAGENTS_STATE_DIR": str(base / "state"),
        "MULTIAGENTS_CONFIG_DIR": str(base / "config"),
    })
    return env


def git(repo: Path, *args: str, env: dict | None = None, check: bool = True,
        ident: tuple[str, str] = IDENT, input: bytes | None = None
        ) -> subprocess.CompletedProcess:
    cmd = ["git", "-c", f"user.name={ident[0]}", "-c", f"user.email={ident[1]}",
           "-C", str(repo), *args]
    p = subprocess.run(cmd, capture_output=True, env=env or os.environ.copy(),
                       input=input, timeout=60)
    if check and p.returncode:
        raise AssertionError(f"git {args}: {p.stderr.decode(errors='replace')}")
    return p


def out(p: subprocess.CompletedProcess) -> str:
    return p.stdout.decode(errors="replace").strip()


class World:
    def __init__(self, base: Path, *, coauthor: bool | None = None,
                 allowed_emails=None, allow=None, patterns: str | None = None,
                 patterns_mode: int = 0o600, patterns_file: bool = True,
                 repo_email: str = IDENT[1]):
        self.base = base
        self.env = base_env(base)
        self.root = base / "work"
        self.bare = base / "remote.git"
        self.patterns = base / "private" / "sensitive-patterns"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.bare)],
                       check=True, env=self.env)
        git(self.root, "init", "-q", "-b", "main", env=self.env)
        git(self.root, "config", "user.name", IDENT[0], env=self.env)
        git(self.root, "config", "user.email", repo_email, env=self.env)
        git(self.root, "remote", "add", "origin", str(self.bare), env=self.env)
        (self.root / ".git" / "info" / "exclude").write_text(".multiagents/\n")
        if patterns is not None:
            self.patterns.parent.mkdir()
            self.patterns.write_text(patterns)
            os.chmod(self.patterns, patterns_mode)
        guard: dict = {}
        if patterns_file:
            guard["patterns_file"] = str(self.patterns)
        if allowed_emails is not None:
            guard["allowed_emails"] = allowed_emails
        if allow is not None:
            guard["allow"] = allow
        section: dict = {"remote": "origin", "base_branch": "main", "guard": guard}
        if coauthor is not None:
            section["coauthor_orchestrator"] = coauthor
        self.write_config({"git": section})
        self.write("seed.txt", "seed\n")
        self.commit("seed")
        git(self.root, "push", "-q", "-u", "origin", "main", env=self.env)

    # -- project ---------------------------------------------------------
    def write_config(self, data: dict) -> None:
        cfg = self.root / ".multiagents" / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "project.yaml").write_text(yaml.safe_dump(data))

    # -- repository ------------------------------------------------------
    def write(self, name: str, text: str | bytes) -> Path:
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode() if isinstance(text, str) else text)
        return p

    def commit(self, message: str = "work", *, author: str | None = None,
               committer_email: str | None = None, add: bool = True) -> str:
        if add:
            git(self.root, "add", "-A", env=self.env)
        env = dict(self.env)
        if committer_email:
            env["GIT_COMMITTER_EMAIL"] = committer_email
        args = ["commit", "-q", "--allow-empty", "-m", message]
        if author:
            args += ["--author", author]
        git(self.root, *args, env=env)
        return self.head()

    def commit_file(self, name: str, text: str | bytes, message: str = "work",
                    **kw) -> str:
        self.write(name, text)
        return self.commit(message, **kw)

    def head(self, rev: str = "HEAD") -> str:
        return out(git(self.root, "rev-parse", rev, env=self.env))

    def branch(self, name: str, start: str = "main") -> None:
        git(self.root, "checkout", "-q", "-b", name, start, env=self.env)

    def checkout(self, name: str) -> None:
        git(self.root, "checkout", "-q", name, env=self.env)

    # -- remote ----------------------------------------------------------
    def remote_refs(self) -> dict[str, str]:
        p = git(self.bare, "for-each-ref", "--format=%(refname) %(objectname)",
                env=self.env)
        return dict(line.split(" ", 1) for line in out(p).splitlines())

    # -- the guard -------------------------------------------------------
    def guard(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "multiagents.cli", "git-guard", *args],
            cwd=str(self.root), env=self.env, capture_output=True, text=True,
            timeout=timeout)

    def scan(self, *args: str) -> subprocess.CompletedProcess:
        return self.guard("scan", *args)

    def push(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return git(self.root, "push", *args, env=env or self.env, check=False)

    def hook_path(self) -> Path:
        return self.root / ".git" / "hooks" / "pre-push"

    def all_output_files(self) -> list[Path]:
        """Files the guard could have logged to: the state root and the
        project's own .multiagents directory."""
        found = []
        for top in (self.base / "state", self.base / "config",
                    self.root / ".multiagents"):
            if top.is_dir():
                found += [p for p in top.rglob("*") if p.is_file()]
        return found


def said(p: subprocess.CompletedProcess) -> str:
    stdout, stderr = p.stdout, p.stderr
    if isinstance(stdout, bytes):
        stdout = stdout.decode(errors="replace")
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    return (stdout or "") + (stderr or "")


def finding_lines(p, sha: str) -> list[str]:
    """Output lines naming the commit: one per finding (GG-R3)."""
    return [ln for ln in said(p).splitlines() if sha[:7] in ln]


def mask(match: str) -> str:
    return match[:2] + "…"
