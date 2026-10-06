"""MT-R1..R7: reaching the monitor through a reverse proxy (`--allow-host`,
`--persistent-token`, `--rotate-token`).

Black box: every test starts the real `multiagents monitor` command in a
subprocess (port 0, its own HOME and XDG_STATE_HOME under tmp_path) and talks
to it over HTTP, or reads its exit code and output. The spec names no
in-process seam, so none is used. MT-R7's "existing monitor tests unchanged"
is checked by running those files, not by a test here.
"""

import http.client
import os
import re
import shlex
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
STARTUP_S = 20.0          # a green start prints its URL well inside this
EXIT_S = 20.0             # a refusal exits well inside this
HTTP_S = 5.0
LAUNCH = "import sys; from multiagents.cli import main; sys.exit(main(sys.argv[1:]))"
LOOPBACK_URL = re.compile(r"http://(?:127\.0\.0\.1|localhost):(\d+)/\?token=(\S+)")
TS_URL = "https://{name}/?token={token}"


class Run:
    """One `multiagents monitor` process with its output captured in files."""

    def __init__(self, proc, out, err):
        self.proc, self.out, self.err = proc, out, err

    def stdout(self):
        return self.out.read_text()

    def stderr(self):
        return self.err.read_text()

    def output(self):
        return self.stdout() + self.stderr()

    def url(self):
        return LOOPBACK_URL.search(self.stdout())

    def wait_url(self):
        """(port, token) once printed; fails fast if the process dies first."""
        deadline = time.monotonic() + STARTUP_S
        while time.monotonic() < deadline:
            m = self.url()
            if m:
                return int(m.group(1)), m.group(2)
            if self.proc.poll() is not None:
                m = self.url()
                if m:
                    return int(m.group(1)), m.group(2)
                pytest.fail(f"monitor exited {self.proc.returncode} without a URL:\n"
                            f"{self.output()}")
            time.sleep(0.05)
        pytest.fail(f"no URL printed in {STARTUP_S}s:\n{self.output()}")

    def wait_exit(self):
        try:
            return self.proc.wait(timeout=EXIT_S)
        except subprocess.TimeoutExpired:
            pytest.fail(f"monitor should have refused to start but kept running:\n"
                        f"{self.output()}")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A project, a state home, and a launcher that stops every process."""
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "xdg-state"
    projects = tmp_path / "projects"
    projects.mkdir()
    runs = []

    class World:
        pass

    w = World()
    w.home, w.state, w.tmp = home, state, tmp_path

    def project(name="proj"):
        root = projects / name
        if not root.exists():
            (root / ".multiagents").mkdir(parents=True)   # `init` costs 7 s; a bare marker is enough
        return root

    def env(xdg=True):
        e = {k: v for k, v in os.environ.items()
             if not k.startswith(("MULTIAGENTS_", "CLAUDE_", "XDG_"))}
        e.update(HOME=str(home), PYTHONPATH=str(SRC), PYTHONUNBUFFERED="1",
                 MULTIAGENTS_STATE_DIR=str(tmp_path / "ma-state"),
                 MULTIAGENTS_CONFIG_DIR=str(tmp_path / "ma-config"))
        if xdg:
            e["XDG_STATE_HOME"] = str(state)
        return e

    def start(*flags, name="proj", xdg=True, wait=True):
        root = project(name)
        n = len(runs)
        out, err = tmp_path / f"out{n}", tmp_path / f"err{n}"
        with out.open("w") as o, err.open("w") as e:
            proc = subprocess.Popen(
                [sys.executable, "-c", LAUNCH, "--path", str(root),
                 "monitor", "--port", "0", "--no-browser", *flags],
                cwd=root, env=env(xdg), stdout=o, stderr=e, stdin=subprocess.DEVNULL)
        run = Run(proc, out, err)
        run.root = root
        runs.append(run)
        if wait:
            run.port, run.token = run.wait_url()
        return run

    def stop(run):
        if run.proc.poll() is None:
            run.proc.terminate()
            try:
                run.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                run.proc.kill()
                run.proc.wait()

    w.start, w.stop, w.project = start, stop, project

    def stored_files():
        base = state / "multiagents"
        return sorted(p for p in base.rglob("*") if p.is_file()) if base.exists() else []

    w.stored_files = stored_files
    yield w
    for run in runs:
        stop(run)


def get(run, path="/", host=None, headers=None, method="GET", body=None):
    """(status, body) for a raw request; host=None sends Host: 127.0.0.1:port,
    host="" sends an empty Host header."""
    conn = http.client.HTTPConnection("127.0.0.1", run.port, timeout=HTTP_S)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", f"127.0.0.1:{run.port}" if host is None else host)
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        payload = body.encode() if isinstance(body, str) else body
        conn.putheader("Content-Length", str(len(payload or b"")))
        conn.endheaders(payload)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def page(run, token, host=None):
    return get(run, f"/?token={token}", host=host)


# --------------------------------------------------------------------------
# MT-R1: Host allow-list
# --------------------------------------------------------------------------

@pytest.fixture
def proxied(world):
    """One monitor allowing two names, one written with capitals."""
    return world.start("--allow-host", "phone.example.ts.net",
                       "--allow-host", "second.example.ts.net")


def test_mt_r1_allowed_host_is_served(proxied):
    assert page(proxied, proxied.token, host="phone.example.ts.net")[0] == 200


def test_mt_r1_allowed_host_with_a_port_is_served(proxied):
    assert page(proxied, proxied.token, host="phone.example.ts.net:8443")[0] == 200


def test_mt_r1_match_ignores_case(proxied):
    # The option was given with capitals; the request arrives in other case.
    assert page(proxied, proxied.token, host="phone.example.ts.net")[0] == 200
    assert page(proxied, proxied.token, host="second.EXAMPLE.ts.net")[0] == 200


def test_mt_r1_option_may_be_repeated(proxied):
    assert page(proxied, proxied.token, host="second.example.ts.net:443")[0] == 200


def test_mt_r1_loopback_names_still_work_alongside(proxied):
    assert page(proxied, proxied.token, host=f"localhost:{proxied.port}")[0] == 200
    assert page(proxied, proxied.token)[0] == 200


def test_mt_r1_other_name_is_refused_even_with_the_token(proxied):
    status, body = page(proxied, proxied.token, host="other.example.ts.net")
    assert status == 403
    assert proxied.token.encode() not in body


def test_mt_r1_subdomain_of_an_allowed_name_is_refused(proxied):
    assert page(proxied, proxied.token, host="evil.example.ts.net")[0] == 403
    assert page(proxied, proxied.token, host="evil.example.ts.net:443")[0] == 403


def test_mt_r1_prefix_and_suffix_variants_are_refused(proxied):
    for host in ("phone.example.ts.net.evil.com", "xphone.example.ts.net",
                 "phone.tail-net.ts.ne", "tail-net.example.ts.net"):
        assert page(proxied, proxied.token, host=host)[0] == 403, host


def test_mt_r1_empty_host_is_refused(proxied):
    assert page(proxied, proxied.token, host="")[0] == 403


def test_mt_r1_host_is_refused_before_the_token_is_checked(proxied):
    # A foreign host gets the host refusal whether or not the token is right.
    wrong = get(proxied, "/?token=nope", host="other.example.ts.net")
    right = get(proxied, f"/?token={proxied.token}", host="other.example.ts.net")
    assert wrong[0] == right[0] == 403
    assert wrong[1] == right[1] == b"bad host"


def test_mt_r1_api_routes_also_check_the_host(proxied):
    token = {"X-Monitor-Token": proxied.token}
    assert get(proxied, "/api/settings", host="other.example.ts.net", headers=token)[0] == 403
    assert get(proxied, "/api/settings", host="phone.example.ts.net",
               headers=token)[0] == 200


def test_mt_r1_allowed_host_still_needs_the_token(proxied):
    assert get(proxied, "/", host="phone.example.ts.net")[0] == 403
    assert page(proxied, "wrong", host="phone.example.ts.net")[0] == 403


def test_mt_r1_equals_form_of_the_option_works(world):
    run = world.start("--allow-host=eq.example.ts.net")
    assert page(run, run.token, host="eq.example.ts.net")[0] == 200


# --------------------------------------------------------------------------
# MT-R2: Origin
# --------------------------------------------------------------------------

QUOTA_ID = "/api/quota/identity"
NAME = "phone.example.ts.net"


def origin_status(run, origin, host=NAME):
    headers = {"X-Monitor-Token": run.token}
    if origin is not None:
        headers["Origin"] = origin
    return get(run, QUOTA_ID, host=host, headers=headers, method="POST",
               body='{"provider":"nobody"}')


@pytest.mark.parametrize("origin", [
    f"https://{NAME}", f"http://{NAME}", f"https://{NAME}:8443",
    f"http://{NAME}:80", "https://phone.example.ts.net", None])
def test_mt_r2_origin_of_an_allowed_name_is_accepted(proxied, origin):
    status, body = origin_status(proxied, origin)
    assert status != 403, body


@pytest.mark.parametrize("origin", [
    "https://evil.example", "http://evil.example", "null",
    f"https://evil.{NAME}", f"https://{NAME}.evil.example",
    "https://other.example.ts.net:443", "https://localhost", "https://127.0.0.1"])
def test_mt_r2_cross_site_origin_is_refused(proxied, origin):
    status, body = origin_status(proxied, origin)
    assert status == 403
    assert b"bad origin" in body


def test_mt_r2_allowed_origin_with_a_loopback_host_is_refused(proxied):
    # "when the request's Host is also allowed": here the Host is loopback and
    # the Origin names the proxy, which is not the same site.
    status, body = origin_status(proxied, f"https://{NAME}", host=f"localhost:{proxied.port}")
    assert status == 403
    assert b"bad origin" in body


def test_mt_r2_allowed_origin_with_a_foreign_host_is_refused(proxied):
    assert origin_status(proxied, f"https://{NAME}", host="other.example.ts.net")[0] == 403


def test_mt_r2_loopback_origin_keeps_working(proxied):
    host = f"localhost:{proxied.port}"
    assert origin_status(proxied, f"http://{host}", host=host)[0] != 403


def test_mt_r2_origin_of_an_unlisted_name_is_refused_without_the_flag(world):
    run = world.start()
    assert origin_status(run, f"https://{NAME}")[0] == 403


def test_mt_r2_get_quota_follows_the_same_origin_rule(proxied):
    ok = get(proxied, "/api/quota", host=NAME,
             headers={"X-Monitor-Token": proxied.token, "Origin": f"https://{NAME}"})
    bad = get(proxied, "/api/quota", host=NAME,
              headers={"X-Monitor-Token": proxied.token, "Origin": "https://evil.example"})
    assert ok[0] == 200
    assert bad[0] == 403


# --------------------------------------------------------------------------
# MT-R3: bind address and rejected values
# --------------------------------------------------------------------------

def listening_addresses(port):
    """Local addresses of the sockets listening on `port`, from /proc/net."""
    found = set()
    for table in ("tcp", "tcp6"):
        try:
            lines = Path(f"/proc/net/{table}").read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            addr, _, p = fields[1].rpartition(":")
            if fields[3] == "0A" and int(p, 16) == port:
                found.add(addr)
    return found


LOOPBACK_HEX = {"0100007F", "00000000000000000000000001000000"}   # 127.0.0.1, ::1


def test_mt_r3_bind_is_loopback_with_allow_host(proxied):
    addresses = listening_addresses(proxied.port)
    assert addresses, "found no listening socket for the monitor's port"
    assert addresses == {"0100007F"}, addresses


def test_mt_r3_bind_is_loopback_with_every_option_together(world):
    run = world.start("--allow-host", "a.example.ts.net", "--rotate-token")
    assert listening_addresses(run.port) == {"0100007F"}


def test_mt_r3_loopback_url_is_printed_for_127_0_0_1(proxied):
    assert re.search(r"http://127\.0\.0\.1:\d+/\?token=", proxied.stdout())


REFUSED = {
    "empty": "",
    "wildcard": "*.example.ts.net",
    "bare-wildcard": "*",
    "slash": "phone.example.ts.net/path",
    "scheme": "https://phone.example.ts.net",
    "colon-port": "phone.example.ts.net:8443",
    "space-inside": "phone ts.net",
    "leading-space": " phone.example.ts.net",
    "trailing-space": "phone.example.ts.net ",
    "tab": "phone\tts.net",
    "only-space": "   ",
    "ipv4": "192.0.2.2",
    "loopback-ip": "127.0.0.1",
    "ipv6": "fd7a:115c:a1e0::1",
    "ipv6-bracketed": "[::1]",
}


@pytest.mark.parametrize("value", REFUSED.values(), ids=REFUSED.keys())
def test_mt_r3_refused_value_exits_2_and_nothing_listens(world, value):
    run = world.start("--allow-host", value, wait=False)
    assert run.wait_exit() == 2
    out = run.output()
    assert "unrecognized arguments" not in out      # the flag must exist
    assert "allow-host" in out                       # and say what was refused
    assert not run.url(), "a refused start must not print a serving URL"


def test_mt_r3_one_bad_value_among_good_ones_refuses_the_start(world):
    run = world.start("--allow-host", "good.example.ts.net", "--allow-host", "bad/name",
                      wait=False)
    assert run.wait_exit() == 2
    assert "unrecognized arguments: --allow-host" not in run.output()
    assert "allow-host" in run.output()
    assert not run.url()


def test_mt_r3_hostname_with_digits_and_hyphens_is_not_mistaken_for_an_ip(world):
    run = world.start("--allow-host", "100-64-1-2.example.ts.net")
    assert page(run, run.token, host="100-64-1-2.example.ts.net")[0] == 200


# --------------------------------------------------------------------------
# MT-R4: persistent token
# --------------------------------------------------------------------------

def test_mt_r4_two_starts_share_one_token(world):
    first = world.start("--persistent-token")
    t1 = first.token
    world.stop(first)
    second = world.start("--persistent-token")
    assert second.token == t1
    assert page(second, t1)[0] == 200


def test_mt_r4_start_without_the_flag_mints_a_fresh_token_and_keeps_the_stored_one(world):
    first = world.start("--persistent-token")
    stored = first.token
    files = world.stored_files()
    before = [f.read_bytes() for f in files]
    world.stop(first)
    plain = world.start()
    assert plain.token != stored
    assert page(plain, stored)[0] == 403
    assert page(plain, plain.token)[0] == 200
    world.stop(plain)
    assert [f.read_bytes() for f in world.stored_files()] == before
    again = world.start("--persistent-token")
    assert again.token == stored


def test_mt_r4_the_plain_start_never_reuses_a_token_either(world):
    a = world.start()
    b = world.start()
    assert a.token != b.token


def test_mt_r4_stored_under_xdg_state_home_with_private_modes(world):
    run = world.start("--persistent-token")
    files = world.stored_files()
    assert len(files) == 1
    f = files[0]
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    base = world.state / "multiagents"
    assert stat.S_IMODE(f.parent.stat().st_mode) == 0o700
    d = f.parent
    while d != base.parent:
        assert stat.S_IMODE(d.stat().st_mode) == 0o700 or d == world.state, d
        d = d.parent
    assert run.token in f.read_text()


def test_mt_r4_defaults_to_home_local_state_when_xdg_is_unset(world):
    world.start("--persistent-token", xdg=False)
    base = world.home / ".local" / "state" / "multiagents"
    files = [p for p in base.rglob("*") if p.is_file()]
    assert len(files) == 1
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(base.stat().st_mode) == 0o700 or \
        stat.S_IMODE(files[0].parent.stat().st_mode) == 0o700


def test_mt_r4_nothing_is_written_inside_the_project_tree(world):
    run = world.start("--persistent-token")
    leaked = [p for p in run.root.rglob("*")
              if p.is_file() and ".git/" not in str(p) and not p.is_symlink()
              and run.token.encode() in p.read_bytes()]
    assert leaked == []


def test_mt_r4_one_file_per_project_with_independent_tokens(world):
    a = world.start("--persistent-token", name="alpha")
    b = world.start("--persistent-token", name="beta")
    assert a.token != b.token
    assert len(world.stored_files()) == 2
    world.stop(a)
    world.stop(b)
    assert world.start("--persistent-token", name="alpha").token == a.token
    assert world.start("--persistent-token", name="beta").token == b.token


def refused_start(world, expect_file_unchanged=True):
    files = world.stored_files()
    assert len(files) == 1
    before = (files[0].read_bytes(), stat.S_IMODE(files[0].stat().st_mode))
    run = world.start("--persistent-token", wait=False)
    code = run.wait_exit()
    out = run.output()
    assert code != 0
    assert "unrecognized arguments" not in out
    assert "--rotate-token" in out                   # says what to do about it
    assert not run.url(), "a refused start must not serve"
    # never silently replaced
    assert (files[0].read_bytes(), stat.S_IMODE(files[0].stat().st_mode)) == before
    return run


@pytest.fixture
def stored(world):
    first = world.start("--persistent-token")
    world.stop(first)
    (f,) = world.stored_files()
    f.token = first.token
    return f, first.token


def test_mt_r4_empty_stored_token_is_refused(world, stored):
    f, _ = stored
    f.write_bytes(b"")
    refused_start(world)


def test_mt_r4_whitespace_only_stored_token_is_refused(world, stored):
    f, _ = stored
    f.write_text("  \n")
    refused_start(world)


def test_mt_r4_short_stored_token_is_refused(world, stored):
    f, _ = stored
    f.write_text("abc\n")
    refused_start(world)


def test_mt_r4_stored_token_one_char_short_is_refused(world, stored):
    f, token = stored
    f.write_text(token[:-1] + "\n")
    refused_start(world)


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660, 0o666, 0o620])
def test_mt_r4_stored_token_readable_by_group_or_others_is_refused(world, stored, mode):
    f, _ = stored
    f.chmod(mode)
    refused_start(world)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_mt_r4_unreadable_stored_token_is_refused(world, stored):
    f, _ = stored
    f.chmod(0o000)
    refused_start(world)


def test_mt_r4_rotate_token_recovers_from_a_refused_state(world, stored):
    f, old = stored
    f.chmod(0o644)
    run = world.start("--rotate-token")
    assert run.token != old
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    world.stop(run)
    assert world.start("--persistent-token").token == run.token


def test_mt_r4_minted_token_is_not_shorter_than_what_a_good_stored_one_is(world, stored):
    # The length rule must not reject the token the monitor itself mints.
    _, token = stored
    assert len(token) >= 16
    assert world.start("--persistent-token").token == token


# --------------------------------------------------------------------------
# MT-R5: rotate
# --------------------------------------------------------------------------

def test_mt_r5_rotate_replaces_the_token_and_the_old_one_stops_working(world):
    first = world.start("--persistent-token")
    old = first.token
    world.stop(first)
    rotated = world.start("--rotate-token")
    assert rotated.token != old
    assert page(rotated, old)[0] == 403
    assert page(rotated, rotated.token)[0] == 200


def test_mt_r5_rotated_token_is_the_one_stored_afterwards(world):
    world.stop(world.start("--persistent-token"))
    rotated = world.start("--rotate-token")
    world.stop(rotated)
    assert world.start("--persistent-token").token == rotated.token


def test_mt_r5_rotate_implies_persistent_when_nothing_is_stored_yet(world):
    rotated = world.start("--rotate-token")
    files = world.stored_files()
    assert len(files) == 1
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    world.stop(rotated)
    assert world.start("--persistent-token").token == rotated.token


def test_mt_r5_rotating_twice_gives_a_new_token_each_time(world):
    a = world.start("--rotate-token")
    world.stop(a)
    b = world.start("--rotate-token")
    assert a.token != b.token


def test_mt_r5_rotate_together_with_persistent_is_accepted(world):
    run = world.start("--persistent-token", "--rotate-token")
    assert page(run, run.token)[0] == 200


# --------------------------------------------------------------------------
# MT-R6: printed URLs, token never in argv or logs
# --------------------------------------------------------------------------

def test_mt_r6_start_output_prints_a_url_per_allowed_name(proxied):
    out = proxied.stdout()
    assert TS_URL.format(name="phone.example.ts.net", token=proxied.token) in out or \
        TS_URL.format(name="phone.example.ts.net", token=proxied.token) in out
    assert TS_URL.format(name="second.example.ts.net", token=proxied.token) in out
    assert re.search(r"http://127\.0\.0\.1:\d+/\?token=", out)


def test_mt_r6_the_printed_https_url_has_no_port(proxied):
    for m in re.finditer(r"https://(\S+?)/\?token=", proxied.stdout()):
        assert ":" not in m.group(1)


def test_mt_r6_persistent_token_is_what_the_urls_print(world):
    first = world.start("--persistent-token", "--allow-host", "a.example.ts.net")
    assert TS_URL.format(name="a.example.ts.net", token=first.token) in first.stdout()
    world.stop(first)
    second = world.start("--persistent-token", "--allow-host", "a.example.ts.net")
    assert TS_URL.format(name="a.example.ts.net", token=first.token) in second.stdout()


def test_mt_r6_without_allow_host_no_https_url_is_printed(world):
    run = world.start()
    assert "https://" not in run.stdout()


@pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="needs /proc")
def test_mt_r6_token_is_not_in_the_process_arguments(world):
    run = world.start("--persistent-token", "--allow-host", "a.example.ts.net")
    cmdline = Path(f"/proc/{run.proc.pid}/cmdline").read_bytes()
    assert run.token.encode() not in cmdline


def test_mt_r6_token_is_not_in_stderr_even_after_requests(proxied):
    page(proxied, proxied.token, host=NAME)
    page(proxied, proxied.token, host="other.example.ts.net")
    get(proxied, "/api/settings", headers={"X-Monitor-Token": proxied.token})
    assert proxied.token not in proxied.stderr()


@pytest.mark.parametrize("args", [
    ["--persistent-token", "sekret-value-0123456789"],
    ["--persistent-token=sekret-value-0123456789"],
    ["--rotate-token", "sekret-value-0123456789"],
    ["--rotate-token=sekret-value-0123456789"],
    ["--token", "sekret-value-0123456789"],
    ["--token=sekret-value-0123456789"],
])
def test_mt_r6_no_option_takes_the_token_as_a_value(world, args):
    run = world.start(*args, wait=False)
    assert run.wait_exit() == 2
    # The refusal must be about the stray value, not a missing flag.
    assert f"unrecognized arguments: {args[0].split('=')[0]}" not in run.output() \
        or args[0].startswith("--token")
    assert not run.url()


def test_mt_r6_flags_exist_and_take_no_value(world):
    # Guards the test above against passing only because the flags are missing.
    run = world.start("--persistent-token")
    assert page(run, run.token)[0] == 200


def test_mt_r6_help_lists_the_new_options(world):
    r = subprocess.run([sys.executable, "-c", LAUNCH, "monitor", "--help"],
                       env={**os.environ, "PYTHONPATH": str(SRC)},
                       capture_output=True, text=True, timeout=EXIT_S)
    assert r.returncode == 0
    for flag in ("--allow-host", "--persistent-token", "--rotate-token"):
        assert flag in r.stdout


# --------------------------------------------------------------------------
# MT-R7: nothing changes without the flags
# --------------------------------------------------------------------------

def test_mt_r7_without_flags_only_loopback_hosts_are_served(world):
    run = world.start()
    assert page(run, run.token)[0] == 200
    assert page(run, run.token, host=f"localhost:{run.port}")[0] == 200
    assert page(run, run.token, host="phone.example.ts.net")[0] == 403
    assert page(run, run.token, host="")[0] == 403


def test_mt_r7_without_flags_no_token_file_is_written(world):
    world.start()
    assert world.stored_files() == []


def test_mt_r7_without_flags_output_is_the_loopback_url_only(world):
    run = world.start()
    assert run.url()
    assert "https://" not in run.output()


def test_mt_r7_without_flags_bind_is_loopback(world):
    run = world.start()
    assert listening_addresses(run.port) == {"0100007F"}


def test_mt_r7_foreign_origin_is_still_refused_without_flags(world):
    run = world.start()
    host = f"127.0.0.1:{run.port}"
    assert origin_status(run, "https://evil.example", host=host)[0] == 403
    assert origin_status(run, f"http://{host}", host=host)[0] != 403
