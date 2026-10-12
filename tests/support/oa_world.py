"""A fake machine for the OA tests: host profiles, a fake vault, a fake `claude`.

Everything lives under one tmp_path. Identities are fictional
(*@example.invalid), tokens are placeholders, nothing reaches the real home,
the real profiles or the network. `multiagents run` is a real subprocess with
HOME pointed at the fake home and the fake `claude` first on PATH.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SITE = Path(__file__).resolve().parent / "oa_site"
RUN_TIMEOUT = 90

FAKE_CLAUDE = r'''#!__PY__
import hashlib, json, os, re, sys, time
FAKE = "__FAKE__"
args = sys.argv[1:]
home = os.environ.get("HOME", "")
cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
root = cfg or os.path.join(home, ".claude")
state = os.path.join(cfg, ".claude.json") if cfg else os.path.join(home, ".claude.json")
creds = os.path.join(root, ".credentials.json")

def vtime():
    try:
        return float(open(os.path.join(FAKE, "vtime")).read())
    except Exception:
        return 0.0

def log(rec):
    with open(os.path.join(FAKE, "launches.jsonl"), "a") as out:
        out.write(json.dumps(rec) + "\n")

with open(os.path.join(FAKE, "invocations.log"), "a") as inv:
    inv.write(json.dumps({"cfg": cfg, "argv": args[:3]}) + "\n")
if args[:2] == ["auth", "status"]:
    try:
        exp = json.load(open(creds))["claudeAiOauth"]["expiresAt"]
        ok = exp / 1000 > time.time()
    except Exception:
        ok = False
    print(json.dumps({"loggedIn": ok, "email": "fake@example.invalid"}))
    sys.exit(0)
if "--model" not in args:
    print("fake claude 0.0")
    sys.exit(0)

def opt(name):
    return args[args.index(name) + 1] if name in args else None

sid = opt("--resume") or opt("--session-id")
mode = "resume" if "--resume" in args else "new"
prompt = opt("-p") or ""
slug = re.sub(r"[^A-Za-z0-9]", "-", os.getcwd())
tdir = os.path.join(root, "projects", slug)
tfile = os.path.join(tdir, sid + ".jsonl")
existed = os.path.isfile(tfile)
sha = hashlib.sha256(open(tfile, "rb").read()).hexdigest() if existed else None
try:
    count = int(open(os.path.join(FAKE, "count")).read())
except Exception:
    count = 0
open(os.path.join(FAKE, "count"), "w").write(str(count + 1))
rec = {"n": count, "cfg": cfg, "sid": sid, "mode": mode, "prompt": prompt,
       "existed": existed, "sha": sha, "v": vtime(), "argv": args,
       "text_at_launch": open(tfile).read() if existed else None}
log(rec)
plan = json.load(open(os.path.join(FAKE, "plan.json")))
steps = plan.get("launches", [])
step = steps[count] if count < len(steps) else plan.get("default", {"do": "ok"})
if mode == "resume" and not existed:
    sys.stderr.write("No conversation found with session ID: %s\n" % sid)
    sys.exit(1)
def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
os.makedirs(tdir, exist_ok=True)
do = step.get("do", "ok")
lines = []
if do != "silent":
    lines.append({"type": "user", "timestamp": now(), "sessionId": sid,
                  "message": {"role": "user", "content": prompt or "go"}})
if do == "limit":
    lines.append({"type": "assistant", "timestamp": now(), "sessionId": sid,
                  "message": {"role": "assistant", "content": [
                      {"type": "text", "text": "You've hit your usage limit. Resets 5pm"}]}})
elif do in ("ok", "fail"):
    lines.append({"type": "assistant", "timestamp": now(), "sessionId": sid,
                  "message": {"role": "assistant", "stop_reason": "end_turn",
                              "content": [{"type": "text", "text": step.get("say", "turn done")}]}})
if lines:
    with open(tfile, "a") as out:
        for l in lines:
            out.write(json.dumps(l) + "\n")
if step.get("other_limit"):
    time.sleep(0.05)
    with open(os.path.join(tdir, "other-session.jsonl"), "w") as out:
        out.write(json.dumps({"type": "assistant", "timestamp": now(), "message": {
            "role": "assistant", "content": [{"type": "text",
            "text": "You've hit your usage limit. Resets 5pm"}]}}) + "\n")
if step.get("exhaust"):
    ex = step["exhaust"]
    import datetime
    reset = (datetime.datetime.now(datetime.timezone.utc)
             + datetime.timedelta(seconds=ex["reset_in"])).isoformat()
    try:
        data = json.load(open(state))
    except Exception:
        data = {}
    data["cachedUsageUtilization"] = {"fetchedAtMs": time.time() * 1000, "utilization": {
        "limits": [{"kind": "session", "percent": ex["used"], "resets_at": reset}]}}
    json.dump(data, open(state, "w"))
sys.exit(1 if do == "fail" else 0)
'''

# ST1-R3 / D11: an empty successful inspect means an existing container with
# unobservable mounts, which must not be started. OA's fake machine has no
# existing containers; say so explicitly and let the normal ensure create one.
FAKE_DOCKER = """#!/bin/sh
if [ "$1" = inspect ]; then
    echo 'No such container' >&2
    exit 1
fi
exit 0
"""


def slug(path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def usage_payload(used: float, reset_in: float, age: float = 0.0) -> dict:
    reset = (datetime.datetime.now(datetime.timezone.utc)
             + datetime.timedelta(seconds=reset_in)).isoformat()
    return {"fetchedAtMs": (time.time() - age) * 1000,
            "utilization": {"limits": [{"kind": "session", "percent": used,
                                        "resets_at": reset}]}}


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Result:
    def __init__(self, proc, world):
        self.code = proc.returncode
        self.out = proc.stdout
        self.err = proc.stderr
        self.world = world

    @property
    def text(self):
        return self.out + "\n" + self.err


class World:
    def __init__(self, tmp_path: Path, name: str = "proj", home: Path | None = None):
        self.base = Path(tmp_path)
        self.home = home or self.base / "home"
        self.fake = self.base / f"fake-{name}"
        self.proj = self.base / name
        self.home.mkdir(exist_ok=True)
        (self.fake / "bin").mkdir(parents=True)
        self.proj.mkdir()
        py = sys.executable
        script = FAKE_CLAUDE.replace("__PY__", py).replace("__FAKE__", str(self.fake))
        (self.fake / "bin" / "claude").write_text(script)
        (self.fake / "bin" / "docker").write_text(FAKE_DOCKER)
        for b in (self.fake / "bin").iterdir():
            b.chmod(0o755)
        self.plan({})
        (self.fake / "sleep_script.json").write_text("[]")
        self.extra_env: dict[str, str] = {}
        git = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        env = {**os.environ, **git}
        subprocess.run(["git", "init", "-q", str(self.proj)], check=True, env=env)
        subprocess.run(["git", "-C", str(self.proj), "commit", "-q", "--allow-empty",
                        "-m", "init"], check=True, env=env)
        self.cli("init")
        self.sid_dir = self.proj / ".multiagents" / "launch"

    # ----------------------------------------------------------- environment
    def env(self) -> dict:
        path = f"{self.fake / 'bin'}:/usr/local/bin:/usr/bin:/bin"
        env = {"HOME": str(self.home), "PATH": path, "LANG": "C.UTF-8",
               "PYTHONPATH": f"{SITE}:{REPO / 'src'}", "OA_FAKE_DIR": str(self.fake),
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        env.update(self.extra_env)
        return env

    def cli(self, *argv, timeout=RUN_TIMEOUT, tty=False) -> Result:
        code = "import sys; from multiagents.cli import main; sys.exit(main(sys.argv[1:]))"
        if argv and argv[0] == "init":
            # init normally downloads the public OpenCode model catalogue.
            # OA exercises Claude profiles, so its fake machine supplies that
            # unrelated service too. Keep the run/resume paths unpatched.
            code = ("import pytest\nfrom multiagents import catalog\n"
                    "with pytest.MonkeyPatch.context() as patch:\n"
                    "    patch.setattr(catalog, 'fetch_remote', "
                    "lambda *a, **k: {'models': {}})\n    " + code)
        proc = subprocess.run(
            [sys.executable, "-c", code, "--path", str(self.proj), *argv],
            env=self.env(), capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, cwd=str(self.proj))
        return Result(proc, self)

    def run(self, turns=4, *extra) -> Result:
        return self.cli("run", "--unattended", str(turns), *extra)

    # --------------------------------------------------------------- profiles
    def profile(self, name: str) -> Path:
        if name == "claude":
            return self.home
        return self.home / ".multiagents" / "profiles" / name

    def state_file(self, name: str) -> Path:
        return self.profile(name) / ".claude.json"

    def creds_file(self, name: str) -> Path:
        return (self.home / ".claude" if name == "claude" else self.profile(name)) / ".credentials.json"

    def projects_dir(self, name: str) -> Path:
        base = self.home / ".claude" if name == "claude" else self.profile(name)
        return base / "projects" / slug(self.proj)

    def make_profile(self, name: str, identity: str | None = "alice@example.invalid",
                     used: float | None = 40, reset_in: float = 3 * 3600,
                     age: float = 0.0, creds: str = "valid"):
        """used=None: no usage reading at all (unknown)."""
        state: dict = {}
        if identity:
            state["oauthAccount"] = {"emailAddress": identity}
        if used is not None:
            state["cachedUsageUtilization"] = usage_payload(used, reset_in, age)
        self.state_file(name).parent.mkdir(parents=True, exist_ok=True)
        self.state_file(name).write_text(json.dumps(state))
        if creds != "absent":
            expires = time.time() + (7200 if creds == "valid" else -7200)
            self.creds_file(name).parent.mkdir(parents=True, exist_ok=True)
            self.creds_file(name).write_text(json.dumps({"claudeAiOauth": {
                "accessToken": "fake-token-" + name + "-" + "x" * 24,
                "refreshToken": "fake-refresh-" + name + "-" + "y" * 24,
                "expiresAt": int(expires * 1000)}}))
        (self.profile(name) / "settings.json").write_text('{"fake": "' + name + '"}') \
            if name != "claude" else None

    def add_provider(self, name: str, account: str | None = None, extra: str = ""):
        block = (f"\n  {name}:\n    extends: claude\n"
                 + (f"    container_account: {account}\n    container_private_home: [\".claude-{name}\"]\n" if account else "")
                 + "    env:\n      CLAUDE_CONFIG_DIR: \"~/.multiagents/profiles/"
                 + f"{name}\"\n" + extra)
        path = self.proj / ".multiagents" / "config" / "providers.yaml"
        path.write_text(path.read_text() + block)

    # ------------------------------------------------------------------ config
    @property
    def agents_yaml(self) -> Path:
        return self.proj / ".multiagents" / "config" / "agents.yaml"

    @property
    def project_yaml(self) -> Path:
        return self.proj / ".multiagents" / "config" / "project.yaml"

    def root_config(self, provider: str | None = None, switch_to=None, role="orchestrator"):
        text = self.agents_yaml.read_text()
        head = f"  {role}:\n    provider: claude\n"
        assert head in text
        new = f"  {role}:\n    provider: {provider or 'claude'}\n"
        if switch_to is not None:
            new += "    switch_to: [" + ", ".join(switch_to) + "]\n"
        self.agents_yaml.write_text(text.replace(head, new, 1))

    def project_set(self, text: str, executor: str | None = None):
        p = self.project_yaml.read_text()
        if executor:
            p = p.replace("  kind: local", f"  kind: {executor}", 1)
        self.project_yaml.write_text(p + "\n" + text)

    def reserve(self, fraction: float):
        text = self.project_yaml.read_text()
        self.project_yaml.write_text(re.sub(
            r"\nquota_handover:\n  reserve_fraction: [0-9.]+\n", "\n", text))
        self.project_set(f"quota_handover:\n  reserve_fraction: {fraction}\n")

    # ------------------------------------------------------------------- vault
    def vault_dir(self, account: str | None = None) -> Path:
        base = self.home / ".multiagents" / "container-state" / "shared" / "claude" / "vault"
        return base / "accounts" / account if account else base

    def make_vault(self, account: str | None, used: float, reset_in: float = 1200):
        d = self.vault_dir(account)
        d.mkdir(parents=True, exist_ok=True)
        (d / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "fake-vault-token-" + "z" * 24,
            "expiresAt": int((time.time() + 7200) * 1000)}}))
        (d / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "vault-" + (account or "default") + "@example.invalid"},
            "cachedUsageUtilization": usage_payload(used, reset_in)}))

    # -------------------------------------------------------------- fake CLI io
    def plan(self, plan: dict):
        (self.fake / "plan.json").write_text(json.dumps(plan))

    def sleep_script(self, steps: list):
        (self.fake / "sleep_script.json").write_text(json.dumps(steps))

    def free_at(self, after: float, name: str, used: float = 10, reset_in: float = 18000):
        return {"after": after, "profiles": [{"state": str(self.state_file(name)),
                                              "used": used, "reset_in": reset_in}]}

    def launches(self) -> list[dict]:
        path = self.fake / "launches.jsonl"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]

    def cfgs(self) -> list[str]:
        """Which profile each launch ran on, by name ('claude' = default)."""
        out = []
        for l in self.launches():
            cfg = l["cfg"]
            out.append("claude" if not cfg else Path(cfg).name)
        return out

    def invocations(self) -> list[dict]:
        p = self.fake / "invocations.log"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def sleeps(self) -> list[float]:
        p = self.fake / "sleeps.log"
        return [float(l.split()[0]) for l in p.read_text().splitlines()] if p.exists() else []

    def net_attempts(self) -> str:
        p = self.fake / "net-attempts"
        return p.read_text() if p.exists() else ""

    def session_id(self) -> str:
        return (self.proj / ".multiagents" / "launch" / "orchestrator.session").read_text().strip()

    def seed_session(self, name: str, sid: str, text: str | None = None):
        d = self.projects_dir(name)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{sid}.jsonl").write_text(text if text is not None else json.dumps(
            {"type": "user", "message": {"role": "user", "content": "earlier work"}}) + "\n")

    # ----------------------------------------------------------------- hashing
    def snapshot(self, root: Path) -> dict[str, str]:
        out = {}
        for p in sorted(root.rglob("*")):
            if p.is_file() and not p.is_symlink():
                out[str(p.relative_to(root))] = sha(p)
        return out


def budget_status(world: World, timeout: int = 60) -> dict:
    """`budget_status` as the root's MCP tool returns it, in the world's fake machine."""
    code = ("import json; from multiagents import server; r = server.budget_status(); "
            "print(json.dumps(json.loads(r) if isinstance(r, str) else r))")
    env = {**world.env(), "MULTIAGENTS_PROJECT": str(world.proj)}
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                          text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                          cwd=str(world.proj))
    assert proc.returncode == 0, f"budget_status failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])
