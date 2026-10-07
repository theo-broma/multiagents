"""Characterization of C2's plugin seam: `scripts.py`'s invocation of provider
scripts — `run_action`, `exec_action`, `build_env`, `resolve`, `find_script`,
`script_argv`.

This pins what the seam DOES, including behaviour that looks wrong. Findings
for anything surprising are filed in `context/review/C2-seam.md` as F110+.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c2_harness as h  # noqa: E402


# ---------------------------------------------------------------------------
# run_action — exit codes
# ---------------------------------------------------------------------------

def test_run_action_returns_exit_0_and_both_streams_on_success(tmp_path):
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", 'check) echo out1; echo err1 >&2; exit 0 ;;')
    code, out, err = h.run_action("p", provider, h.FakeExecutor(), "check", tmp_path)
    assert (code, out, err) == (0, "out1\n", "err1\n")


def test_run_action_passes_through_an_arbitrary_nonzero_exit_code(tmp_path):
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", "check) exit 7 ;;")
    code, out, err = h.run_action("p", provider, h.FakeExecutor(), "check", tmp_path)
    assert (code, out, err) == (7, "", "")


def test_run_action_returns_64_for_an_action_the_script_does_not_implement(tmp_path):
    """`case_script`'s fallthrough `*) exit 64` — matches scripts.UNIMPLEMENTED."""
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", "check) exit 0 ;;")
    code, _, _ = h.run_action("p", provider, h.FakeExecutor(), "budget", tmp_path)
    assert code == 64  # scripts.UNIMPLEMENTED


def test_run_action_returns_127_with_a_named_provider_when_no_script_exists(tmp_path):
    provider = h.make_provider("nope")
    code, out, err = h.run_action("nope", provider, h.FakeExecutor(), "check", tmp_path)
    assert code == 127
    assert out == ""
    assert err == "no script for provider 'nope'"


# ---------------------------------------------------------------------------
# run_action — stdout / stderr content
# ---------------------------------------------------------------------------

def test_run_action_does_not_strip_trailing_newlines_or_lack_thereof(tmp_path):
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", 'check) printf "no-newline"; exit 0 ;;')
    code, out, err = h.run_action("p", provider, h.FakeExecutor(), "check", tmp_path)
    assert out == "no-newline"  # no trailing \n appended or stripped


def test_run_action_captures_a_large_stdout_payload_in_full(tmp_path):
    provider = h.make_provider("p")
    h.case_script(
        tmp_path, "p.sh",
        'check) python3 -c "import sys; sys.stdout.write(\'x\' * 2000000)"; exit 0 ;;',
    )
    code, out, err = h.run_action("p", provider, h.FakeExecutor(), "check", tmp_path)
    assert code == 0
    assert len(out) == 2_000_000
    assert out == "x" * 2_000_000


def test_run_action_does_not_deadlock_on_simultaneous_large_stdout_and_stderr(tmp_path):
    """Both streams are read via `communicate()`, which uses a selector rather
    than reading stdout then stderr in sequence — a script that fills BOTH
    pipe buffers before either is drained would deadlock a naive sequential
    reader. Pin that this one does not."""
    provider = h.make_provider("p")
    h.case_script(
        tmp_path, "p.sh",
        'check) python3 -c "'
        'import sys; sys.stdout.write(\'a\' * 500000); sys.stderr.write(\'b\' * 500000)"; '
        'exit 3 ;;',
    )
    code, out, err = h.run_action("p", provider, h.FakeExecutor(), "check", tmp_path, timeout=10)
    assert code == 3
    assert out == "a" * 500000
    assert err == "b" * 500000


def test_run_action_decodes_non_utf8_output_with_replacement_and_never_raises(tmp_path):
    """A script that writes bytes that are not UTF-8 on either stream still
    comes back as a status tuple: its exit code, the valid text intact, and
    each invalid byte as U+FFFD. `run_action`'s "never raises" covers this
    fourth way for a child to misbehave too.

    Inverted deliberately: this pinned F110 (`UnicodeDecodeError` straight out
    of `run_action`), fixed in d4ae4ec under
    context/specs/phase0-context-and-team.md, P0-R8c attack finding 1 and the
    "Decided, from the implementer's read (ag-829577)" block. The driver-level
    consequence is covered by
    test_phase0_contract_b_attack_driver.py::test_attack_r8c_3_non_utf8_output_from_compact_does_not_crash_the_driver;
    this one pins the seam itself, for every captured action."""
    provider = h.make_provider("p")
    h.write_script(
        tmp_path, "p.sh",
        "#!/bin/sh\n"
        'case "$1" in\n'
        "check) printf 'ok\\377\\376end'; printf 'err\\200\\201' >&2; exit 3 ;;\n"
        "esac\n",
    )
    code, out, err = h.run_action("p", provider, h.FakeExecutor(), "check", tmp_path)
    assert code == 3
    assert out == "ok\ufffd\ufffdend"
    assert err == "err\ufffd\ufffd"


# ---------------------------------------------------------------------------
# run_action — timeout
# ---------------------------------------------------------------------------

def test_run_action_returns_124_on_timeout_with_the_reason_in_stderr(tmp_path):
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", "check) sleep 5 ;;")
    code, out, err = h.run_action("p", provider, h.FakeExecutor(), "check", tmp_path, timeout=1)
    assert code == 124
    assert out == ""
    assert "TimeoutExpired" in err
    assert "timed out after 1 seconds" in err


def test_run_action_timeout_kills_a_backgrounded_grandchild_too(tmp_path):
    """On timeout the whole process group the action started is killed, not
    only the direct child: work the script forked into the background does
    not run on after `run_action` has reported the attempt timed out, and the
    call returns at the timeout rather than when the grandchild would finish.

    Inverted deliberately: this pinned F111 (the grandchild was reparented and
    ran to completion), fixed in d4ae4ec under
    context/specs/phase0-context-and-team.md, P0-R8c attack finding 3 and the
    "Decided, from the implementer's read (ag-829577)" block ("on timeout the
    whole process group is killed. This holds for every captured action").
    The driver-level `compact` case is
    test_phase0_contract_b_attack_driver.py::test_attack_r8c_3_a_timed_out_compaction_does_not_run_on_into_the_next_turn;
    this one pins the seam with a shell-backgrounded grandchild."""
    provider = h.make_provider("p")
    marker = tmp_path / "marker"
    h.case_script(
        tmp_path, "p.sh",
        f'check) ( sleep 2; echo done > "{marker}" ) & wait ;;',
    )
    started = time.monotonic()
    code, out, err = h.run_action("p", provider, h.FakeExecutor(), "check", tmp_path, timeout=1)
    elapsed = time.monotonic() - started
    assert code == 124
    assert elapsed < 1.9, f"run_action returned after {elapsed:.1f}s, not at the timeout"
    time.sleep(2.5)
    assert not marker.exists(), "the backgrounded grandchild outlived the timeout"


# ---------------------------------------------------------------------------
# run_action — a script that cannot be exec'd
# ---------------------------------------------------------------------------

def test_run_action_on_a_dot_sh_script_ignores_the_executable_bit(tmp_path):
    """`.sh` runs as `sh <path>` — an argument to sh, not an exec target — so
    a missing +x has no effect at all, unlike every other script kind."""
    provider = h.make_provider("p")
    script = h.case_script(tmp_path, "p.sh", "check) exit 0 ;;")
    script.chmod(0o644)
    code, out, err = h.run_action("p", provider, h.FakeExecutor(), "check", tmp_path)
    assert (code, err) == (0, "")


def test_run_action_on_a_non_sh_script_without_exec_bit_is_an_oserror_reported_as_124(tmp_path):
    provider = h.make_provider("p", script="p")
    script = h.write_script(tmp_path, "p", "#!/bin/sh\nexit 0\n")
    script.chmod(0o644)
    code, out, err = h.run_action("p", provider, h.FakeExecutor(), "check", tmp_path)
    assert code == 124
    assert "not executable" in err
    assert "chmod +x" in err


def test_run_action_on_an_executable_non_sh_script_with_no_shebang_is_enoexec(tmp_path):
    provider = h.make_provider("p", script="p")
    script = h.write_script(tmp_path, "p", "#!/bin/sh\necho x\n")
    # overwrite without a shebang line, still executable
    script.write_text("echo x\n")
    script.chmod(0o755)
    code, out, err = h.run_action("p", provider, h.FakeExecutor(), "check", tmp_path)
    assert code == 124
    assert "cannot tell how to run it" in err
    assert "shebang" in err


def test_run_action_skips_a_directory_with_the_scripts_name(tmp_path):
    """`find_script` filters candidates with `.is_file()`, so a directory that
    happens to share the script's name is treated as "not found", not as an
    exec attempt that would raise IsADirectoryError."""
    provider = h.make_provider("p")
    (tmp_path / "providers").mkdir()
    (tmp_path / "providers" / "p.sh").mkdir()
    code, out, err = h.run_action("p", provider, h.FakeExecutor(), "check", tmp_path)
    assert code == 127
    assert err == "no script for provider 'p'"


def test_run_action_skips_a_dangling_symlink(tmp_path):
    provider = h.make_provider("p")
    d = tmp_path / "providers"
    d.mkdir()
    (d / "p.sh").symlink_to(d / "does-not-exist")
    code, out, err = h.run_action("p", provider, h.FakeExecutor(), "check", tmp_path)
    assert code == 127
    assert err == "no script for provider 'p'"


# ---------------------------------------------------------------------------
# exec_action
# ---------------------------------------------------------------------------

def test_exec_action_returns_none_when_no_script_resolves(tmp_path):
    provider = h.make_provider("nope")
    assert h.exec_action("nope", provider, h.FakeExecutor(), "login", tmp_path) is None


def test_exec_action_builds_argv_for_a_dot_sh_script(tmp_path):
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", "login) exit 0 ;;")
    argv, env = h.exec_action("p", provider, h.FakeExecutor(), "login", tmp_path)
    assert argv == ["sh", str(tmp_path / "providers" / "p.sh"), "login"]
    assert env["MULTIAGENTS_PROVIDER"] == "p"


def test_exec_action_builds_argv_for_a_non_sh_script_as_running_itself(tmp_path):
    provider = h.make_provider("p", script="p.py")
    script = h.write_script(tmp_path, "p.py", "#!/usr/bin/env python3\nprint('hi')\n")
    argv, env = h.exec_action("p", provider, h.FakeExecutor(), "login", tmp_path)
    assert argv == [str(script), "login"]


def test_exec_action_never_runs_the_script_it_describes(tmp_path):
    provider = h.make_provider("p")
    marker = tmp_path / "ran"
    h.case_script(tmp_path, "p.sh", f'login) echo ran > "{marker}"; exit 0 ;;')
    argv, env = h.exec_action("p", provider, h.FakeExecutor(), "login", tmp_path)
    assert not marker.exists()


# ---------------------------------------------------------------------------
# A provider whose script identity and `provider_name` argument diverge
# ---------------------------------------------------------------------------

def test_run_action_uses_provider_dot_script_name_not_the_provider_name_argument(tmp_path):
    """Script resolution keys off `provider.script_name` (derived from the
    `Provider` object's OWN `name`/`script` fields), never off the
    `provider_name` string a caller happens to pass as the first argument to
    `run_action`/`resolve`. Here the two are made to disagree, and the script
    tied to `provider.name` is the one that runs."""
    provider = h.make_provider("realname")
    h.case_script(tmp_path, "realname.sh", "check) echo ran; exit 0 ;;")
    code, out, err = h.run_action("calledas", provider, h.FakeExecutor(), "check", tmp_path)
    assert (code, out) == (0, "ran\n")


def test_build_env_provider_identity_env_var_comes_from_the_argument_not_provider_dot_name(tmp_path):
    """F113 — companion to the test above. The script that runs is chosen by
    `provider.name`/`provider.script_name`; the identity that script is TOLD
    it is running as (`MULTIAGENTS_PROVIDER`) comes from the separate
    `provider_name` argument. When a caller passes a `provider_name` that
    does not match `provider.name`, the running script sees an environment
    that names a different provider than the one whose script actually
    executed it."""
    provider = h.make_provider("realname")
    env = h.build_env("calledas", provider, h.FakeExecutor())
    assert env["MULTIAGENTS_PROVIDER"] == "calledas"
    assert provider.name == "realname"
    assert env["MULTIAGENTS_PROVIDER"] != provider.name


# ---------------------------------------------------------------------------
# build_env — base keys, every kind
# ---------------------------------------------------------------------------

def test_build_env_sets_the_base_keys_for_local_kind(tmp_path, monkeypatch):
    docker_only_keys = ("MULTIAGENTS_CONTAINER", "MULTIAGENTS_PRIVATE_HOME",
                        "MULTIAGENTS_PRIVATE_BACKING", "MULTIAGENTS_PRIVATE_VAULT",
                        "MULTIAGENTS_AUTH_PROXY")
    # build_env copies the ambient environment verbatim (F112), so on a host
    # that already has one of these set, its mere presence — not anything
    # build_env did — would make the "absent for local kind" assertion below
    # pass or fail by accident. Clear them first so the absence is a fact
    # about build_env, not about the machine running the test.
    for key in docker_only_keys:
        monkeypatch.delenv(key, raising=False)
    provider = h.make_provider("p")
    env = h.build_env("p", provider, h.FakeExecutor(kind="local"))
    assert env["MULTIAGENTS_PROVIDER"] == "p"
    assert env["MULTIAGENTS_EXECUTOR"] == "local"
    assert env["MULTIAGENTS_UID"] == str(os.getuid())
    assert env["MULTIAGENTS_GID"] == str(os.getgid())
    for docker_only in docker_only_keys:
        assert docker_only not in env


def test_build_env_docker_keys_only_appear_for_docker_kind(tmp_path):
    provider = h.make_provider("p")
    executor = h.FakeExecutor(
        kind="docker", container="cty",
        private={"/c/path": "/h/path"}, vault={"/c/vault": "/h/vault"},
        auth_proxy=True,
    )
    env = h.build_env("p", provider, executor)
    assert env["MULTIAGENTS_CONTAINER"] == "cty"
    assert env["MULTIAGENTS_PRIVATE_HOME"] == "/c/path"
    assert env["MULTIAGENTS_PRIVATE_BACKING"] == "/h/path"
    assert env["MULTIAGENTS_PRIVATE_VAULT"] == "/h/vault"
    assert env["MULTIAGENTS_AUTH_PROXY"] == "1"


def test_build_env_auth_proxy_key_is_absent_not_zero_when_disabled(tmp_path, monkeypatch):
    # As above: clear the ambient value first so the absence pins build_env's
    # behaviour rather than whatever this process happened to inherit.
    monkeypatch.delenv("MULTIAGENTS_AUTH_PROXY", raising=False)
    provider = h.make_provider("p")
    executor = h.FakeExecutor(kind="docker", container="cty", auth_proxy=False)
    env = h.build_env("p", provider, executor)
    assert "MULTIAGENTS_AUTH_PROXY" not in env


def test_build_env_private_state_with_multiple_entries_uses_only_the_first_inserted(tmp_path):
    """`build_env` breaks after the first `dict.items()` pair. Python dicts
    preserve insertion order, so this is whichever entry the executor happened
    to put first, not one selected by matching key/value to the provider."""
    provider = h.make_provider("p")
    executor = h.FakeExecutor(
        kind="docker", container="c",
        private={"/first": "/host-first", "/second": "/host-second"},
    )
    env = h.build_env("p", provider, executor)
    assert env["MULTIAGENTS_PRIVATE_HOME"] == "/first"
    assert env["MULTIAGENTS_PRIVATE_BACKING"] == "/host-first"


def test_build_env_vault_state_with_multiple_entries_uses_the_first_value_not_key(tmp_path):
    """Asymmetric with the private-home case above: for vault, `build_env`
    takes `next(iter(vault.values()))` — the first VALUE — while for private
    it takes both the key and the value of the first pair."""
    provider = h.make_provider("p")
    executor = h.FakeExecutor(
        kind="docker", container="c",
        vault={"/container/vault/key": "/host/vault/value"},
    )
    env = h.build_env("p", provider, executor)
    assert env["MULTIAGENTS_PRIVATE_VAULT"] == "/host/vault/value"


def test_build_env_private_state_typeerror_falls_back_to_no_arg_call(tmp_path):
    """`build_env` calls `executor.private_state(provider_name)` inside a
    `try/except TypeError`, falling back to `executor.private_state()` for
    "an executor from before the filter" — an executor whose `private_state`
    takes no arguments at all."""
    class NoArgExecutor:
        kind = "docker"
        container = "c"

        def private_state(self):
            return {"/legacy/path": "/legacy/host"}

        def vault_state(self, name=""):
            return {}

        def auth_proxy_enabled(self):
            return False

    provider = h.make_provider("p")
    env = h.build_env("p", provider, NoArgExecutor())
    assert env["MULTIAGENTS_PRIVATE_HOME"] == "/legacy/path"
    assert env["MULTIAGENTS_PRIVATE_BACKING"] == "/legacy/host"


# ---------------------------------------------------------------------------
# build_env — provider.env, precedence, and expansion
# ---------------------------------------------------------------------------

def _extra_keys(env):
    base_keys = {"MULTIAGENTS_PROVIDER", "MULTIAGENTS_BIN", "MULTIAGENTS_EXECUTOR",
                 "MULTIAGENTS_UID", "MULTIAGENTS_GID"}
    return set(env) - set(os.environ) - base_keys


def test_build_env_with_no_env_block_and_a_found_binary_adds_nothing_beyond_ambient_and_base_keys(tmp_path):
    provider = h.make_provider("p", bin="sh")
    env = h.build_env("p", provider, h.FakeExecutor())
    # BIN_ERROR may be absent or empty when found; nothing else may appear.
    assert _extra_keys(env) - {"MULTIAGENTS_BIN_ERROR"} == set()
    assert env.get("MULTIAGENTS_BIN_ERROR", "") == ""


def test_build_env_with_no_env_block_and_a_missing_binary_adds_only_bin_error(tmp_path):
    """H7 PS-R2: a missing binary is reported via MULTIAGENTS_BIN_ERROR."""
    provider = h.make_provider("p", bin="definitely-not-a-real-binary-c2-seam-test")
    env = h.build_env("p", provider, h.FakeExecutor())
    assert _extra_keys(env) == {"MULTIAGENTS_BIN_ERROR"}


def test_build_env_providers_env_block_is_applied_and_can_override_protocol_keys(tmp_path):
    """F112 (context) — a provider's own `env:` block is applied AFTER the
    `MULTIAGENTS_*` keys `build_env` itself computed, with no protection: a
    provider config can overwrite `MULTIAGENTS_PROVIDER` (or any other
    protocol variable) for every action run against it, not just the ones it
    plausibly needs to change."""
    provider = h.make_provider("p", env={"MULTIAGENTS_PROVIDER": "hijacked", "CUSTOM": "1"})
    env = h.build_env("p", provider, h.FakeExecutor())
    assert env["MULTIAGENTS_PROVIDER"] == "hijacked"
    assert env["CUSTOM"] == "1"


def test_build_env_expands_provider_env_vars_and_user_against_the_real_process_environment(
    tmp_path, monkeypatch,
):
    """F115 — `provider.env` values go through
    `os.path.expanduser(os.path.expandvars(value))`, and `expandvars` reads
    the REAL `os.environ` at call time — not the `env` dict `build_env` is in
    the middle of assembling. A provider's `env:` block can reach a variable
    that was already on the host process's environment before `build_env`
    ran, but NOT one of the `MULTIAGENTS_*` values `build_env` just computed
    for this very call: those look like they ought to be interpolatable and
    silently are not — the reference is left as a literal, unexpanded
    string."""
    monkeypatch.setenv("MULTIAGENTS_SEAM_TEST_HOST_VAR", "from-host-environ")
    # If the ambient process already had MULTIAGENTS_PROVIDER set (e.g. from
    # a real orchestration run on this host), `expandvars` would resolve
    # $MULTIAGENTS_PROVIDER from THAT value instead of leaving it literal,
    # which is a fact about the host, not about build_env. Clear it so the
    # "left as a literal string" assertion below pins build_env's behaviour.
    monkeypatch.delenv("MULTIAGENTS_PROVIDER", raising=False)
    provider = h.make_provider(
        "p",
        env={
            "FROM_HOST": "$MULTIAGENTS_SEAM_TEST_HOST_VAR",
            "FROM_JUST_COMPUTED": "$MULTIAGENTS_PROVIDER",
            "HOME_EXPANDED": "~/subdir",
        },
    )
    env = h.build_env("p", provider, h.FakeExecutor())
    assert env["FROM_HOST"] == "from-host-environ"
    assert env["FROM_JUST_COMPUTED"] == "$MULTIAGENTS_PROVIDER"  # NOT "p" — left literal
    assert env["HOME_EXPANDED"] == os.path.expanduser("~/subdir")


def test_build_env_extra_overrides_everything_including_providers_env_block(tmp_path):
    provider = h.make_provider("p", env={"X": "from-env-block"})
    env = h.build_env("p", provider, h.FakeExecutor(), extra={"X": "from-extra"})
    assert env["X"] == "from-extra"


def test_ev_r1_build_env_does_not_copy_the_full_ambient_process_environment(tmp_path, monkeypatch):
    """EV-R1 (F112, inverted from the characterization that pinned
    `dict(os.environ)`): an ambient variable that is neither allowlisted nor
    one of the keys `build_env` computes never reaches a provider script."""
    monkeypatch.setenv("MULTIAGENTS_SEAM_TEST_AMBIENT_SECRET", "should-not-leak")
    monkeypatch.setenv("SEAM_TEST_AMBIENT_API_TOKEN", "should-not-leak")
    provider = h.make_provider("p")
    env = h.build_env("p", provider, h.FakeExecutor())
    assert "MULTIAGENTS_SEAM_TEST_AMBIENT_SECRET" not in env
    assert "SEAM_TEST_AMBIENT_API_TOKEN" not in env


# ---------------------------------------------------------------------------
# build_env — MULTIAGENTS_BIN via provider.available() / shutil.which
# ---------------------------------------------------------------------------

def test_build_env_bin_is_the_absolute_launcher_path_when_the_binary_is_on_path(tmp_path):
    """H7 PS-R2/PS-R2a: `build_env` resolves the binary live against the real
    PATH and hands the scripts the launcher path: absolute, symlinks not
    resolved (`sh` stays `.../sh` even where it links to dash)."""
    provider = h.make_provider("p", bin="sh")
    env = h.build_env("p", provider, h.FakeExecutor())
    assert env["MULTIAGENTS_BIN"] != "sh"
    assert os.path.isabs(env["MULTIAGENTS_BIN"])
    assert env["MULTIAGENTS_BIN"].endswith("/sh")
    assert env.get("MULTIAGENTS_BIN_ERROR", "") == ""


def test_build_env_bin_is_empty_with_an_error_when_the_binary_is_not_found(tmp_path):
    """H7 PS-R2: no fallback to the configured name any more."""
    provider = h.make_provider("p", bin="definitely-not-a-real-binary-c2-seam-test")
    env = h.build_env("p", provider, h.FakeExecutor())
    assert env["MULTIAGENTS_BIN"] == ""
    assert env["MULTIAGENTS_BIN_ERROR"]


# ---------------------------------------------------------------------------
# resolve / find_script — precedence
# ---------------------------------------------------------------------------

def _put_script(base: Path, layer: str, marker: str):
    d = base / layer
    d.mkdir(parents=True, exist_ok=True)
    f = d / "p.sh"
    f.write_text(f'#!/bin/sh\ncase "$1" in\ncheck) echo {marker}; exit 0 ;;\nesac\n')
    f.chmod(0o755)
    return f


def test_resolve_precedence_config_providers_beats_config_auth(tmp_path):
    provider = h.make_provider("p")
    config_dir = tmp_path / "config"
    _put_script(config_dir, "auth", "from-auth")
    _put_script(config_dir, "providers", "from-providers")
    resolved = h.resolve("p", provider, config_dir, None)
    assert resolved == config_dir / "providers" / "p.sh"


def test_resolve_precedence_project_layer_beats_config_layer_entirely(tmp_path):
    """Even `project_config/auth` (the legacy dir, lower-priority WITHIN its
    own layer) beats `config_dir/providers` — the layers are compared as a
    whole before `providers/` vs `auth/` is considered within one."""
    provider = h.make_provider("p")
    config_dir = tmp_path / "config"
    project_dir = tmp_path / "project"
    _put_script(config_dir, "providers", "from-config-providers")
    _put_script(project_dir, "auth", "from-project-auth")
    resolved = h.resolve("p", provider, config_dir, project_dir)
    assert resolved == project_dir / "auth" / "p.sh"


def test_resolve_precedence_project_providers_beats_project_auth(tmp_path):
    provider = h.make_provider("p")
    config_dir = tmp_path / "config"
    project_dir = tmp_path / "project"
    _put_script(project_dir, "auth", "from-project-auth")
    _put_script(project_dir, "providers", "from-project-providers")
    resolved = h.resolve("p", provider, config_dir, project_dir)
    assert resolved == project_dir / "providers" / "p.sh"


def test_resolve_falls_back_to_the_shipped_script_when_nothing_else_matches(tmp_path):
    providers = h.shipped_providers()
    claude = providers["claude"]
    resolved = h.resolve("claude", claude, tmp_path, None)
    assert resolved == h.shipped_script_path("claude")


def test_resolve_tolerates_a_project_config_directory_that_does_not_exist_on_disk(tmp_path):
    providers = h.shipped_providers()
    claude = providers["claude"]
    resolved = h.resolve("claude", claude, tmp_path, tmp_path / "no-such-project")
    assert resolved == h.shipped_script_path("claude")


def test_resolve_uses_the_providers_custom_script_name_verbatim(tmp_path):
    """`provider.script_name` is `provider.script` when set, with no implicit
    `.sh` suffix added — a provider can name a non-shell script."""
    provider = h.make_provider("p", script="totally-different-name.py")
    h.write_script(tmp_path, "totally-different-name.py", "#!/usr/bin/env python3\n")
    resolved = h.resolve("p", provider, tmp_path, None)
    assert resolved == tmp_path / "providers" / "totally-different-name.py"


def test_find_script_returns_none_for_an_unknown_name(tmp_path):
    assert h.find_script("nothing-here.sh", tmp_path, None) is None


# ---------------------------------------------------------------------------
# script_argv
# ---------------------------------------------------------------------------

def test_script_argv_dot_sh_runs_under_sh(tmp_path):
    script = h.write_script(tmp_path, "p.sh", "#!/bin/sh\n")
    assert h.script_argv(script) == ["sh", str(script)]


def test_script_argv_non_dot_sh_runs_itself(tmp_path):
    script = h.write_script(tmp_path, "p.py", "#!/usr/bin/env python3\n")
    assert h.script_argv(script) == [str(script)]


def test_script_argv_extensionless_name_also_runs_itself(tmp_path):
    script = h.write_script(tmp_path, "p", "#!/bin/sh\n")
    assert h.script_argv(script) == [str(script)]
