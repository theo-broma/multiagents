"""Slice 4C black-box helpers: credentials for isolated attempts.

Contract: context/specs/phase7-part2-contract.md, "Slice 4C" (D-S4C.1..6,
IC-C32..IC-C36). Built on the 4B helpers (`ic_s4b`, `ic_s4b2`): the scheduler on
the real isolated Docker backend over the fake `docker` CLI. Fake-CLI passes are
never evidence of containment; the real-Docker cases are in test_s4c_docker.py.

ASSUMED SEAMS (NEED_INFO(s4c-seams); the contract names none of them, so each is
the smallest invention and lives in exactly one place here so a rename is a
one-line change):

* S1 the provider field is `isolated_credentials`, read as
  `Provider.isolated_credentials`; the default is `unsupported`.
* S2 the mode an attempt ran under is recorded in `launch_evidence` under a key
  named `isolated_credentials` (searched at any depth: `recorded_modes`).
* S3 the host vault of a provider is the existing
  `$MULTIAGENTS_STATE_DIR/container-state/shared/<provider>/vault`. A Claude
  vault holds `.credentials.json` (account `default`) and
  `accounts/<label>/.credentials.json` (`{"claudeAiOauth": {"accessToken",
  "refreshToken", "expiresAt"}}`); the pinned account is the provider's
  `container_account`. A projection vault holds `auth.json`, opencode's store
  (`{name: {"type": "oauth", "access", "refresh", "expires"} | {"type": "api",
  "key"}}`); its single account is `default`.
* S4 a projection provider's projected directory is its
  `container_private_home` entry (`.local/share/fakecode`), and the projected
  file is `<that dir>/auth.json` in the guest HOME.
* S5 the host renewal worker: `executor.isolated.credential_renew_interval`
  (seconds) sets how often it looks; an entry whose expiry has passed is renewed
  by the host-side refresher loaded from `MULTIAGENTS_TEST_CREDENTIAL_REFRESH`
  (`ic_s4c_refresh`); the refreshed entry is written back to the vault.
* S6 the redactor's secret list for an attempt is the `redaction_secrets` member
  of the guest invocation input (as `redaction_values` delivers it today).
* S7 the host adapter of the Claude gateway is built by
  `multiagents.scheduler.gateways.claude_adapter(vault, *, pins, upstream,
  refresh)`; `upstream(method, url, headers, body)` is the transport to the fixed
  upstream and `refresh(refresh_token) -> {"accessToken", "refreshToken",
  "expiresAt"}` the OAuth call. The adapter has the K4B2.5 `open(...)` interface.

All secret values are fictional.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import time
from pathlib import Path

import fake_docker as fdk
import ic_s4b as nb
import ic_s4b2 as ab
import ic_s4c_refresh as rf
import ic_support as ic

PROVIDER = "acme"
ENV_REFRESH = "MULTIAGENTS_TEST_CREDENTIAL_REFRESH"
RENEW_INTERVAL = 0.2           # S5, seconds
WAIT = nb.WAIT
RENEW_WAIT = 8                 # a bounded wait for one renewal by the host worker
FAR = rf.FAR_FUTURE_MS
PAST = 1                       # an access token that expired in 1970

# fictional values, one per account so a confusion between accounts shows
ACCESS = {"default": "FICTION-ACCESS-default-3f9a", "work": "FICTION-ACCESS-work-71c2",
          "spare": "FICTION-ACCESS-spare-b05d"}
REFRESH = {"default": "FICTION-REFRESH-default-8e41", "work": "FICTION-REFRESH-work-2ad7",
           "spare": "FICTION-REFRESH-spare-c936"}
OAUTH_ACCESS = "FICTION-OC-ACCESS-5d2e"
OAUTH_REFRESH = "FICTION-OC-REFRESH-a77b"
API_KEY = "FICTION-OC-APIKEY-91f0c3"
PROJECTED_DIR = ".local/share/fakecode"
PROJECTED_FILE = PROJECTED_DIR + "/auth.json"


# ------------------------------------------------------------------ the vaults

def write_claude_vault(vault: Path, *, expired=(), labels=("default", "work", "spare")) -> Path:
    for label in labels:
        path = vault / ".credentials.json" if label == "default" else vault / "accounts" / label / ".credentials.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": ACCESS[label], "refreshToken": REFRESH[label],
            "expiresAt": PAST if label in expired else FAR}}))
    return vault


def write_projection_vault(vault: Path, *, expired=False, entries=None) -> Path:
    vault.mkdir(parents=True, exist_ok=True)
    data = entries or {
        "fakecode": {"type": "oauth", "access": OAUTH_ACCESS, "refresh": OAUTH_REFRESH,
                     "expires": PAST if expired else FAR},
        "fakekey": {"type": "api", "key": API_KEY},
    }
    (vault / "auth.json").write_text(json.dumps(data))
    return vault / "auth.json"


def read_json(path: Path):
    return json.loads(Path(path).read_text())


def find_key(obj, key):
    """Every value stored under `key`, at any depth."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            yield from find_key(v, key)
    elif isinstance(obj, list):
        for item in obj:
            yield from find_key(item, key)


def keys_anywhere(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from keys_anywhere(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from keys_anywhere(item)


# --------------------------------------------------- watch every `docker start`

START_SHIM = r'''#!{python}
import json, os, subprocess, sys
LOG = {log!r}
SHIM_DIR = os.path.dirname(os.path.abspath(__file__))
rest = [p for p in os.environ.get("PATH", "").split(os.pathsep) if os.path.abspath(p) != SHIM_DIR]
env = dict(os.environ, PATH=os.pathsep.join(rest))
argv = sys.argv[1:]
words = [a for a in argv if not a.startswith("-")]
if words[:1] == ["start"] or words[:2] == ["container", "start"]:
    ref = words[-1]
    out = subprocess.run(["docker", "inspect", ref], capture_output=True, text=True, env=env).stdout
    try:
        host = json.loads(out)[0]["HostConfig"]
    except (ValueError, KeyError, IndexError):
        host = {{}}
    mounts = []
    for bind in host.get("Binds") or []:
        parts = bind.split(":")
        mounts.append((parts[0], parts[1] if len(parts) > 1 else ""))
    for m in host.get("Mounts") or []:
        mounts.append((m.get("Source", ""), m.get("Destination", "")))
    seen = {{}}
    for source, dest in mounts:
        files = {{}}
        if source.startswith("/") and os.path.isdir(source):
            for base, _, names in os.walk(source):
                for n in names:
                    p = os.path.join(base, n)
                    try:
                        files[os.path.relpath(p, source)] = open(p, errors="replace").read(200000)
                    except OSError:
                        pass
        seen[dest] = {{"source": source, "dir": os.path.isdir(source), "files": files}}
    with open(LOG, "a") as handle:
        handle.write(json.dumps({{"ref": ref, "mounts": seen}}) + "\n")
os.execvpe("docker", ["docker"] + argv, env)
'''


def install_start_recorder(tmp_path: Path, monkeypatch) -> Path:
    """A `docker` in front of the fake that records, at every `start`, the files
    under each mount's host source: what the guest would find when its CLI starts."""
    shim_dir = tmp_path / "ic-start-shim"
    shim_dir.mkdir()
    log = tmp_path / "ic-starts.jsonl"
    log.touch()
    shim = shim_dir / "docker"
    shim.write_text(START_SHIM.format(python=sys.executable, log=str(log)))
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(shim_dir) + os.pathsep + os.environ.get("PATH", ""))
    return log


# ------------------------------------------------------------ the fake-CLI world

class CredSched(ab.AuthSched):
    """AuthSched whose provider needs credentials and declares a credential mode.

    mode: "gateway" (network + gateway_auth anthropic + a Claude vault),
          "projection" (no network; an opencode-like vault and projected dir),
          "unsupported" or None (field absent): needs credentials, nothing else.
    """

    def __init__(self, tmp_path, monkeypatch, *, mode="gateway", refresh_delay=0.0, expired=False,
                 renew=True, account="work", vault=True, **kw):
        self.tmp_root = tmp_path
        self.ma_state = tmp_path / "ma-state"
        monkeypatch.setenv("MULTIAGENTS_STATE_DIR", str(self.ma_state))
        self.refresh_log = tmp_path / "refresh.jsonl"
        monkeypatch.setenv(rf.LOG_ENV, str(self.refresh_log))
        monkeypatch.setenv(rf.DELAY_ENV, str(refresh_delay))
        monkeypatch.setenv(ENV_REFRESH, rf.SPEC)
        self.mode = mode
        super().__init__(tmp_path, monkeypatch, gateway_auth="anthropic" if mode == "gateway" else None, **kw)
        self.starts_log = install_start_recorder(tmp_path, monkeypatch)    # in front of the fake docker
        network = mode == "gateway"
        self.provider_declares(credentials=True, network=network, rpc=False)
        if vault:
            if mode == "projection":
                write_projection_vault(self.vault, expired=expired)
            elif mode == "gateway":
                write_claude_vault(self.vault, expired=("work",) if expired else ())
        self.declare_mode(mode, account=account)
        if renew:
            self.configure(network=None, allowlist=nb.ALLOW, credential_renew_interval=RENEW_INTERVAL)

    # -- declarations (S1, S3, S4) ----------------------------------------------
    @property
    def vault(self) -> Path:
        return self.ma_state / "container-state" / "shared" / PROVIDER / "vault"

    def declare_mode(self, mode, account="work") -> None:
        entry = self.fake.entry
        if mode is None:
            entry.pop("isolated_credentials", None)
        else:
            entry["isolated_credentials"] = mode
        if mode == "gateway":
            entry["container_private_home"] = [".claude"]
            entry["container_account"] = account
        elif mode == "projection":
            entry["container_private_home"] = [PROJECTED_DIR]
        self.p.write()
        self._reset_server()

    # -- what the scheduler recorded and what the guest was given ----------------
    def recorded_modes(self, attempt_id: str) -> list:
        return list(find_key(self.evidence(attempt_id), "isolated_credentials"))

    def started(self) -> list[dict]:
        return [json.loads(line) for line in self.starts_log.read_text().splitlines() if line.strip()]

    def home_of(self, container: dict) -> str:
        return self.invocation(container)["env"]["HOME"]

    def mounts(self, container: dict) -> list[dict]:
        return fdk.mounts_of(container)

    def credential_mounts(self, container: dict) -> list[dict]:
        return [m for m in self.mounts(container) if m["destination"] != "/multiagents-input"]

    def guest_files(self, container: dict, *, with_input=False) -> dict[str, str]:
        """{host path: text} of every file under every mount's host source."""
        out = {}
        for m in self.mounts(container):
            if m["destination"] == "/multiagents-input" and not with_input:
                continue
            src = Path(m["source"]) if m["source"] else None
            if not src or not src.is_dir():
                if src and src.is_file():
                    out[str(src)] = src.read_text(errors="replace")
                continue
            for p in src.rglob("*"):
                if p.is_file() and p.stat().st_size < 1_000_000:
                    out[str(p)] = p.read_text(errors="replace")
        return out

    def projected_path(self, container: dict) -> Path | None:
        """The host file behind the guest's `$HOME/<projected file>` (S4), found
        the way the guest would: by the mount that covers that guest path."""
        guest = self.home_of(container).rstrip("/") + "/" + PROJECTED_FILE
        best = None
        for m in self.credential_mounts(container):
            dest = m["destination"].rstrip("/")
            if m["source"] and (guest == dest or guest.startswith(dest + "/")):
                if best is None or len(dest) > len(best["destination"].rstrip("/")):
                    best = m
        if best is None:
            return None
        rel = guest[len(best["destination"].rstrip("/")):].lstrip("/")
        return Path(best["source"]) / rel if rel else Path(best["source"])

    def refresh_events(self) -> list[dict]:
        if not self.refresh_log.exists():
            return []
        return [json.loads(line) for line in self.refresh_log.read_text().splitlines() if line.strip()]

    def refreshes(self) -> int:
        return sum(1 for e in self.refresh_events() if e["event"] == "enter")

    def max_overlap(self) -> int:
        depth = best = 0
        for e in sorted(self.refresh_events(), key=lambda e: e["t"]):
            depth += 1 if e["event"] == "enter" else -1
            best = max(best, depth)
        return best

    def ended(self, node: str, timeout: float = WAIT) -> dict:
        return self.wait(lambda: (v := self.get(node)) and v["state"] in ("held", "failed", "done", "cancelled") and v,
                         timeout, f"{node} to end")


def vault_entry(path: Path, name: str = "fakecode") -> dict:
    return read_json(path)[name]


def claude_entry(vault: Path, label: str) -> dict:
    path = vault / ".credentials.json" if label == "default" else vault / "accounts" / label / ".credentials.json"
    return read_json(path)["claudeAiOauth"]


def leaks_of(texts: dict[str, str], secrets) -> dict[str, list[str]]:
    """{where: [secrets found]} for each literal secret in each text."""
    found = {}
    for where, text in texts.items():
        hit = [s for s in secrets if s and s in text]
        if hit:
            found[where] = hit
    return found


def secrets_of_claude() -> list[str]:
    return list(ACCESS.values()) + list(REFRESH.values())


def tree_texts(root: Path) -> dict[str, str]:
    out = {}
    for p in Path(root).rglob("*"):
        if p.is_file() and not p.is_symlink() and p.stat().st_size < 2_000_000:
            try:
                out[str(p)] = p.read_text(errors="replace")
            except OSError:
                pass
    return out


def salted(salt: str, value: str) -> str:
    return hashlib.sha256((salt + value).encode()).hexdigest()


def claude_adapter():
    """S7: the factory of the Claude gateway's host adapter, or a failure that says what is missing."""
    import importlib
    try:
        module = importlib.import_module("multiagents.scheduler.gateways")
    except ImportError as exc:
        raise AssertionError(f"IC-C33: multiagents.scheduler.gateways is missing ({exc})") from None
    build = getattr(module, "claude_adapter", None)
    assert build is not None, "IC-C33 (seam S7): multiagents.scheduler.gateways.claude_adapter is missing"
    return build


def host_secret_texts(s: "CredSched") -> dict[str, str]:
    """Every file under the test's world except the host vault itself and the
    recorders that deliberately keep what they saw."""
    skip = (str(s.vault), str(s.starts_log), str(s.refresh_log))
    return {k: v for k, v in tree_texts(s.tmp_root).items() if not k.startswith(skip) and "fake-docker" not in k}


def snapshot_file(record: dict, home: str, relative: str = PROJECTED_FILE):
    """From a start-recorder record: (mount info, text) of the file the guest would
    find at `$HOME/<relative>` when its CLI starts, or (None, None)."""
    guest = home.rstrip("/") + "/" + relative
    best = None
    for dest, info in record["mounts"].items():
        d = dest.rstrip("/")
        if guest == d or guest.startswith(d + "/"):
            if best is None or len(d) > len(best[0].rstrip("/")):
                best = (dest, info)
    if best is None:
        return None, None
    rel = guest[len(best[0].rstrip("/")):].lstrip("/")
    return best[1], best[1]["files"].get(rel)
