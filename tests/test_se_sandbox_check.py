"""SE5 (F11, adversary finding): `multiagents docker check` probes accepted
entries only.

Contract: context/specs/findings-c1-c2-part2.md, Part 2b, group SE, SE5. The
command (`cmd_docker`, action `check`; the spec calls it `sandbox check`) is
driven through `cli.main`. `DockerExecutor.ensure_running` is faked (the
container is not what is under test), and the network is faked by a `docker`
first on PATH: `docker exec CONTAINER CMD...` runs CMD on this host with a fake
`curl` first on PATH. The fake curl behaves like curl 8 behind tinyproxy for
the outcome configured per host, and records every URL it was asked for.

Assumption: the probe is made with `curl` inside the container, as today.
No Docker daemon, no network.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import yaml

from multiagents import cli
from multiagents.executor.docker import DockerExecutor
from multiagents.paths import ProjectPaths

sys.path.insert(0, str(Path(__file__).parent / "support"))
from c3_harness import make_git_repo  # noqa: E402


FAKE_DOCKER = r"""#!{python}
import os, subprocess, sys
argv = sys.argv[1:]
if argv[:1] != ["exec"]:
    sys.exit(0)
rest = argv[1:]
with_value = {"-u", "--user", "-e", "--env", "-w", "--workdir", "--env-file"}
while rest and rest[0].startswith("-"):
    option = rest.pop(0)
    if option in with_value and "=" not in option:
        rest.pop(0)
command = rest[1:]  # rest[0] is the container
env = dict(os.environ, PATH=os.environ["SE5_FAKE_BIN"] + os.pathsep + os.environ["PATH"])
sys.exit(subprocess.run(command, env=env).returncode)
"""

# curl 8 behind tinyproxy. `proxy`: tinyproxy's filter answers the CONNECT
# with 403 Filtered. `dns`/`tls`: the request got past the proxy's filter and
# failed afterwards. `http`: the site answered 503.
FAKE_CURL = r"""#!{python}
import json, os, sys
from urllib.parse import urlsplit
args = sys.argv[1:]
state = os.environ["SE5_STATE"]
url = next((a for a in args if "://" in a), args[-1] if args else "")
host = urlsplit(url if "://" in url else "https://" + url).hostname or url
with open(os.path.join(state, "probes.jsonl"), "a") as log:
    log.write(json.dumps(host) + "\n")
outcomes = json.load(open(os.path.join(state, "outcomes.json")))
outcome = outcomes.get(host, outcomes["*"])
fmt = ""
for i, a in enumerate(args):
    if a in ("-w", "--write-out") and i + 1 < len(args):
        fmt = args[i + 1]
    elif a.startswith("--write-out="):
        fmt = a.split("=", 1)[1]
flags = "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))
silent = "s" in flags or "--silent" in args
show_error = "S" in flags or "--show-error" in args
verbose = "v" in flags or "--verbose" in args
fail = "f" in flags or "--fail" in args
table = {
    "ok":    dict(code="200", connect="200", exit=0, error=""),
    "http":  dict(code="503", connect="200", exit=0, error=""),
    "proxy": dict(code="000", connect="403", exit=56,
                  error="CONNECT tunnel failed, response 403"),
    "dns":   dict(code="000", connect="000", exit=6,
                  error=f"Could not resolve host: {host}"),
    "tls":   dict(code="000", connect="200", exit=60,
                  error="SSL certificate problem: unable to get local issuer certificate"),
}[outcome]
if fail and outcome == "http":
    table.update(exit=22, error="The requested URL returned error: 503")
if verbose:
    sys.stderr.write(f"* Uses proxy env variable HTTPS_PROXY == 'http://proxy:8888'\n"
                     f"> CONNECT {host}:443 HTTP/1.1\n")
    if outcome == "proxy":
        sys.stderr.write("< HTTP/1.0 403 Filtered\n< Server: tinyproxy/1.11.1\n")
    else:
        sys.stderr.write("< HTTP/1.0 200 Connection established\n")
if table["error"] and (show_error or not silent):
    sys.stderr.write(f"curl: ({table['exit']}) {table['error']}\n")
body = "" if outcome in ("proxy", "dns", "tls") else "<html></html>"
if "-o" not in args and "--output" not in args:
    sys.stdout.write(body)
out = (fmt.replace("%{http_code}", table["code"]).replace("%{response_code}", table["code"])
          .replace("%{http_connect}", table["connect"])
          .replace("%{exitcode}", str(table["exit"]))
          .replace("%{errormsg}", table["error"]).replace("\\n", "\n"))
sys.stdout.write(out)
sys.exit(table["exit"])
"""


@pytest.fixture
def check(tmp_path, monkeypatch):
    """Run `docker check` for an `egress_allowlist` value; return (exit code or
    exception, output, probed hosts)."""
    state = tmp_path / "net"
    state.mkdir()
    bin_dir, curl_bin = tmp_path / "bin", tmp_path / "curlbin"
    bin_dir.mkdir()
    curl_bin.mkdir()
    for directory, name, source in ((bin_dir, "docker", FAKE_DOCKER),
                                    (curl_bin, "curl", FAKE_CURL)):
        fake = directory / name
        fake.write_text(source.replace("{python}", sys.executable))
        fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SE5_FAKE_BIN", str(curl_bin))
    monkeypatch.setenv("SE5_STATE", str(state))
    monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("MULTIAGENTS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(DockerExecutor, "ensure_running",
                        lambda self: {"ok": True, "container": self.container})

    def run(allowlist, outcomes):
        root = make_git_repo(tmp_path / f"proj{len(list(tmp_path.glob('proj*')))}")
        paths = ProjectPaths(root)
        paths.ensure()
        paths.config.mkdir(parents=True, exist_ok=True)
        (paths.config / "project.yaml").write_text(yaml.safe_dump({"executor": {
            "kind": "docker",
            "docker": {"network": "allowlist", "egress_allowlist": allowlist}}}))
        (state / "outcomes.json").write_text(json.dumps(outcomes))
        probes = state / "probes.jsonl"
        if probes.exists():
            probes.unlink()
        try:
            result = cli.main(["--path", str(root), "docker", "check"])
        except SystemExit as exc:
            result = exc.code
        except Exception as exc:  # SE5: never raises
            result = exc
        hosts = [json.loads(line) for line in probes.read_text().splitlines()] \
            if probes.exists() else []
        return result, hosts
    return run


def output_of(capsys):
    captured = capsys.readouterr()
    return captured.out + captured.err


# A correct sandbox: the accepted host is reached, everything else is
# refused by the proxy's filter.
CORRECT = {"a.com": "ok", "*": "proxy"}


def test_se5_the_probe_uses_the_first_accepted_entry_not_the_raw_first_value(check, capsys):
    result, hosts = check(["b.com:443", "a.com"], CORRECT)
    assert not isinstance(result, Exception), repr(result)
    assert "a.com" in hosts, hosts
    assert not any(h.startswith("b.com") for h in hosts), hosts


def test_se5_the_denied_probe_targets_a_name_under_dot_invalid(check, capsys):
    _, hosts = check(["a.com"], CORRECT)
    denied = [h for h in hosts if h != "a.com"]
    assert denied, f"no denied-host probe was made: {hosts}"
    assert all(h.endswith(".invalid") for h in denied), denied


def test_se5_a_correct_sandbox_is_never_reported_unexpected(check, capsys):
    result, hosts = check(["a.com"], CORRECT)
    output = output_of(capsys)
    assert result == 0, (result, output)
    assert "UNEXPECTED" not in output, output
    assert "a.com" in hosts


@pytest.mark.parametrize("value", [
    pytest.param({"a.com": None}, id="mapping"),
    pytest.param("a.com", id="string"),
    pytest.param(["b.com:443", " a.com", 5], id="no-entry-accepted"),
    pytest.param([], id="empty-list"),
])
def test_se5_with_no_accepted_entry_it_says_there_is_nothing_to_probe(check, capsys, value):
    """A mapping or a string never raises (SE1 refuses it whole), and no host
    is probed as allowed: not the raw value, not a character of it, not a
    built-in default."""
    result, hosts = check(value, CORRECT)
    output = output_of(capsys)
    assert not isinstance(result, Exception), repr(result)
    assert "nothing to probe" in output.lower(), output
    assert all(h.endswith(".invalid") for h in hosts), hosts


@pytest.mark.parametrize("failure", ["dns", "tls", "http"])
def test_se5_a_proxy_refusal_is_reported_apart_from_other_failures(check, capsys, failure):
    """The denied probe's report when the proxy refuses it differs from its
    report when the request got past the filter and then failed."""
    def report(outcomes):
        _, hosts = check(["a.com"], outcomes)
        text = output_of(capsys)
        for host in set(hosts) - {"a.com"}:  # the denied name may vary per run
            text = text.replace(host, "<denied>")
        return text
    refused = report(CORRECT)
    other = report({"a.com": "ok", "*": failure})
    assert refused != other, (
        f"a proxy refusal and a {failure} failure of the denied probe are "
        f"reported identically:\n{refused}")
