"""Slice 4B.1 on a REAL Docker daemon (SG_TEST_DOCKER=1): containment of the
per-attempt network, the egress gateway's real answers, producer-side
redaction and network orphans.

Fake-CLI passes are never evidence of containment (4B work split); these are
the cases that are. With the switch set a missing prerequisite FAILS
(`dk.require_docker`), it never skips. Without it the module is skipped.

NOT RUN by the author: no Docker daemon exists in the authoring sandbox. The
module is verified to collect only. Not written here (they need a controlled DNS
and upstream reachable from inside Docker, which the contract leaves to the
orchestrator's host): positive forwarding to an approved host, closing a live
CONNECT tunnel on cancel, gateway replacement under traffic, restart/adoption
with a live tunnel.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_docker as dk  # noqa: E402
import ic_probe  # noqa: E402
import ic_support as ic  # noqa: E402
import ic_workload as wl  # noqa: E402

pytestmark = pytest.mark.skipif(not dk.OPT_IN, reason="real Docker is opt-in: SG_TEST_DOCKER=1")

EXTRA = r'''
def cred_facts(url):
    # the probe never prints the credential: only non-secret facts and a salted
    # hash the host recomputes from the password it holds itself
    import hashlib
    m = re.fullmatch(r"http://([^:@/]+):([^@/]*)@([^/]+)", url or "")
    if not m: return {"parsed": False, "length": len(url or "")}
    return {"parsed": True, "user": m.group(1), "hostport": m.group(3), "pw_len": len(m.group(2)),
            "pw_salted_sha256": hashlib.sha256((ins.get("salt", "") + m.group(2)).encode()).hexdigest()}
if probe == "egress":
    proxy = os.environ.get("HTTP_PROXY", "")
    def can(addr, port, udp=False):
        try:
            c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM); c.settimeout(2)
            c.connect((addr, port))
            if udp:
                c.send(b"x"); c.recv(10)
            c.close(); return True
        except OSError as e: return repr(e)
    def via(raw):
        try:
            c = socket.create_connection(("ma-egress", 8888), timeout=3); c.settimeout(3)
            c.sendall(raw.encode()); data = b""
            while b"\r\n" not in data:
                chunk = c.recv(4096)
                if not chunk: break
                data += chunk
            c.close(); return data.split(b"\r\n")[0].decode("latin-1")
        except OSError as e: return repr(e)
    url = ins.get("credential") or proxy
    m = re.match(r"http://([^:]+):([^@]+)@", url)
    basic = base64.b64encode((m.group(1) + ":" + m.group(2)).encode()).decode() if m else ""
    def req(target, extra="", method="GET"):
        return "%s %s HTTP/1.1\r\nHost: x\r\nProxy-Authorization: Basic %s\r\n%s\r\n" % (method, target, basic, extra)
    try: dns = socket.gethostbyname(ins.get("dns_name", "example.org"))
    except OSError as e: dns = repr(e)
    say(dict(common(), cred=cred_facts(proxy), no_proxy=os.environ.get("NO_PROXY"), all_proxy=os.environ.get("ALL_PROXY"),
             gateway_reachable=can("ma-egress", 8888),
             management={str(p): can("ma-egress", p) for p in ins.get("ports", [])},
             direct={a: can(a.split(":")[0], int(a.split(":")[1])) for a in ins.get("direct", [])},
             udp_dns={a: can(a, 53, udp=True) for a in ins.get("dns_servers", [])},
             dns_external=dns,
             evil=via(req("http://evil.test/")),
             connect_ssh=via(req("example.org:22", method="CONNECT")),
             no_auth=via("GET http://evil.test/ HTTP/1.1\r\nHost: x\r\n\r\n"),
             forged=via(req("http://evil.test/", "X-Forwarded-For: 10.0.0.2\r\nForwarded: for=10.0.0.2\r\n")),
             ))
    if ins.get("echo"):
        print(proxy, file=sys.stderr, flush=True)       # deliberate: the redactor must scrub stderr too
    if ins.get("echo"):
        sys.stdout.write("pw="); sys.stdout.flush()
        pw = m.group(2) if m else ""
        sys.stdout.write(pw[:30]); sys.stdout.flush(); time.sleep(0.3); sys.stdout.write(pw[30:] + "\n"); sys.stdout.flush()
    if ins.get("hold"):
        import signal
        signal.pause()
elif probe == "privsep":
    import ctypes, errno, signal
    libc = ctypes.CDLL(None, use_errno=True)
    def status_of(pid):
        out = {}
        try:
            for line in open("/proc/%s/status" % pid):
                k, _, v = line.partition(":"); out[k] = v.strip()
        except OSError as e: out["error"] = repr(e)
        return out
    me = status_of("self")
    proxy = os.environ.get("HTTP_PROXY", "")
    m = re.match(r"http://([^:]+):([^@]+)@", proxy)
    def sig1():
        try: os.kill(1, 0); return True
        except OSError as e: return errno.errorcode.get(e.errno, repr(e))
    def sigterm_probe():
        try: os.kill(1, signal.SIGCONT); return True
        except OSError as e: return errno.errorcode.get(e.errno, repr(e))
    def ptrace1():
        for req in (16, 0x4206):                      # PTRACE_ATTACH, PTRACE_SEIZE
            r = libc.ptrace(req, 1, 0, 0)
            if r != -1:
                libc.ptrace(17, 1, 0, 0); return True
        return errno.errorcode.get(ctypes.get_errno(), "?")
    def fds_of_1():
        opened = []
        try: names = os.listdir("/proc/1/fd")
        except OSError as e: return {"listed": repr(e), "opened": []}
        for n in names:
            for flags in (os.O_RDONLY, os.O_WRONLY):
                try: os.close(os.open("/proc/1/fd/" + n, flags)); opened.append((n, flags))
                except OSError: pass
        return {"listed": names, "opened": opened}
    def readable(path):
        try: open(path, "rb").read(1); return True
        except OSError as e: return errno.errorcode.get(e.errno, repr(e))
    def regain():
        res = {}
        u1 = status_of(1).get("Uid", "0").split()[0]
        for name, fn in (("setuid0", lambda: os.setresuid(0, 0, 0)),
                         ("setuid_redactor", lambda: os.setresuid(int(u1), int(u1), int(u1))),
                         ("setgid0", lambda: os.setresgid(0, 0, 0)),
                         ("setgroups", lambda: os.setgroups([0]))):
            try: fn(); res[name] = True
            except OSError as e: res[name] = errno.errorcode.get(e.errno, repr(e))
        # PR_CAP_AMBIENT(47) / PR_CAP_AMBIENT_RAISE(2) of CAP_SETUID(7)
        res["ambient_raise"] = libc.prctl(47, 2, 7, 0, 0) == 0
        # capset adding CAP_SYS_PTRACE (19) to effective+permitted+inheritable
        class Hdr(ctypes.Structure): _fields_ = [("v", ctypes.c_uint32), ("pid", ctypes.c_int)]
        class Dat(ctypes.Structure): _fields_ = [("e", ctypes.c_uint32), ("p", ctypes.c_uint32), ("i", ctypes.c_uint32)]
        data = (Dat * 2)(); data[0].e = data[0].p = data[0].i = 1 << 19
        hdr = Hdr(0x20080522, 0)
        sc = ctypes.CDLL(None, use_errno=True).syscall
        sc.restype = ctypes.c_long
        sc.argtypes = [ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
        ctypes.set_errno(0)
        import platform
        # SYS_capset per architecture (x86-64: 126; 125 is capget); generic table for arm64 et al.
        nr = {"x86_64": 126, "aarch64": 91, "arm64": 91, "riscv64": 91}.get(platform.machine())
        if nr is None: res["capset"] = "UNSUPPORTED_ARCH"; return res
        ret = sc(nr, ctypes.addressof(hdr), ctypes.addressof(data))
        res["capset"] = True if ret == 0 else errno.errorcode.get(ctypes.get_errno(), "?")
        return res
    path = os.path.join(CWD, "ic-priv-" + ins["nonce"])
    open(path, "w").write("child-wrote")
    os.makedirs(os.path.join(CWD, "ic-priv-dir-" + ins["nonce"]), exist_ok=True)
    regained = regain()
    after = status_of("self")
    say(dict(common(), phase="ready", cred=cred_facts(proxy), uids=os.getresuid(), gids=os.getresgid(), groups=os.getgroups(),
             caps={k: me.get(k) for k in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb", "NoNewPrivs")},
             pid=os.getpid(), pid1=status_of(1).get("Name"), pid1_uid=status_of(1).get("Uid"),
             signal_pid1=sig1(), cont_pid1=sigterm_probe(), ptrace_pid1=ptrace1(),
             fd_scan=fds_of_1(), pid1_mem=readable("/proc/1/mem"), pid1_environ=readable("/proc/1/environ"),
             pid1_cmdline=readable("/proc/1/cmdline"), regain=regained, file=path,
             caps_after={k: after.get(k) for k in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb", "NoNewPrivs")}))
    def on_term(*_):
        open(os.path.join(CWD, "ic-term-" + ins["nonce"]), "w").write("graceful"); sys.exit(0)
    signal.signal(signal.SIGTERM, on_term)
    if ins.get("echo"):
        pw = m.group(2) if m else ""
        sys.stdout.write("pw=" + pw[:30]); sys.stdout.flush(); time.sleep(0.3); sys.stdout.write(pw[30:] + "\n"); sys.stdout.flush()
    time.sleep(120)
elif probe == "plant":
'''


class NetworkWorld(dk.IsolationWorld):
    """The isolated world whose `acme` provider runs the s4b probes (egress,
    privsep) and needs the per-attempt network. Its probes are reached through
    the writable `s4b` agent: the default `worker` runs the stock probe, which
    answers `unknown probe` to both."""

    AGENT = "s4b"

    def __init__(self, tmp_path, monkeypatch, owner):
        super().__init__(tmp_path, monkeypatch, owner)
        probe = self.p.fakes["acme"]
        source = ic_probe.PROBE_CLI.replace('if probe == "plant":\n', EXTRA, 1)
        if "import base64" not in source:
            source = "import base64\n" + source
        # the base class already made the entry guest-runnable (`python3 -c TEXT`):
        # put the host script back, then make the new text guest-runnable again
        spawn = dict(probe.entry["spawn"])
        spawn["args"] = spawn["args"][2:]
        script = tmp_path / "ic-s4b-probe.py"
        script.write_text(source)
        probe.entry["bin"], probe.entry["spawn"] = str(script), spawn
        wl.guest_runnable(probe.entry)
        probe.entry["needs"] = {"credentials": False, "network": True, "rpc": False}
        self.p.add_agent(self.AGENT, "acme", "acme/m1")
        self.p.project.setdefault("executor", {})
        self.p.write()
        self._reset_server()

    def probe_node(self, probe: str, agent: str = AGENT, **fields) -> str:
        return super().probe_node(probe, agent, **fields)


@pytest.fixture
def world(tmp_path, monkeypatch, st_live_docker, request):
    dk.require_docker()
    w = NetworkWorld(tmp_path, monkeypatch, dk.test_owner(request))
    w.start()
    yield w
    dk.cleanup(w)


def issued_proxy_url(w, node) -> str:
    """The credential URL the scheduler issued to `node`'s latest attempt, read
    host-side from the invocation file the guest is started from (never from
    anything the guest prints, which the redactor scrubs)."""
    attempt = w.runs(node)[-1]["attempt_id"]
    ids = dk.docker("ps", "-aq", "--no-trunc", "--filter", f"label=multiagents.owner={attempt}").stdout.split()
    assert len(ids) == 1, (attempt, ids)
    info = json.loads(dk.docker("inspect", ids[0]).stdout)[0]
    mounts = [m for m in info["Mounts"] if m["Destination"] == "/multiagents-input"]
    assert mounts, "the invocation mount is missing"
    return json.loads((Path(mounts[0]["Source"]) / "invocation.json").read_text())["env"]["HTTP_PROXY"]


def password_of(url: str) -> str:
    m = re.fullmatch(r"http://(ma1-[^:@/\s]+):([0-9a-f]{64})@ma-egress:8888", url)
    assert m, "credential URL"
    return m.group(2)


def salted(salt: str, password: str) -> str:
    return hashlib.sha256((salt + password).encode()).hexdigest()


def egress(w, **fields):
    node = w.probe_node("egress", **fields)
    return node, w.report(node, timeout=dk.START_WAIT)


def guest_files(w, name: str) -> list[Path]:
    """Files the guest wrote to its workspace: the probe runs with cwd in the
    guest checkout, which is attached to the project worktree under the
    project root, so search the root and everything below it (its worktree)."""
    return list(Path(w.root).rglob(name))


def test_ic_c27_docker_the_run_reaches_only_the_gateway_and_every_bypass_fails(world):
    node, r = egress(world, ports=[22, 80, 443, 2375, 3128, 8080, 8930, 9000], hold=True,
                     direct=["1.1.1.1:443", "8.8.8.8:53", "169.254.169.254:80", "host.docker.internal:80"],
                     dns_servers=["8.8.8.8", "1.1.1.1"])
    assert r["gateway_reachable"] is True, "positive control: ma-egress:8888 must be reachable"
    assert [p for p, v in r["management"].items() if v is True] == [], r["management"]
    assert not any(v is True for v in r["direct"].values()), r["direct"]
    assert not any(v is True for v in r["udp_dns"].values()), r["udp_dns"]
    assert isinstance(r["dns_external"], str) and r["dns_external"].startswith(("OSError", "gaierror", "socket")) \
        or "Error" in r["dns_external"] or "error" in r["dns_external"], \
        f"an external name resolved inside the run: {r['dns_external']}"
    world.stop_victim(node)


def test_ic_c26_docker_the_real_gateway_answers_403_407_and_ignores_forged_headers(world):
    node, r = egress(world, salt="s26", hold=True)
    url = issued_proxy_url(world, node)
    password = password_of(url)
    cred = r["cred"]
    assert cred["parsed"] and cred["user"] == url.split("//")[1].split(":")[0] and cred["user"].startswith("ma1-")
    assert cred["hostport"] == "ma-egress:8888" and cred["pw_len"] == 64
    assert cred["pw_salted_sha256"] == salted("s26", password), "the guest holds a different password than the one issued"
    assert " 403 " in r["evil"] + " ", r["evil"]
    assert " 403 " in r["connect_ssh"] + " ", r["connect_ssh"]
    assert " 407 " in r["no_auth"] + " ", r["no_auth"]
    assert " 403 " in r["forged"] + " ", "forged forwarding headers must prove nothing and must not widen policy"
    assert r["no_proxy"] == "localhost,127.0.0.1,::1,ma-auth" and r["all_proxy"] is None
    world.stop_victim(node)


def test_ic_c26_docker_a_credential_copied_to_another_attempt_network_is_407(world):
    a, ra = egress(world, hold=True)
    b, rb = egress(world, credential=issued_proxy_url(world, a))
    assert " 407 " in rb["evil"] + " ", f"another network's copied credential was accepted: {rb['evil']}"
    assert " 407 " in rb["forged"] + " ", rb["forged"]
    world.stop_victim(a)


def test_ic_c26_docker_two_simultaneous_attempts_have_separate_networks_and_credentials(world):
    a, ra = egress(world, hold=True)
    b, rb = egress(world, hold=True)
    ua, ub = issued_proxy_url(world, a), issued_proxy_url(world, b)
    assert ua != ub and password_of(ua) != password_of(ub)
    assert ra["cred"]["user"] != rb["cred"]["user"]
    assert ra["cred"]["pw_salted_sha256"] != rb["cred"]["pw_salted_sha256"]
    assert " 403 " in ra["evil"] + " " and " 403 " in rb["evil"] + " "
    nets = dk.docker("network", "ls", "--filter", f"label=multiagents.project={world.slug}", "-q").stdout.split()
    assert len(nets) == 2, nets
    for n in nets:
        info = json.loads(dk.docker("network", "inspect", n).stdout)[0]
        assert info["Internal"] is True
    world.stop_victim(a), world.stop_victim(b)


def test_d_s4b_7_docker_logs_hold_only_sanitised_frames_and_the_pid1_redactor_is_out_of_reach(world):
    node, r = egress(world, echo=True, hold=True)
    password = password_of(issued_proxy_url(world, node))
    world.stop_victim(node)
    world.wait(lambda: world.get(node)["state"] in ("done", "held", "failed", "cancelled"), dk.PROBE_WAIT, "the end")
    ids = dk.docker("ps", "-aq", "--filter", f"label=multiagents.project={world.slug}").stdout.split()
    assert ids
    for cid in ids:
        logs = dk.docker("logs", cid)
        assert password not in logs.stdout + logs.stderr, "a raw Docker log copy holds the issued password"
    out, err = world.host_output(world.runs(node)[-1]["run_id"]) if hasattr(world, "host_output") else (b"", b"")
    assert password.encode() not in out + err
    assert password not in world.texts(node)


def test_ic_c31_docker_orphan_network_lifecycle_and_refusals(world):
    slug = world.slug
    def create(project, owner="att_" + "0" * 32):
        out = dk.docker("network", "create", "--internal", "--label", f"multiagents.project={project}",
                        "--label", f"multiagents.owner={owner}",
                        "--label", f"multiagents.creation_key=key-{owner[-6:]}{project[-4:]}", "--label", f"{dk.OWNER_LABEL}={world.owner}",
                        "ic-orphan-" + owner[-6:] + project[-4:])
        assert out.returncode == 0, out.stderr
        return dk.docker("network", "inspect", out.stdout.strip(), "--format", "{{.Id}}").stdout.strip()
    def cli(*a):
        return world.cli(*a, timeout=60)
    orphan, foreign = create(slug), create("somebody-else", "att_" + "1" * 32)
    listed = json.loads(cli("scheduler", "orphans").stdout)["orphans"]
    assert [o["resource_id"] for o in listed if o["resource_kind"] == "network"] == [orphan]
    assert dk.docker("network", "inspect", orphan).returncode == 0, "listing must delete nothing"
    bad = cli("scheduler", "remove-orphan-network", foreign)
    assert bad.returncode != 0 and json.loads(bad.stdout)["error"] == "wrong_project"
    short = cli("scheduler", "remove-orphan-network", orphan[:12])
    assert short.returncode != 0 and json.loads(short.stdout)["error"] == "invalid_id"
    for _ in range(2):
        done = cli("scheduler", "remove-orphan-network", orphan)
        assert done.returncode == 0 and json.loads(done.stdout) == {"ok": True, "network_id": orphan, "state": "absent"}
    assert dk.docker("network", "inspect", orphan).returncode != 0
    assert dk.docker("network", "inspect", foreign).returncode == 0
    dk.docker("network", "rm", foreign)


def test_ic_c31_docker_a_live_attempts_network_is_refused_and_survives(world):
    node, r = egress(world, hold=True)
    nets = dk.docker("network", "ls", "--filter", f"label=multiagents.project={world.slug}", "--no-trunc", "-q").stdout.split()
    assert len(nets) == 1
    refused = world.cli("scheduler", "remove-orphan-network", nets[0], timeout=60)
    assert refused.returncode != 0 and json.loads(refused.stdout)["error"] == "not_orphan"
    assert world.get(node)["state"] == "running" and dk.docker("network", "inspect", nets[0]).returncode == 0
    world.stop_victim(node)


# ------------------------------------------- K4B.7: privilege separation (real Docker)

def privsep(w, **fields):
    nonce = w.nonce("priv")
    node = w.probe_node("privsep", nonce=nonce, hold=True, **fields)
    return node, nonce, w.report(node, phase="ready", timeout=dk.START_WAIT)


def test_d_s4b_7_docker_the_child_runs_as_the_host_identity_with_no_capabilities_and_no_way_back(world):
    node, nonce, r = privsep(world)
    import os
    host = (os.getuid(), os.getgid())
    assert r["uids"] == [host[0]] * 3 and r["gids"] == [host[1]] * 3, (r["uids"], r["gids"])
    assert r["groups"] == [], f"supplementary groups must be cleared: {r['groups']}"
    zero = "0" * 16
    for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
        assert r["caps"][name] == zero, (name, r["caps"])
        assert r["caps_after"][name] == zero, f"{name} changed after the regain attempts: {r['caps_after']}"
    assert r["caps"]["NoNewPrivs"] == "1" and r["caps_after"]["NoNewPrivs"] == "1"
    assert r["regain"]["capset"] == "EPERM", f"capset must be refused with EPERM: {r['regain']['capset']}"
    assert [k for k, v in r["regain"].items() if v is True] == [], r["regain"]
    world.stop_victim(node)


def test_d_s4b_7_docker_the_child_cannot_signal_ptrace_or_inspect_pid_1(world):
    node, nonce, r = privsep(world)
    assert r["pid1_uid"] and r["pid1_uid"].split()[0] != "0", \
        f"the redactor must itself have dropped root before the child runs: {r['pid1_uid']}"
    assert r["signal_pid1"] != True and r["cont_pid1"] != True, (r["signal_pid1"], r["cont_pid1"])    # noqa: E712
    assert r["signal_pid1"] == "EPERM" and r["cont_pid1"] == "EPERM"
    assert r["ptrace_pid1"] in ("EPERM", "EACCES"), r["ptrace_pid1"]
    for name in ("pid1_mem", "pid1_environ"):
        assert r[name] != True, f"/proc/1/{name[5:]} readable by the child"    # noqa: E712
    world.stop_victim(node)


def test_d_s4b_7_docker_the_child_cannot_open_the_redactors_raw_log_descriptors(world):
    node, nonce, r = privsep(world)
    assert r["fd_scan"]["opened"] == [], r["fd_scan"]
    world.stop_victim(node)


def test_d_s4b_7_docker_a_graceful_stop_reaches_the_child_through_the_relay(world):
    node, nonce, r = privsep(world)
    world.stop_victim(node)
    world.wait(lambda: world.get(node)["state"] in ("done", "held", "failed", "cancelled"), dk.PROBE_WAIT, "the stop")
    seen = guest_files(world, "ic-term-" + nonce)
    assert seen, "the child's SIGTERM handler never ran: the stop did not reach the child gracefully"
    assert seen[0].read_text() == "graceful"


def test_d_s4b_7_docker_files_the_child_writes_keep_the_host_uid_and_the_host_can_read_them(world):
    import os
    node, nonce, r = privsep(world)
    found = guest_files(world, "ic-priv-" + nonce) + guest_files(world, "ic-priv-dir-" + nonce)
    assert found, "the child's workspace files are not visible on the host"
    for f in found:
        st = f.stat()
        assert (st.st_uid, st.st_gid) == (os.getuid(), os.getgid()), (str(f), st.st_uid, st.st_gid)
    assert [f.read_text() for f in found if f.is_file()] == ["child-wrote"]
    world.stop_victim(node)


def test_d_s4b_7_docker_the_host_reads_the_redacted_output_through_the_separation(world):
    from multiagents.paths import ProjectPaths
    node, nonce, r = privsep(world, echo=True, salt="s7")
    password = password_of(issued_proxy_url(world, node))
    assert r["cred"]["pw_len"] == 64 and r["cred"]["pw_salted_sha256"] == salted("s7", password)
    def host_out():
        return b"".join((ProjectPaths(world.root).run_dir(run["run_id"]) / "output.ndjson").read_bytes()
                        for run in world.runs(node) if run.get("run_id")
                        and (ProjectPaths(world.root).run_dir(run["run_id"]) / "output.ndjson").is_file())
    world.wait(lambda: b"pw=" in host_out() or None, dk.START_WAIT, "the child's output on the host")
    out = host_out()
    assert b"[REDACTED]" in out and password.encode() not in out
    ids = dk.docker("ps", "-aq", "--filter", f"label=multiagents.project={world.slug}").stdout.split()
    for cid in ids:
        logs = dk.docker("logs", cid)
        assert password not in logs.stdout + logs.stderr, "a raw Docker log copy holds the issued password"
    world.stop_victim(node)
