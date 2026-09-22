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
    an IPv6 literal             "ipv6" or "address", and NOT the bare word
                                "port" — the contract rules that one out by
                                name, so it is the single place in this table
                                where a word is forbidden as well as required
    the empty string            "empty"

That is a constraint on vocabulary, not on phrasing: any sentence using one of
those words passes, and the forms may share a message or have one each.

**Where this file was deliberately silent, and now is not.** The run that
wrote it stopped on four questions the contract did not answer. They are
answered in its closing section, "Amendment — four questions the test engineer
asked, answered", and the tests at the bottom of this file assert those
answers rather than a guess at them:

- R15's scope is **every non-hostname**, not the five named forms only — so
  `evil.com|.*` and `a(b`, which phase 2 item 1 made inert but not *noticed*,
  are refused as well;
- a bare IPv4 literal (`192.168.1.10`) is **valid**, and matches exactly by
  the same rule R14 gives a dotless entry; an IPv6 literal (`::1`, `[::1]`)
  is **malformed**;
- the empty string is **malformed**, and is named as empty;
- under `network: bridge` and `network: none` the allowlist is **not
  validated at all**, because it is unused there.

**Untouched on purpose.** `tests/test_c1_allowlist_characterization.py` holds
the five F11 reproductions and the F10 one. They pin today's silent-and-dead
behaviour, they go red when this contract is implemented, and inverting them
is a separate, deliberate run. Nothing here duplicates them.
"""

from __future__ import annotations

import argparse
import re
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


# ---------------------------------------------------------------------------
# R15 — the scope question: every non-hostname, not the five named forms.
#
# `NEED_INFO(scope-of-r15)` is answered "every non-hostname": R15 validates
# against a hostname grammar, and the five F11 reproductions are examples of
# what that catches rather than the whole list. The adversary's fuzz remainder
# is in the contract by name — "nothing validates that an entry is a hostname
# at all" — and F11's complaint was never about what a dead entry matches, it
# is that the operator is not told. `a(b` is exactly as dead and exactly as
# silent as `example.com.`.
#
# No word set for these, on purpose. The contract fixes a vocabulary for the
# five named forms and for the two the amendment adds, and says nothing about
# how to phrase "this is not a hostname" — so nothing below asserts a phrasing
# nobody agreed to. Naming the entry is the whole requirement here.
# ---------------------------------------------------------------------------

NOT_HOSTNAMES = [
    # The two the amendment names, and the two phase 2 item 1 made inert.
    ("alternation", "evil.com|.*"),
    ("unbalanced_group", "a(b"),
    # The rest of item 1's reproductions, which are the same class of string.
    ("unbalanced_class", "a[b"),
    ("balanced_group", "a(b)c"),
    ("character_class", "a[bc]d"),
    ("glob", "*.example.com"),
    ("quantifier", "a*b"),
    # Not regex at all, just not a hostname: characters no label may carry.
    ("interior_space", "exam ple.com"),
    ("path_without_scheme", "example.com/path"),
    ("userinfo", "user@example.com"),
]

_NOT_HOSTNAME_PARAMS = [pytest.param(entry, id=case) for case, entry in NOT_HOSTNAMES]


@pytest.mark.parametrize("entry", _NOT_HOSTNAME_PARAMS)
def test_r15_a_string_that_is_not_a_hostname_at_all_is_a_preflight_problem_naming_it(
        tmp_path, monkeypatch, entry):
    """None of these is one of the five F11 forms, and every one of them is
    dead on arrival: it generates a filter line that matches no host any
    resolver will ever be asked for, and says nothing about it.

    An implementation that enumerated the five forms — a leading dot, a
    trailing dot, whitespace, `:port`, a URL — and let everything else past
    satisfies every other R15 test in this file and fails these. That is the
    distinction they exist to draw.
    """
    problems = _preflight(tmp_path, monkeypatch, [entry])
    assert problems, (
        f"{entry!r} is not a hostname and must be refused by preflight — R15 "
        f"is every non-hostname, not only the five named forms")
    joined = " ".join(problems)
    assert entry in joined, (
        f"the problem must name the offending entry; {entry!r} is not in "
        f"{joined!r}")


@pytest.mark.parametrize("entry,r12_pins_its_own_literal", [
    pytest.param("evil.com|.*", True, id="alternation"),
    pytest.param("a(b", False, id="unbalanced_group"),
])
def test_r15_an_entry_it_refuses_is_still_inert_if_it_reaches_the_generator(
        tmp_path, monkeypatch, entry, r12_pins_its_own_literal):
    """Deliberate belt and braces, asserted about one string in one test so
    that the relationship between the two phase 2 items is visible instead of
    having to be inferred from two files that never mention each other.

    They are different seams and the amendment says so in as many words: item
    1 (R12, `tests/test_phase2_ere_escaping.py`) governs what
    `write_proxy_config` does with an entry; R15 governs whether an entry
    reaches it. So R15 is a signal added on top of inertness, not a
    replacement for it — an entry that somehow gets past preflight, through a
    caller that does not preflight or a future flag that skips it, must still
    match nothing but itself.

    Which means the literal-match half must keep passing. It is
    `test_r12_f1_entry_containing_a_pipe_admits_only_its_own_literal_text` and
    `test_r12_f13_entry_with_an_unbalanced_group_or_class_decides_without_raising`
    restated against the same two strings: an implementer who satisfies R15 by
    having the generator drop, rewrite or raise on a refused entry turns that
    file red, and this test red with it.

    `a(b` carries `r12_pins_its_own_literal=False` because R12's own contract
    declines to say what that entry matches — only that evaluating it must not
    raise. Asserting more about it here would be inventing a requirement the
    other file deliberately refused to make.
    """
    # R15: preflight refuses it, and names it.
    problems = _preflight(tmp_path, monkeypatch, [entry])
    assert problems, f"a non-hostname entry {entry!r} must be refused by preflight"
    assert entry in " ".join(problems), \
        f"the problem must name {entry!r}; got {problems!r}"

    # Item 1 / R12: and on the other side of that gate it is still inert.
    try:
        admits_itself = h.allowlist_admits(tmp_path, [entry], entry)
        admits_unrelated = h.allowlist_admits(tmp_path, [entry], "anything.example")
    except Exception as exc:  # noqa: BLE001 — R12 says none may escape
        pytest.fail(
            f"R15 must not change what the generator does with {entry!r}: "
            f"{type(exc).__name__}: {exc}")

    assert not admits_unrelated, (
        f"{entry!r} must still admit nothing it does not name, even when "
        f"preflight has already refused it")
    if r12_pins_its_own_literal:
        assert admits_itself, (
            f"{entry!r} must still match its own literal text — R15 gates the "
            f"entry, it does not rewrite it")


# ---------------------------------------------------------------------------
# R14/R15 — a bare IPv4 literal is valid, and matches exactly.
#
# `NEED_INFO(ip-entries)`, first half. An operator naming a host by address is
# naming one host, so the amendment gives an IPv4 literal the same rule R14
# gives a dotless entry: it admits that address and nothing else.
#
# The exactness is the point rather than a detail. An IPv4 literal is full of
# dots, so an implementation that decides "dot present, therefore suffix" will
# hand it `(^|\.)` and admit every host ending `.192.168.1.10` — which is
# F10's mistake reached from the other direction, and unlike `com` it is a
# shape an attacker can register a subdomain for.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", [
    pytest.param("192.168.1.10", id="rfc1918"),
    pytest.param("127.0.0.1", id="loopback"),
    pytest.param("10.0.0.1", id="private_a"),
    pytest.param("8.8.8.8", id="public"),
    pytest.param("255.255.255.255", id="max_octets"),
    pytest.param("0.0.0.0", id="min_octets"),
])
def test_r15_a_bare_ipv4_literal_is_valid_configuration_not_a_malformed_one(
        tmp_path, monkeypatch, entry):
    """An address is a legitimate thing to put in an allowlist, so preflight
    must let it through. Asserted at both octet boundaries as well as the
    ordinary case, because a validator built out of a hostname grammar can
    reject `0.0.0.0` and `255.255.255.255` by accident — an all-numeric label
    is not a valid *hostname* label, and an IP entry has nothing but."""
    assert _preflight(tmp_path, monkeypatch, [entry]) == [], \
        f"a bare IPv4 literal {entry!r} is valid configuration"


def test_r14_an_ipv4_entry_admits_that_address_and_nothing_beneath_it(tmp_path):
    """The load-bearing half, and the reason the amendment answered this
    question at all: `foo.192.168.1.10` is a host nobody means to admit.

    Today the entry generates `(^|\\.)192\\.168\\.1\\.10$` and `foo.` in front
    of it matches the optional-dot prefix, so a name an attacker controls is
    inside the boundary. Letting the dotted-entry suffix rule apply to an
    address by accident is F10's class of mistake, so it is pinned here rather
    than left to follow from R14's wording about dots.
    """
    entry = "192.168.1.10"
    assert h.allowlist_admits(tmp_path, [entry], entry), \
        "an IPv4 entry must admit its own address"
    for refused in ("foo.192.168.1.10", "evil.example.com.192.168.1.10",
                    "a.192.168.1.10"):
        assert not h.allowlist_admits(tmp_path, [entry], refused), \
            f"an IPv4 entry must match exactly; {refused!r} is not that address"


def test_r14_an_ipv4_entry_does_not_admit_its_neighbours_by_prefix_or_suffix(
        tmp_path):
    """The near misses either side of an exact match, which a fix built from
    string containment rather than a whole-value comparison would admit:
    another address this one is a prefix of, and one it is a suffix of."""
    entry = "192.168.1.1"
    assert h.allowlist_admits(tmp_path, [entry], entry)
    for refused in ("192.168.1.10", "192.168.1.100", "1192.168.1.1",
                    "192.168.1.2"):
        assert not h.allowlist_admits(tmp_path, [entry], refused), \
            f"{entry!r} must not admit {refused!r}"


def test_r14_an_ipv4_entry_and_a_domain_entry_keep_their_own_rules_together(
        tmp_path):
    """Both rules in one realistic list: an internal address alongside a
    public domain. The domain keeps its subdomains, the address does not
    acquire any, and neither widens the other."""
    allowlist = ["192.168.1.10", "googleapis.com"]
    assert h.allowlist_admits(tmp_path, allowlist, "192.168.1.10")
    assert h.allowlist_admits(tmp_path, allowlist, "storage.googleapis.com")
    assert not h.allowlist_admits(tmp_path, allowlist, "foo.192.168.1.10")
    assert not h.allowlist_admits(tmp_path, allowlist, "evil.net")


# ---------------------------------------------------------------------------
# R15 — an IPv6 literal is malformed, and is told it is an address.
#
# `NEED_INFO(ip-entries)`, second half. Not because `::1` is meaningless, but
# because tinyproxy's host filter does not handle it: admitting the entry
# generates a pattern that quietly matches nothing, which is F11 again in a
# new costume.
#
# Both spellings are the same answer. `::1` is what an operator copies out of
# a config file, `[::1]` is what they copy out of a URL, and a validator that
# catches one by shape will often miss the other.
#
# The message constraint has two halves, and the second is the unusual one:
# the contract says a "looks like a port" message about `::1` is worse than no
# message, and asks for the IPv6 case to be stated in the validator rather
# than caught by side effect from the `:port` rule. So this is the one form
# where a word is forbidden as well as required.
# ---------------------------------------------------------------------------

_IPV6_ADDRESS_WORDS = ("ipv6", "address")

IPV6 = [
    ("loopback", "::1"),
    ("loopback_bracketed", "[::1]"),
    ("documentation", "2001:db8::1"),
    ("documentation_bracketed", "[2001:db8::1]"),
    ("link_local", "fe80::1"),
    ("unspecified", "::"),
    ("full_form", "2001:0db8:0000:0000:0000:0000:0000:0001"),
]

_IPV6_PARAMS = [pytest.param(entry, id=case) for case, entry in IPV6]


@pytest.mark.parametrize("entry", _IPV6_PARAMS)
def test_r15_an_ipv6_literal_is_a_preflight_problem_naming_it(
        tmp_path, monkeypatch, entry):
    """An IPv6 entry reaches the filter as a pattern no destination host will
    ever match, and today nothing says so — the operator who wrote `::1`
    meaning their local registry gets an allowlist one entry shorter than
    they think it is, with no signal at all."""
    problems = _preflight(tmp_path, monkeypatch, [entry])
    assert problems, (
        f"an IPv6 literal {entry!r} must be refused by preflight — "
        f"tinyproxy's host filter cannot express it")
    joined = " ".join(problems)
    assert entry in joined, (
        f"the problem must name the offending entry; {entry!r} is not in "
        f"{joined!r}")


@pytest.mark.parametrize("entry", _IPV6_PARAMS)
def test_r15_the_ipv6_problem_names_an_address_form_and_does_not_say_port(
        tmp_path, monkeypatch, entry):
    """"A message saying "looks like a port" about `::1` is worse than no
    message" — the contract, verbatim, and the reason it asks for the IPv6
    case to be stated in the validator instead of falling out of the `:port`
    rule. An operator told their loopback address has a port suffix goes
    looking for a port to delete and finds none.

    So: an address word must be present, and the word "port" must be absent.
    `\\bport\\b` rather than a substring test, because "not supported" is a
    perfectly good thing for the message to say and contains "port" —
    forbidding that spelling would be pinning phrasing rather than meaning.
    """
    problems = _preflight(tmp_path, monkeypatch, [entry])
    joined = " ".join(problems).lower()
    assert any(word in joined for word in _IPV6_ADDRESS_WORDS), (
        f"the problem for {entry!r} must name it as an address form — "
        f"expected one of {list(_IPV6_ADDRESS_WORDS)} in {joined!r}")
    assert not re.search(r"\bport\b", joined), (
        f"{entry!r} has no port in it; a port message here sends the operator "
        f"looking for something that is not there: {joined!r}")


def test_r15_an_ipv6_literal_alongside_valid_entries_is_the_only_one_reported(
        tmp_path, monkeypatch):
    """The realistic paste: one address among working entries. Exactly one
    problem, and it names the address rather than the list."""
    good = ["api.anthropic.com", "pypi.org", "localhost", "192.168.1.10"]
    problems = _preflight(tmp_path, monkeypatch, good[:2] + ["::1"] + good[2:])

    assert len(problems) == 1, f"expected one problem, got {problems}"
    assert "::1" in problems[0], f"the problem must name `::1`; got {problems[0]!r}"
    for entry in good:
        assert entry not in problems[0], \
            f"{entry!r} is valid and must not be reported"


# ---------------------------------------------------------------------------
# R15 — the empty string is malformed, and is named as empty.
#
# `NEED_INFO(empty-entry)`, answered: refused, and told it is empty. It is
# not one of the five forms and it is unmistakably not a hostname.
#
# It is also the entry most likely to arrive by accident rather than by
# mistake — a trailing `- ` in the YAML list, a templated value that resolved
# to nothing — and today it generates `(^|\.)$`, a line that matches the empty
# host and nothing else. Silent, dead, and invisible in a diff.
#
# "empty" is asserted instead of the entry appearing in the message, because
# there is no text to look for: naming the entry and saying what is wrong with
# it are the same sentence here.
# ---------------------------------------------------------------------------

def test_r15_the_empty_string_is_a_preflight_problem_saying_it_is_empty(
        tmp_path, monkeypatch):
    """The whole of the requirement, in one assertion each: refused, and the
    message says which kind of nothing it is."""
    problems = _preflight(tmp_path, monkeypatch, [""])
    assert problems, "an empty allowlist entry must be refused by preflight"
    joined = " ".join(problems).lower()
    assert "empty" in joined, (
        f"the problem for an empty entry must say it is empty — an entry with "
        f"no text to quote is the one case where the word is the whole "
        f"message: {joined!r}")


def test_r15_an_empty_entry_is_reported_without_swallowing_the_valid_ones(
        tmp_path, monkeypatch):
    """The boundary the empty string is dangerous at: an entry with no text
    cannot be pointed at by quoting it, so a message built by substituting the
    entry into a sentence produces `allowlist entry '' is invalid` — or, worse,
    a problem that names one of the neighbouring entries instead.

    An empty entry alongside valid ones is also the shape this actually
    arrives in. A list that is *entirely* empty entries is not: the empty
    LIST is a separate and valid case, already pinned above.
    """
    good = ["api.anthropic.com", "pypi.org", "localhost"]
    problems = _preflight(tmp_path, monkeypatch, [good[0], "", good[1], good[2]])

    assert len(problems) == 1, f"expected one problem, got {problems}"
    assert "empty" in problems[0].lower()
    for entry in good:
        assert entry not in problems[0], \
            f"{entry!r} is valid and must not be named in the empty entry's problem"


@pytest.mark.parametrize("entry", [
    pytest.param(" ", id="one_space"),
    pytest.param("   ", id="several_spaces"),
    pytest.param("\t", id="tab"),
    pytest.param("\n", id="newline"),
])
def test_r15_a_whitespace_only_entry_is_refused_too(tmp_path, monkeypatch, entry):
    """The boundary between the empty case and the whitespace case, and the
    one the two answers meet at: `"   "` is whitespace by its characters and
    empty by its content.

    The contract does not choose between those two messages, so this asserts
    only that the entry is refused — which both answers agree on, and which
    follows from the scope answer above whichever way the wording lands.
    Pinning a word here would be inventing a decision nobody made.
    """
    assert _preflight(tmp_path, monkeypatch, [entry]), \
        f"a whitespace-only entry {entry!r} is not a hostname and must be refused"


# ---------------------------------------------------------------------------
# R15 — and NOT under `network: bridge` or `network: none`.
#
# The fourth answer, and the only one that takes something out of scope rather
# than putting something in. The allowlist is unused under those modes —
# `write_proxy_config` runs only for `allowlist`, and `ensure_proxy` returns
# `{"ok": True, "skipped": ...}` — so refusing to start a bridge-network
# environment over a key it ignores is the same mistake the amendment avoided
# by moving R15 off `config.load()`: a check that fires where it does not
# apply. An operator who switches to `allowlist` later gets the refusal then,
# which is the moment it means something.
#
# These pass today, and they pass for a reason that will stop being true:
# nothing validates the allowlist at all yet. They are guards, not coverage —
# their job starts the moment the check exists, and what they rule out is the
# obvious implementation, a loop over `egress_allowlist` at the top of
# `preflight` with no look at `network_mode`. Every other R15 test in this
# file is what stops them being vacuous, by failing if the check never fires
# anywhere.
# ---------------------------------------------------------------------------

_UNVALIDATED_MODES = [pytest.param("bridge", id="bridge"),
                      pytest.param("none", id="none")]

# Deliberately one of every form R15 refuses under `allowlist`: the five F11
# reproductions, a non-hostname, an IPv6 literal and the empty string. If any
# single rule is applied unconditionally, this list finds it.
EVERY_MALFORMED_FORM = [
    ".example.com", "example.com.", " example.com ", "example.com:8080",
    "https://example.com/path", "evil.com|.*", "::1", "",
]


@pytest.mark.parametrize("network", _UNVALIDATED_MODES)
def test_r15_a_malformed_allowlist_is_not_validated_under_bridge_or_none(
        tmp_path, monkeypatch, network):
    """Exact equality with the empty list, not "no problem mentions the
    allowlist": under these modes the key is inert, so the correct number of
    problems it can produce is zero.

    Every form R15 refuses is in the list at once, so a rule applied
    unconditionally is caught whichever rule it is.
    """
    assert _preflight(tmp_path, monkeypatch, EVERY_MALFORMED_FORM,
                      network=network) == [], (
        f"the egress allowlist is unused under `network: {network}` — refusing "
        f"to start over a key this mode ignores is a check firing where it "
        f"does not apply")


@pytest.mark.parametrize("network", _UNVALIDATED_MODES)
def test_r15_a_valid_allowlist_is_also_clean_under_bridge_or_none(
        tmp_path, monkeypatch, network):
    """The other half, so the test above cannot be satisfied by a preflight
    that reports something unrelated under these modes and happens not to
    mention the allowlist. Nothing about switching network mode is a
    problem."""
    assert _preflight(tmp_path, monkeypatch,
                      ["api.anthropic.com", "localhost", "192.168.1.10"],
                      network=network) == []


def test_r15_the_same_malformed_allowlist_is_refused_under_allowlist_mode(
        tmp_path, monkeypatch):
    """The contrast that gives the two tests above their meaning, written out
    rather than left to the reader: the identical list, the only difference
    being `network: allowlist`, and now every entry in it is a problem.

    Read together, the three say what the answer actually was — not "this list
    is fine" but "this list is not looked at here".
    """
    problems = _preflight(tmp_path, monkeypatch, EVERY_MALFORMED_FORM,
                          network="allowlist")
    assert len(problems) == len(EVERY_MALFORMED_FORM), (
        f"expected one problem per entry ({len(EVERY_MALFORMED_FORM)}), got "
        f"{len(problems)}: {problems}")
