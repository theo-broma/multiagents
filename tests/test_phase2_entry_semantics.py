"""R14 and R15 — what an `egress_allowlist` entry *means*, and what it may be.

The contract is `context/specs/phase2-entry-semantics.md` **and its amendment**
("Amendment — the seam for R15, before anyone builds against it"), which wins
wherever the two disagree. It moves R15's seam off `config.load()` and onto
`DockerExecutor.preflight()`, and brings `multiagents docker up` into scope.
So nothing in this file asserts that anything raises at configuration load;
R15 is asserted as problem strings coming back from `preflight()`, and as the
command that starts the environment refusing to start it.

**R14 is about the generated pattern**, so it goes through `h.allowlist_admits`
— the same seam the characterization suite and the two preceding phase-2 files
use. Nothing here asserts the *text* of a generated pattern: an entry that
admits the right hosts and refuses the rest has honoured R14 whatever the
pattern looks like.

**R15 is about a list of strings**, so it goes through `preflight()` and
through `cli.cmd_docker`. Two seams are opened for it, and neither is an
assertion:

- `docker_available` is patched truthy. `preflight` returns early with
  `["docker is not on PATH"]` when it is not, and docker is deliberately absent
  from the container this suite runs in (see the harness docstring). Patching
  it also makes these tests indifferent to *where* in `preflight` the new check
  goes — before that early return or after it, the allowlist problems are the
  only ones left either way.
- `image_exists` is patched True, so a missing workspace or proxy image is not
  confused with a malformed entry.

**What the messages must contain, stated here rather than guessed at.** The
contract asks for a message that says what is wrong, not merely that something
is — "`example.com.` has a trailing dot" against "invalid allowlist entry".
That distinction cannot be tested by asserting a non-empty list, so each form
below carries a small set of words, any ONE of which satisfies it, chosen so
that none of them appears in the offending entry itself:

    a leading or trailing dot   "dot" or "period"
    whitespace                  "whitespace", "space", "blank" or "tab"
    a `:port` suffix            "port"
    a full URL                  "url" or "scheme"

That is a constraint on vocabulary, not on phrasing: any sentence using one of
those words passes, and the five forms may share a message or have one each.

**Where this file is deliberately silent.** Three questions the contract does
not answer, so nothing here asserts an answer to them — see the `## Result`
of the run that wrote this file:

- whether a bare IPv4 address (`192.168.1.10`) or an IPv6 literal (`::1`,
  `[::1]`) is a malformed entry;
- whether the empty string `""` is one;
- whether R15 is the five named forms only, or every string that is not a
  plausible hostname — the adversary's fuzz remainder reads like the latter,
  and `evil.com|.*` and `a(b` (made inert, not noticed, by phase 2 item 1)
  are the cases that separate them.

**Untouched on purpose.** `tests/test_c1_allowlist_characterization.py` holds
the five F11 reproductions and the F10 one. They pin today's silent-and-dead
behaviour, they go red when this contract is implemented, and inverting them
is a separate, deliberate run. Nothing here duplicates them.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c1_harness as h  # noqa: E402


# ---------------------------------------------------------------------------
# Seams
# ---------------------------------------------------------------------------

def _preflight(tmp_path, monkeypatch, allowlist, **config) -> list[str]:
    """`DockerExecutor.preflight()` with everything but the allowlist healthy.

    Docker is reported present and both images built, so an empty result means
    "this configuration is fine" rather than "this machine has no docker".
    """
    import multiagents.executor.docker as docker_mod

    monkeypatch.setattr(docker_mod, "docker_available", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_mod.DockerExecutor, "image_exists",
                        lambda self, name: True)
    executor = h.make_docker_executor(tmp_path, egress_allowlist=allowlist, **config)
    return executor.preflight()


# The five forms of F11, with the variants that are the same form written
# differently. Each row is (id, entry, words any one of which makes the
# message actionable) — see the module docstring for why the words are there.
_WHITESPACE = ("whitespace", "space", "blank", "tab")

MALFORMED = [
    ("leading_dot", ".example.com", ("dot", "period")),
    ("trailing_dot", "example.com.", ("dot", "period")),
    ("leading_space", " example.com", _WHITESPACE),
    ("trailing_space", "example.com ", _WHITESPACE),
    ("surrounding_space", " example.com ", _WHITESPACE),
    ("leading_tab", "\texample.com", _WHITESPACE),
    ("port", "example.com:8080", ("port",)),
    ("port_443", "example.com:443", ("port",)),
    ("url_with_path", "https://example.com/path", ("url", "scheme")),
    ("url_bare", "http://example.com", ("url", "scheme")),
]

_MALFORMED_PARAMS = [pytest.param(entry, words, id=case)
                     for case, entry, words in MALFORMED]


# ---------------------------------------------------------------------------
# R14 — an entry with no dot matches exactly.
# ---------------------------------------------------------------------------

def test_r14_a_dotless_entry_admits_the_literal_host_and_nothing_beneath_it(tmp_path):
    """F10. `com` is one typo away from `example.com`, and today it generates
    a line anchored `(^|\\.)com$` that admits every `.com` host on the
    internet — the operator who made the typo gets an allowlist that is not
    one, and nothing says so.

    The load-bearing assertions are the refusals: a dotless entry must admit
    its own literal text and refuse every host that merely ends with it.
    """
    allowlist = ["com"]
    assert h.allowlist_admits(tmp_path, allowlist, "com")
    for refused in ("example.com", "evil.com", "attacker-controlled.com",
                    "api.storage.example.com", "a.com"):
        assert not h.allowlist_admits(tmp_path, allowlist, refused), \
            f"a dotless entry `com` must not admit {refused!r}"


def test_r14_a_single_label_internal_name_is_still_an_exact_match(tmp_path):
    """The reason the fix is NOT "reject dotless entries": container networks
    carry single-label service names, and an allowlist has to be able to name
    them. `localhost` and `redis` must keep working — and, by the same rule
    that makes `com` safe, must not carry a subdomain with them."""
    for entry in ("localhost", "redis", "postgres", "host"):
        assert h.allowlist_admits(tmp_path, [entry], entry), \
            f"a single-label internal name {entry!r} must still be usable"
        assert not h.allowlist_admits(tmp_path, [entry], f"evil.{entry}"), \
            f"{entry!r} must be an exact match, not a suffix"


def test_r14_a_dotted_entry_keeps_todays_suffix_behaviour(tmp_path):
    """The regression guard that matters most. Every entry in both real
    allowlists is multi-label and every one of them depends on the `(^|\\.)`
    prefix — the contract names it as explicitly out of scope. A fix that made
    *all* entries exact would silently cut the agents off from
    `storage.googleapis.com`, `files.pythonhosted.org` and the rest, which is
    an outage rather than a tightening."""
    allowlist = ["googleapis.com"]
    assert h.allowlist_admits(tmp_path, allowlist, "googleapis.com")
    assert h.allowlist_admits(tmp_path, allowlist, "storage.googleapis.com")
    assert h.allowlist_admits(tmp_path, allowlist, "a.b.googleapis.com")
    # Still a label-boundary suffix, not a string suffix.
    assert not h.allowlist_admits(tmp_path, allowlist, "evil-googleapis.com")
    assert not h.allowlist_admits(tmp_path, allowlist, "googleapis.com.evil.net")


def test_r14_dotless_and_dotted_entries_keep_their_own_rules_in_one_list(tmp_path):
    """The two rules live in the same file and are applied per entry, so a
    real allowlist mixing an internal service name with public domains gets
    exact matching for one and suffix matching for the other."""
    allowlist = ["redis", "example.com"]
    assert h.allowlist_admits(tmp_path, allowlist, "redis")
    assert h.allowlist_admits(tmp_path, allowlist, "example.com")
    assert h.allowlist_admits(tmp_path, allowlist, "api.example.com")
    assert not h.allowlist_admits(tmp_path, allowlist, "cache.redis")
    assert not h.allowlist_admits(tmp_path, allowlist, "evil.net")


def test_r14_a_dotless_entry_is_valid_configuration_not_a_malformed_one(
        tmp_path, monkeypatch):
    """R14 and R15 meet here, and the contract is explicit about which way it
    goes: `com` is narrowed, not refused. An implementation that closed F10 by
    making `preflight` reject every dotless entry would break the single-label
    internal names above, so this rules that fix out by name."""
    assert _preflight(tmp_path, monkeypatch, ["com", "localhost", "redis"]) == []


def test_r14_a_multi_label_public_suffix_is_out_of_scope_and_still_a_suffix(tmp_path):
    """What R14 does NOT close, pinned so nobody reads F10 as fully resolved.

    `co.uk` has a dot, so it takes the dotted rule and keeps admitting every
    host beneath it. Distinguishing a public suffix from an ordinary domain
    needs a public suffix list, which is a dependency and a decision this
    contract declines to take. Asserted rather than left implicit, because the
    obvious "while I am here" improvement is to add one, and doing that
    quietly is how a dependency arrives without a decision.
    """
    assert h.allowlist_admits(tmp_path, ["co.uk"], "anything.co.uk")
    assert h.allowlist_admits(tmp_path, ["github.io"], "someones-pages.github.io")


# ---------------------------------------------------------------------------
# R15 — a malformed entry is a preflight problem, naming the entry.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry,words", _MALFORMED_PARAMS)
def test_r15_a_malformed_entry_is_a_preflight_problem_naming_it(
        tmp_path, monkeypatch, entry, words):
    """F11, at the seam the amendment chose. Each of these five forms produces
    a filter line that matches nothing and says nothing; the operator then
    debugs container routing for hours over an entry that was dead before the
    proxy started.

    `entry.strip()` rather than `entry` is what has to appear, and only
    because a whitespace form is most usefully reported through `repr` — where
    a tab arrives as the two characters `\\t` and the raw character is not in
    the message at all. The whitespace itself is asserted by the companion
    test below, through the word the message has to use.
    """
    problems = _preflight(tmp_path, monkeypatch, [entry])
    assert problems, f"a malformed entry {entry!r} must be refused by preflight"
    joined = " ".join(problems)
    assert entry.strip() in joined, (
        f"the problem must name the offending entry; {entry!r} is not in {joined!r}")


@pytest.mark.parametrize("entry,words", _MALFORMED_PARAMS)
def test_r15_the_problem_says_what_is_wrong_not_merely_that_something_is(
        tmp_path, monkeypatch, entry, words):
    """"`example.com.` has a trailing dot" is actionable; "invalid allowlist
    entry" is not, and a test that only asserted a non-empty list would let
    the second one through. Any ONE of the words below satisfies this, none of
    them appears in the entry itself, and the phrasing is otherwise free —
    see the module docstring for the whole table."""
    problems = _preflight(tmp_path, monkeypatch, [entry])
    joined = " ".join(problems).lower()
    assert any(word in joined for word in words), (
        f"the problem for {entry!r} must say what is wrong with it — expected "
        f"one of {list(words)} in {joined!r}")


def test_r15_every_malformed_entry_in_a_list_gets_its_own_problem(
        tmp_path, monkeypatch):
    """The amendment is explicit: "one per offending entry, naming the entry
    and what is wrong with it". An operator who pasted three bad lines needs
    to fix three, not rediscover the second after fixing the first — and a
    single "the allowlist is invalid" costs exactly that.

    The valid entries alongside them must not be named, or the message stops
    telling anyone which line to edit.
    """
    bad = [".example.com", "example.com.", " example.com ",
           "example.com:8080", "https://example.com/path"]
    good = ["api.anthropic.com", "pypi.org", "localhost"]
    problems = _preflight(tmp_path, monkeypatch, good[:1] + bad + good[1:])

    assert len(problems) == len(bad), (
        f"expected one problem per offending entry ({len(bad)}), got "
        f"{len(problems)}: {problems}")
    for entry in bad:
        assert any(entry.strip() in problem for problem in problems), \
            f"no problem named {entry!r}"
    for entry in good:
        assert not any(entry in problem for problem in problems), \
            f"{entry!r} is valid and must not be reported"


@pytest.mark.parametrize("allowlist", [
    pytest.param([], id="empty"),
    pytest.param(None, id="absent"),
    pytest.param(["example.com"], id="one"),
    pytest.param(["localhost"], id="single_label"),
    pytest.param(["api.anthropic.com", "files.pythonhosted.org",
                  "raw.githubusercontent.com", "static.crates.io",
                  "proxy.golang.org", "redis"], id="realistic"),
])
def test_r15_a_valid_allowlist_produces_no_preflight_problems(
        tmp_path, monkeypatch, allowlist):
    """"A valid allowlist is unaffected." Exact equality rather than "no
    problem mentions the allowlist": R15 adds a refusal to the one code path
    that already refuses `mount_docker_socket`, and a check that fires on a
    correct configuration turns every `multiagents run` into a support
    question. The empty and absent cases are the boundary — a project that has
    not configured egress at all must still preflight clean."""
    assert _preflight(tmp_path, monkeypatch, allowlist) == []


def test_r15_preflight_gives_the_same_answer_every_time_it_is_asked(
        tmp_path, monkeypatch):
    """`preflight` is called from three places — `_executor_problems`,
    `Runner.start_agent`, and `docker status` — and `docker status` prints its
    result. A check that accumulates into state on the executor reports one
    problem the first time and two the second, which reads as the
    configuration having changed underneath the operator."""
    import multiagents.executor.docker as docker_mod

    monkeypatch.setattr(docker_mod, "docker_available", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_mod.DockerExecutor, "image_exists",
                        lambda self, name: True)
    executor = h.make_docker_executor(
        tmp_path, egress_allowlist=["example.com.", "good.example.com"])

    first = executor.preflight()
    assert first, "expected the trailing-dot entry to be refused"
    assert executor.preflight() == first
    assert executor.preflight() == first


# ---------------------------------------------------------------------------
# R15 — and the two real allowlists still load clean.
#
# Read off disk rather than copied in, so the guard keeps working when someone
# edits either file. Both are checked for being non-empty first: an allowlist
# that failed to parse would otherwise pass this test by being nothing.
# ---------------------------------------------------------------------------

def _allowlist_in(path: Path) -> list:
    data = yaml.safe_load(path.read_text()) or {}
    docker = (data.get("executor", {}) or {}).get("docker", {}) or {}
    return docker.get("egress_allowlist")


def _live_project_yaml() -> Path:
    """This repository's own `.multiagents/config/project.yaml`.

    `.multiagents/` is gitignored runtime state, so it exists in the main
    checkout and not in an agent's linked worktree — `--git-common-dir` is
    what points back from one to the other.
    """
    common = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=Path(__file__).resolve().parent, capture_output=True, text=True)
    if common.returncode != 0:
        return Path("/nonexistent")
    git_dir = Path(common.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = (Path(__file__).resolve().parents[1] / git_dir).resolve()
    return git_dir.parent / ".multiagents" / "config" / "project.yaml"


def test_r15_the_shipped_allowlist_produces_no_preflight_problems(
        tmp_path, monkeypatch):
    """`src/multiagents/defaults/project.yaml` is what every new project gets
    from `multiagents init`. A validation rule that refuses it makes the tool
    unusable out of the box, which is a worse failure than the one R15
    closes."""
    shipped = (Path(__file__).resolve().parents[1]
               / "src" / "multiagents" / "defaults" / "project.yaml")
    allowlist = _allowlist_in(shipped)
    assert allowlist and len(allowlist) > 10, \
        f"read no allowlist out of {shipped} — this test would pass vacuously"
    assert _preflight(tmp_path, monkeypatch, allowlist) == []


def test_r15_the_live_project_allowlist_produces_no_preflight_problems(
        tmp_path, monkeypatch):
    """The allowlist this project's own agents are running behind right now.
    It is edited by hand and diverges from the shipped one, so checking the
    shipped file does not cover it."""
    live = _live_project_yaml()
    if not live.is_file():
        pytest.skip(f"no live project config at {live} — nothing to check here")
    allowlist = _allowlist_in(live)
    assert allowlist and len(allowlist) > 10, \
        f"read no allowlist out of {live} — this test would pass vacuously"
    assert _preflight(tmp_path, monkeypatch, allowlist) == []


# ---------------------------------------------------------------------------
# R15 — `multiagents docker up`, the gap the amendment brought into scope.
#
# `docker up` is the command whose entire job is starting the environment, and
# it is the command that WRITES the proxy config — and it is the one caller
# that goes straight to `ensure_running` without asking `preflight` anything.
# Satisfying R15 at the seam without closing this leaves it true on paper and
# absent from the path that matters.
# ---------------------------------------------------------------------------

def _project_with_allowlist(tmp_path: Path, allowlist: list) -> Path:
    root = tmp_path / "project"
    config = root / ".multiagents" / "config"
    config.mkdir(parents=True)
    (config / "project.yaml").write_text(yaml.safe_dump(
        {"executor": {"kind": "docker",
                      "docker": {"image": "img", "proxy_image": "proxy-img",
                                 "network": "allowlist",
                                 "egress_allowlist": list(allowlist)}}}))
    return root


def _docker_up(root: Path, monkeypatch, capsys):
    """Run `multiagents docker up` against `root`, without a docker daemon.

    Returns `(exit_code, everything it printed, [containers it tried to
    start])`. `ensure_running` is stubbed to record and succeed, so "refused"
    is observable as the environment not having been started — rather than as
    a docker call that happened to fail for its own reasons.
    """
    import multiagents.cli as cli
    import multiagents.executor.docker as docker_mod

    started: list[str] = []

    def _ensure_running(self):
        started.append(self.container)
        return {"ok": True, "container": self.container}

    monkeypatch.setattr(docker_mod, "docker_available", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_mod.DockerExecutor, "image_exists",
                        lambda self, name: True)
    monkeypatch.setattr(docker_mod.DockerExecutor, "ensure_running", _ensure_running)

    code = cli.cmd_docker(argparse.Namespace(action="up", path=str(root)))
    captured = capsys.readouterr()
    return code, captured.out + captured.err, started


def test_r15_docker_up_refuses_a_malformed_allowlist_before_starting_anything(
        tmp_path, monkeypatch, capsys):
    """Fail closed and fail loudly, on the command that starts the thing.

    Three things have to be true together, and each rules out a different
    half-fix: a non-zero exit (so a script does not carry on), the offending
    entry in what is printed (so the operator knows which line to edit), and
    nothing started (so the container does not come up behind a proxy config
    generated from an allowlist that was refused).
    """
    entry = "example.com."
    root = _project_with_allowlist(tmp_path, ["api.anthropic.com", entry])

    code, output, started = _docker_up(root, monkeypatch, capsys)

    assert code == 1, "a refused configuration must not exit 0"
    assert entry in output, \
        f"the refusal must name the offending entry; got {output!r}"
    assert started == [], \
        "docker up must consult preflight BEFORE starting the environment"


@pytest.mark.parametrize("entry,words", _MALFORMED_PARAMS)
def test_r15_docker_up_refuses_every_malformed_form(
        tmp_path, monkeypatch, capsys, entry, words):
    """The same refusal for each of the five forms, so `docker up` cannot end
    up checking a narrower set than `preflight` does."""
    root = _project_with_allowlist(tmp_path, ["api.anthropic.com", entry])
    code, output, started = _docker_up(root, monkeypatch, capsys)

    assert code == 1, f"{entry!r} must stop `docker up`"
    assert entry.strip() in output, f"the refusal must name {entry!r}; got {output!r}"
    assert started == []


def test_r15_docker_up_still_starts_on_a_valid_allowlist(
        tmp_path, monkeypatch, capsys):
    """The other half of the same contract, and the one an over-correction
    breaks: a check that refuses everything satisfies the test above and makes
    the tool unusable. A mixed list of the two shapes R14 defines — a
    multi-label domain and a single-label internal name — must come up."""
    root = _project_with_allowlist(
        tmp_path, ["api.anthropic.com", "googleapis.com", "redis"])

    code, _output, started = _docker_up(root, monkeypatch, capsys)

    assert code == 0, "a valid allowlist must not stop `docker up`"
    assert started, "a valid allowlist must reach ensure_running"
