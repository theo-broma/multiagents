"""Characterization tests for C1 — executor selection and the agent environment.

Surface: `get_executor` / `executor_for`, `DockerExecutor` and `LocalExecutor`
argument construction, `build_env`, `prepare_home`. Pins what the code
currently does, including the parts that look wrong — those are flagged as
`F<n>` findings in `context/review/C1-sandbox-executor.md`, not "fixed" here.

`docker` is not on PATH in this sandbox, so anything that shells out
(`ensure_running`, `ensure_proxy`, `ensure_auth_proxy`, ...) is unreachable and
guarded with `h.docker_available()`. Argument construction (`run_args`,
`mounts`) does no I/O and is fully exercised.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c1_harness as h  # noqa: E402


# ---------------------------------------------------------------------------
# get_executor / executor_for
# ---------------------------------------------------------------------------

def test_get_executor_local_ignores_config_paths_and_providers():
    ex = h.get_executor("local", config={"image": "whatever"}, paths="not-a-paths-object",
                        providers={"claude": object()}, config_dir=Path("/nonexistent"))
    assert isinstance(ex, h.LocalExecutor)
    assert ex.kind == "local"


def test_get_executor_docker_builds_with_config_and_paths(tmp_path):
    paths = h.ProjectPaths(tmp_path)
    ex = h.get_executor("docker", config={"image": "custom:tag"}, paths=paths,
                        providers={"claude": object()}, config_dir=tmp_path / "cfg")
    assert isinstance(ex, h.DockerExecutor)
    assert ex.image == "custom:tag"
    assert ex.paths is paths
    assert ex.config_dir == tmp_path / "cfg"


def test_get_executor_docker_defaults_config_and_providers_when_none(tmp_path):
    # get_executor passes `config or {}` and `providers` straight through
    # (no `or {}` on providers) — DockerExecutor.__init__ is what defaults
    # providers to {}, not get_executor itself.
    ex = h.get_executor("docker", config=None, paths=h.ProjectPaths(tmp_path), providers=None)
    assert ex.config == {}
    assert ex.providers == {}


@pytest.mark.parametrize("kind", ["bogus", "", "LOCAL", "Docker"])
def test_get_executor_unknown_kind_raises_value_error(kind):
    with pytest.raises(ValueError, match="Unknown executor kind"):
        h.get_executor(kind)


def test_get_executor_none_kind_raises_value_error_not_typeerror():
    # `kind == "local"` / `kind == "docker"` are plain equality checks, so a
    # non-string `kind` falls through to the same ValueError rather than
    # blowing up on e.g. a `.lower()` call that isn't there.
    with pytest.raises(ValueError, match=r"Unknown executor kind None"):
        h.get_executor(None)


def test_executor_for_picks_per_agent_executor_pin_over_project_default(tmp_path):
    paths = h.ProjectPaths(tmp_path)
    config = SimpleNamespace(
        executor="docker",
        agents={
            "reviewer": SimpleNamespace(provider="claude", executor="local"),
            "writer": SimpleNamespace(provider="claude", executor=""),
        },
        project={},
    )
    build = h.executor_for(paths, config, providers={})
    # The pinned agent's provider gets "local" even though config.executor
    # is "docker" — the first matching spec whose `.executor` is truthy wins.
    assert isinstance(build("claude"), h.LocalExecutor)


def test_executor_for_falls_back_to_project_default_when_no_pin(tmp_path):
    paths = h.ProjectPaths(tmp_path)
    config = SimpleNamespace(
        executor="local",
        agents={"writer": SimpleNamespace(provider="claude", executor="")},
        project={},
    )
    build = h.executor_for(paths, config, providers={})
    assert isinstance(build("claude"), h.LocalExecutor)


def test_executor_for_ignores_pin_on_a_different_provider(tmp_path):
    paths = h.ProjectPaths(tmp_path)
    config = SimpleNamespace(
        executor="local",
        agents={"other": SimpleNamespace(provider="opencode", executor="docker")},
        project={},
    )
    build = h.executor_for(paths, config, providers={})
    # "opencode"'s pin to docker does not leak into "claude"'s build.
    assert isinstance(build("claude"), h.LocalExecutor)


# ---------------------------------------------------------------------------
# DockerExecutor — naming properties
# ---------------------------------------------------------------------------

def test_network_property_is_the_docker_network_name(tmp_path):
    ex = h.make_docker_executor(tmp_path)
    assert ex.network == f"multiagents-net-{ex.slug}"


def test_network_mode_property_reads_the_network_config_key_not_the_network_name(tmp_path):
    # `.network` (the docker network NAME) and `.network_mode` (the
    # allowlist/bridge/none policy) are two different properties that both
    # derive from the same config key literally named "network". Easy to
    # confuse; pinned explicitly.
    ex = h.make_docker_executor(tmp_path, network="bridge")
    assert ex.network_mode == "bridge"
    assert ex.network == f"multiagents-net-{ex.slug}"       # unaffected
    assert ex.network != ex.network_mode


def test_network_mode_defaults_to_allowlist_when_key_absent(tmp_path):
    ex = h.DockerExecutor({"image": "img"}, h.ProjectPaths(tmp_path), {}, tmp_path)
    assert ex.network_mode == "allowlist"


def test_slug_and_container_name_default_when_paths_is_none():
    ex = h.DockerExecutor({"image": "img"}, None, {}, None)
    assert ex.slug == "default"
    assert ex.container == "multiagents-default"


def test_container_name_config_override_wins_over_slug(tmp_path):
    ex = h.make_docker_executor(tmp_path, container_name="pinned-name")
    assert ex.container == "pinned-name"


# ---------------------------------------------------------------------------
# DockerExecutor.mounts() — construction from extra_mounts, dedup, ordering
# ---------------------------------------------------------------------------

def test_mounts_with_no_extra_config_and_nonexistent_worktrees_homes_is_root_only(tmp_path):
    # worktrees/homes live under the machine-wide state root, not under the
    # project root, and are not created just by constructing ProjectPaths.
    # mounts()'s existence guard (`path.exists()`) silently drops them until
    # something else has created them — only the project root's own mounts
    # survive here. Since SG-R2 those are the root READ-ONLY, with
    # `.multiagents` runtime state reopened writable and its config closed
    # again; they are listed whether or not they exist yet, because
    # `protect_project` creates them before a container starts. tmp_path has
    # no `.git`, so nothing under `.git` is listed.
    ex = h.make_docker_executor(tmp_path)
    paths = ex.paths
    result = ex.mounts()
    dests = {p for p, _ in result}
    assert dests == {paths.root, paths.data, paths.config}
    assert dict(result) == {paths.root: True, paths.data: False, paths.config: True}


def test_mounts_includes_worktrees_and_homes_once_they_exist(tmp_path):
    ex = h.make_docker_executor(tmp_path)
    paths = ex.paths
    paths.worktrees.mkdir(parents=True, exist_ok=True)
    paths.homes.mkdir(parents=True, exist_ok=True)
    dests = {p for p, ro in ex.mounts()}
    assert dests == {paths.root, paths.data, paths.config, paths.worktrees, paths.homes}
    assert dict(ex.mounts())[paths.root] is True        # SG-R2: root read-only


def test_mounts_returns_empty_list_when_paths_is_none():
    ex = h.DockerExecutor({"image": "img"}, None, {}, None)
    assert ex.mounts() == []


def test_extra_mounts_dict_form_read_only_true_is_honoured(tmp_path):
    extra = tmp_path / "extra-ro"
    extra.mkdir()
    ex = h.make_docker_executor(tmp_path, extra_mounts=[{"path": str(extra), "read_only": True}])
    result = dict(ex.mounts())
    assert result[extra] is True


def test_extra_mounts_string_form_can_never_be_read_only(tmp_path):
    # Only the dict form can request read_only; a bare string always becomes
    # (Path(entry).expanduser(), False) regardless of any expectation the
    # config author may have had that it inherits or defaults to read-only.
    extra = tmp_path / "extra-str"
    extra.mkdir()
    ex = h.make_docker_executor(tmp_path, extra_mounts=[str(extra)])
    result = dict(ex.mounts())
    assert result[extra] is False


def test_extra_mounts_relative_path_is_not_resolved_to_absolute(tmp_path, monkeypatch):
    # Path(entry).expanduser() only expands "~" — it does not call .resolve(),
    # so a relative extra_mounts entry stays relative all the way through
    # mounts() (and, per test below, into the run_args() argv string).
    monkeypatch.chdir(tmp_path)
    Path("relative-dir").mkdir()
    ex = h.make_docker_executor(tmp_path, extra_mounts=["relative-dir"])
    result = dict(ex.mounts())
    assert Path("relative-dir") in result
    assert not Path("relative-dir").is_absolute()


def test_extra_mounts_nonexistent_path_is_silently_dropped(tmp_path):
    missing = tmp_path / "does-not-exist"
    ex = h.make_docker_executor(tmp_path, extra_mounts=[str(missing)])
    dests = {p for p, _ in ex.mounts()}
    assert missing not in dests


def test_extra_mounts_path_with_colon_and_spaces_passes_through_unvalidated(tmp_path):
    # No validation anywhere in mounts() or run_args(). A path containing ':'
    # is ambiguous in docker's `-v source:dest[:ro]` syntax; multiagents does
    # not guard against it, it is simply passed straight through.
    weird = tmp_path / "weird dir:with-colon"
    weird.mkdir()
    ex = h.make_docker_executor(tmp_path, extra_mounts=[str(weird)])
    result = dict(ex.mounts())
    assert weird in result
    argv = ex.run_args()
    joined = " ".join(argv)
    assert f"{weird}:{weird}" in joined


def test_extra_mounts_read_only_is_silently_dropped_when_path_collides_with_root(tmp_path):
    # WIDEN-THE-BOUNDARY finding (see F30 in C1-sandbox-executor.md): the
    # built-in project mounts come BEFORE extra_mounts is appended, and the
    # dedup loop's `path not in seen` guard means the FIRST occurrence of a
    # path wins. Since SG-R2 the built-in root mount is read-only, so the
    # collision is tested in the direction that still matters: a config
    # author who names paths.root in extra_mounts WITHOUT read_only (a bare
    # string, always writable) does not get a writable root — the built-in
    # read-only mount wins and the extra entry is dropped.
    ex = h.make_docker_executor(
        tmp_path,
        extra_mounts=[str(tmp_path), {"path": str(tmp_path), "read_only": False}],
    )
    result = ex.mounts()
    assert dict(result)[ex.paths.root] is True        # NOT False, despite the request
    assert [p for p, _ in result].count(ex.paths.root) == 1
    assert ex.paths.root == tmp_path


def test_duplicate_extra_mounts_entries_first_one_wins(tmp_path):
    dup = tmp_path / "dup"
    dup.mkdir()
    ex = h.make_docker_executor(
        tmp_path,
        extra_mounts=[
            {"path": str(dup), "read_only": False},
            {"path": str(dup), "read_only": True},
        ],
    )
    result = dict(ex.mounts())
    assert result[dup] is False       # first entry's read_only wins, second dropped


def test_extra_mounts_wins_over_a_colliding_cli_binary_mount_because_it_comes_first_in_out(tmp_path):
    # `out` is built in this order: root/worktrees/homes, then extra_mounts,
    # then (if mount_cli_from_host) each provider's CLI-binary mount and
    # home_links. First-wins dedup means extra_mounts is checked BEFORE the
    # CLI/home_links entries are even appended — so an extra_mounts entry
    # naming the exact same absolute path as a provider's CLI binary wins
    # over that binary's normal read-only mount, producing a writable mount
    # at the path the sandboxed CLI binary lives at. This is the same
    # first-wins mechanism as F30, applied to a built-in CLI mount instead of
    # the project root.
    binary_dir = tmp_path / "fakebin"
    binary_dir.mkdir()
    binary_path = binary_dir / "fake-cli"
    binary_path.write_text("#!/bin/sh\n")
    binary_path.chmod(0o755)

    provider = SimpleNamespace(
        available=lambda: str(binary_path),
        container_private_home=[],
        home_links=[],
    )
    ex = h.make_docker_executor(
        tmp_path,
        providers={"fake": provider},
        extra_mounts=[{"path": str(binary_path), "read_only": False}],
    )
    result = dict(ex.mounts())
    assert result[binary_path] is False        # extra_mounts' writable entry won


def test_config_dir_is_mounted_read_only_after_root_and_wins_at_its_own_path(tmp_path):
    ex = h.make_docker_executor(tmp_path)
    ex.paths.config.mkdir(parents=True, exist_ok=True)
    result = ex.mounts()
    # All three exist: the read-only root (SG-R2), the writable
    # `.multiagents` data dir reopened below it, and the narrower read-only
    # config dir mounted at its own (different, nested) destination path.
    as_dict = dict(result)
    assert as_dict[ex.paths.root] is True
    assert as_dict[ex.paths.data] is False
    assert as_dict[ex.paths.config] is True
    # config comes after both the root and the writable data dir that
    # encloses it in the returned list.
    dests = [p for p, _ in result]
    assert result.index((ex.paths.config, True)) > dests.index(ex.paths.root)
    assert result.index((ex.paths.config, True)) > dests.index(ex.paths.data)


# ---------------------------------------------------------------------------
# DockerExecutor.run_args() — argv construction
# ---------------------------------------------------------------------------

def test_run_args_always_emits_init_flag(tmp_path):
    # Pinning a specific, ticket-driving fact: `--init` IS present in
    # run_args()'s argv, for any config exercised here. This changed because
    # of bug-cfdc71 — the container had no PID 1 reaper, so fork-heavy work
    # left zombies until the 512-pid limit was exhausted. Not a judgement on
    # whether it should — just what the code does today.
    for config in ({}, {"network": "bridge"}, {"network": "none"},
                   {"cpus": 2, "memory": "4g", "pids_limit": 100}):
        ex = h.make_docker_executor(tmp_path, **config)
        assert "--init" in ex.run_args()


def test_run_args_network_none_uses_docker_network_none(tmp_path):
    ex = h.make_docker_executor(tmp_path, network="none")
    argv = ex.run_args()
    i = argv.index("--network")
    assert argv[i + 1] == "none"


def test_run_args_network_allowlist_uses_the_project_network_and_sets_proxy_env(tmp_path):
    ex = h.make_docker_executor(tmp_path, network="allowlist")
    argv = ex.run_args()
    i = argv.index("--network")
    assert argv[i + 1] == ex.network
    joined = " ".join(argv)
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        assert f"--env {var}=http://{ex.proxy_container}:8888" in joined
    assert "--env NO_PROXY=localhost,127.0.0.1" in joined


def test_run_args_bridge_network_omits_network_flag_entirely(tmp_path):
    # network_mode "bridge" matches neither the "none" nor "allowlist"
    # branch, so no --network flag is added at all (docker's own default
    # network applies).
    ex = h.make_docker_executor(tmp_path, network="bridge")
    argv = ex.run_args()
    assert "--network" not in argv


def test_run_args_pids_limit_zero_is_silently_omitted(tmp_path):
    # `if value:` is a truthiness check, not `is not None` — pids_limit=0
    # (a plausible "no limit" or "limit to zero" config value) never reaches
    # the argv at all. Same for cpus=0 / memory=0.
    ex = h.make_docker_executor(tmp_path, pids_limit=0, cpus=0, memory=0)
    argv = ex.run_args()
    assert "--pids-limit" not in argv
    assert "--cpus" not in argv
    assert "--memory" not in argv


def test_run_args_pids_limit_negative_is_passed_through_unvalidated(tmp_path):
    # A negative value IS truthy in Python, so it passes the same `if value:`
    # check that zero fails, and is forwarded to docker with no validation.
    ex = h.make_docker_executor(tmp_path, pids_limit=-1)
    argv = ex.run_args()
    i = argv.index("--pids-limit")
    assert argv[i + 1] == "-1"


def test_run_args_pids_limit_large_value_passed_through_as_str(tmp_path):
    ex = h.make_docker_executor(tmp_path, pids_limit=10_000_000)
    argv = ex.run_args()
    i = argv.index("--pids-limit")
    assert argv[i + 1] == "10000000"


def test_run_args_cpus_and_memory_present_when_truthy(tmp_path):
    ex = h.make_docker_executor(tmp_path, cpus=2.5, memory="4g")
    argv = ex.run_args()
    assert argv[argv.index("--cpus") + 1] == "2.5"
    assert argv[argv.index("--memory") + 1] == "4g"


def test_run_args_mount_flags_use_dash_v_source_colon_dest_ro_suffix(tmp_path):
    ex = h.make_docker_executor(tmp_path)
    argv = ex.run_args()
    root = ex.paths.root
    idx = argv.index("-v")
    # find the -v flag for root specifically
    v_values = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    root_entries = [v for v in v_values if v.startswith(f"{root}:")]
    assert len(root_entries) == 1
    assert root_entries[0] == f"{root}:{root}:ro"  # read-only (SG-R2): ":ro" suffix
    # A writable mount carries no suffix at all: the reopened data dir.
    data = ex.paths.data
    data_entries = [v for v in v_values if v.startswith(f"{data}:")]
    assert data_entries == [f"{data}:{data}"]
    del idx


def test_run_args_readonly_mount_gets_ro_suffix(tmp_path):
    extra = tmp_path / "ro-mount"
    extra.mkdir()
    ex = h.make_docker_executor(tmp_path, extra_mounts=[{"path": str(extra), "read_only": True}])
    argv = ex.run_args()
    v_values = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert f"{extra}:{extra}:ro" in v_values


def test_run_args_auth_proxy_disabled_by_default_no_anthropic_base_url(tmp_path):
    ex = h.make_docker_executor(tmp_path)
    assert "ANTHROPIC_BASE_URL" not in " ".join(ex.run_args())


def test_run_args_ends_with_image_and_sleep_infinity(tmp_path):
    ex = h.make_docker_executor(tmp_path, image="my-image:tag")
    argv = ex.run_args()
    assert argv[-3:] == ["my-image:tag", "sleep", "infinity"]


def test_run_args_user_flag_is_invoking_uid_gid(tmp_path):
    ex = h.make_docker_executor(tmp_path)
    argv = ex.run_args()
    i = argv.index("--user")
    assert argv[i + 1] == f"{os.getuid()}:{os.getgid()}"


# ---------------------------------------------------------------------------
# build_env
# ---------------------------------------------------------------------------

def test_build_env_base_keys_forwarded_when_present_in_os_environ(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-test")
    monkeypatch.delenv("TMPDIR", raising=False)
    env = h.build_env(passthrough=[], blocked=[], home=None, identity={})
    assert env["TERM"] == "xterm-test"
    assert "TMPDIR" not in env       # unset base key is simply absent


def test_build_env_starts_from_nothing_unlisted_vars_never_appear(monkeypatch):
    monkeypatch.setenv("SOME_RANDOM_SECRET_LOOKING_VAR", "sekret")
    env = h.build_env(passthrough=[], blocked=[], home=None, identity={})
    assert "SOME_RANDOM_SECRET_LOOKING_VAR" not in env


def test_ev_r3_build_env_base_keys_named_in_blocked_are_not_forwarded(monkeypatch):
    # EV-R3 (F33, inverted): blocking a base key such as PATH is honoured.
    monkeypatch.setenv("PATH", "/fake/bin")
    env = h.build_env(passthrough=[], blocked=["PATH"], home=None, identity={})
    assert "PATH" not in env


def test_build_env_passthrough_bare_name_forwards_current_process_value(monkeypatch):
    monkeypatch.setenv("MY_TOOLCHAIN_HOME", "/opt/tool")
    env = h.build_env(passthrough=["MY_TOOLCHAIN_HOME"], blocked=[], home=None, identity={})
    assert env["MY_TOOLCHAIN_HOME"] == "/opt/tool"


def test_build_env_passthrough_key_equals_value_form_sets_a_literal(monkeypatch):
    monkeypatch.delenv("EXPLICIT_KEY", raising=False)
    env = h.build_env(passthrough=["EXPLICIT_KEY=literal-value"], blocked=[], home=None, identity={})
    assert env["EXPLICIT_KEY"] == "literal-value"


def test_build_env_passthrough_value_containing_equals_keeps_the_rest_after_first_split(monkeypatch):
    # `key.partition("=")` splits on the FIRST "=" only, so a value that
    # itself contains "=" is preserved intact after that first split.
    env = h.build_env(passthrough=["FOO=bar=baz"], blocked=[], home=None, identity={})
    assert env["FOO"] == "bar=baz"


def test_build_env_passthrough_unset_variable_is_omitted_entirely(monkeypatch):
    monkeypatch.delenv("DEFINITELY_UNSET_VAR_XYZ", raising=False)
    env = h.build_env(passthrough=["DEFINITELY_UNSET_VAR_XYZ"], blocked=[], home=None, identity={})
    assert "DEFINITELY_UNSET_VAR_XYZ" not in env


def test_build_env_passthrough_empty_string_variable_is_forwarded_as_empty(monkeypatch):
    # Distinguishes "unset" (omitted) from "set to empty string" (forwarded)
    # — both look like "nothing" to a casual reader but behave differently.
    monkeypatch.setenv("EMPTY_VAR", "")
    env = h.build_env(passthrough=["EMPTY_VAR"], blocked=[], home=None, identity={})
    assert env["EMPTY_VAR"] == ""
    assert "EMPTY_VAR" in env


def test_build_env_passthrough_blocked_name_is_skipped_even_with_explicit_value(monkeypatch):
    env = h.build_env(passthrough=["SECRET=leaked"], blocked=["SECRET"], home=None, identity={})
    assert "SECRET" not in env


def test_build_env_passthrough_blocked_check_is_against_stripped_key(monkeypatch):
    # `key.strip()` happens before the `in blocked` check, so leading/
    # trailing whitespace in a passthrough entry's name does not evade the
    # block list.
    env = h.build_env(passthrough=[" SECRET =oops"], blocked=["SECRET"], home=None, identity={})
    assert "SECRET" not in env
    assert " SECRET " not in env


def test_build_env_passthrough_bare_name_with_whitespace_is_stripped_before_lookup(monkeypatch):
    monkeypatch.setenv("SPACED", "value")
    env = h.build_env(passthrough=[" SPACED "], blocked=[], home=None, identity={})
    assert env["SPACED"] == "value"


def test_build_env_home_none_sets_no_home_or_xdg_vars():
    env = h.build_env(passthrough=[], blocked=[], home=None, identity={})
    assert "HOME" not in env
    assert "XDG_CONFIG_HOME" not in env
    assert "XDG_DATA_HOME" not in env
    assert "XDG_CACHE_HOME" not in env


def test_build_env_home_set_populates_home_and_xdg_vars(tmp_path):
    home = tmp_path / "agent-home"
    env = h.build_env(passthrough=[], blocked=[], home=home, identity={})
    assert env["HOME"] == str(home)
    assert env["XDG_CONFIG_HOME"] == str(home / ".config")
    assert env["XDG_DATA_HOME"] == str(home / ".local" / "share")
    assert env["XDG_CACHE_HOME"] == str(home / ".cache")


def test_build_env_identity_overrides_everything_including_blocked_names(monkeypatch):
    # identity is applied last via env.update(identity) — it can override a
    # base key, a passthrough value, or even a name that appears in `blocked`
    # (identity is caller-supplied, not user config, so this is not itself a
    # user-controlled bypass — but it IS the literal precedence order).
    monkeypatch.setenv("TERM", "xterm-real")
    env = h.build_env(passthrough=[], blocked=["TERM"], home=None, identity={"TERM": "identity-wins"})
    assert env["TERM"] == "identity-wins"


# ---------------------------------------------------------------------------
# prepare_home
# ---------------------------------------------------------------------------

def test_prepare_home_shared_policy_returns_none_and_creates_nothing(tmp_path):
    home = tmp_path / "never-created"
    result = h.prepare_home(home, links=[], policy="shared")
    assert result is None
    assert not home.exists()


def test_prepare_home_per_agent_creates_directory_mode_0700(tmp_path):
    home = tmp_path / "agent-home"
    result = h.prepare_home(home, links=[], policy="per-agent")
    assert result == home
    assert home.is_dir()
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


def test_prepare_home_creates_config_data_and_cache_subdirs(tmp_path):
    home = tmp_path / "agent-home"
    h.prepare_home(home, links=[], policy="per-agent")
    assert (home / ".config").is_dir()
    assert (home / ".local" / "share").is_dir()
    assert (home / ".cache").is_dir()


def test_prepare_home_writes_gitconfig_when_absent(tmp_path):
    home = tmp_path / "agent-home"
    h.prepare_home(home, links=[], policy="per-agent", agent="myagent")
    gitconfig = (home / ".gitconfig").read_text()
    assert "myagent (multiagents)" in gitconfig
    assert "myagent@multiagents.local" in gitconfig
    assert "gpgsign = false" in gitconfig


def test_prepare_home_leaves_preexisting_gitconfig_untouched(tmp_path):
    home = tmp_path / "agent-home"
    home.mkdir()
    (home / ".gitconfig").write_text("garbage-not-touched")
    h.prepare_home(home, links=[], policy="per-agent")
    assert (home / ".gitconfig").read_text() == "garbage-not-touched"


def test_prepare_home_is_idempotent_on_second_call(tmp_path):
    home = tmp_path / "agent-home"
    h.prepare_home(home, links=[], policy="per-agent")
    marker = home / ".config" / "custom-marker"
    marker.write_text("keep-me")
    h.prepare_home(home, links=[], policy="per-agent")
    assert marker.read_text() == "keep-me"


def test_prepare_home_target_already_a_file_raises_file_exists_error(tmp_path):
    # `home.mkdir(parents=True, exist_ok=True)` only tolerates an existing
    # DIRECTORY at that path; a pre-existing regular file at `home` raises
    # FileExistsError, uncaught by prepare_home.
    home = tmp_path / "agent-home"
    home.write_text("i-am-a-file-not-a-directory")
    with pytest.raises(FileExistsError):
        h.prepare_home(home, links=[], policy="per-agent")


def test_prepare_home_unwritable_parent_raises_permission_error(tmp_path):
    if os.getuid() == 0:
        pytest.skip("root ignores directory permission bits")
    parent = tmp_path / "locked-parent"
    parent.mkdir(mode=0o500)
    home = parent / "agent-home"
    try:
        with pytest.raises(PermissionError):
            h.prepare_home(home, links=[], policy="per-agent")
    finally:
        parent.chmod(0o700)     # let pytest clean up tmp_path afterwards


def test_prepare_home_link_source_missing_is_silently_skipped(tmp_path, monkeypatch):
    fake_real_home = tmp_path / "fake-real-home"
    fake_real_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_real_home)
    home = tmp_path / "agent-home"
    result = h.prepare_home(home, links=["does-not-exist-in-real-home"], policy="per-agent")
    assert result == home
    assert not (home / "does-not-exist-in-real-home").exists()


def test_prepare_home_link_creates_symlink_to_real_home_source(tmp_path, monkeypatch):
    fake_real_home = tmp_path / "fake-real-home"
    fake_real_home.mkdir()
    (fake_real_home / ".provider-creds").write_text("token")
    monkeypatch.setattr(Path, "home", lambda: fake_real_home)
    home = tmp_path / "agent-home"
    h.prepare_home(home, links=[".provider-creds"], policy="per-agent")
    linked = home / ".provider-creds"
    assert linked.is_symlink()
    assert linked.resolve() == (fake_real_home / ".provider-creds").resolve()
    assert linked.read_text() == "token"


def test_prepare_home_link_is_noop_when_target_already_exists_as_a_plain_file(tmp_path, monkeypatch):
    # A pre-existing regular file (not a symlink) at the link target is left
    # alone with no error and no overwrite — `target.is_symlink() or
    # target.exists()` short-circuits before symlink_to is ever attempted.
    fake_real_home = tmp_path / "fake-real-home"
    fake_real_home.mkdir()
    (fake_real_home / ".provider-creds").write_text("real-token")
    monkeypatch.setattr(Path, "home", lambda: fake_real_home)
    home = tmp_path / "agent-home"
    home.mkdir()
    (home / ".provider-creds").write_text("pre-existing-plain-file")
    h.prepare_home(home, links=[".provider-creds"], policy="per-agent")
    target = home / ".provider-creds"
    assert not target.is_symlink()
    assert target.read_text() == "pre-existing-plain-file"


def test_prepare_home_copies_copies_a_file_and_chmods_0600(tmp_path, monkeypatch):
    fake_real_home = tmp_path / "fake-real-home"
    fake_real_home.mkdir()
    (fake_real_home / "state.json").write_text('{"k": "v"}')
    monkeypatch.setattr(Path, "home", lambda: fake_real_home)
    home = tmp_path / "agent-home"
    h.prepare_home(home, links=[], policy="per-agent", copies=["state.json"])
    copied = home / "state.json"
    assert copied.is_file()
    assert not copied.is_symlink()
    assert copied.read_text() == '{"k": "v"}'
    assert stat.S_IMODE(copied.stat().st_mode) == 0o600


def test_prepare_home_copies_source_missing_is_silently_skipped(tmp_path, monkeypatch):
    fake_real_home = tmp_path / "fake-real-home"
    fake_real_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_real_home)
    home = tmp_path / "agent-home"
    h.prepare_home(home, links=[], policy="per-agent", copies=["missing.json"])
    assert not (home / "missing.json").exists()


def test_prepare_home_copies_is_noop_once_target_exists_even_if_source_changes(tmp_path, monkeypatch):
    # "Seeded once, not kept in step": a second prepare_home call does not
    # refresh the copy even though the source changed in between.
    fake_real_home = tmp_path / "fake-real-home"
    fake_real_home.mkdir()
    (fake_real_home / "state.json").write_text("version-1")
    monkeypatch.setattr(Path, "home", lambda: fake_real_home)
    home = tmp_path / "agent-home"
    h.prepare_home(home, links=[], policy="per-agent", copies=["state.json"])
    (fake_real_home / "state.json").write_text("version-2")
    h.prepare_home(home, links=[], policy="per-agent", copies=["state.json"])
    assert (home / "state.json").read_text() == "version-1"
