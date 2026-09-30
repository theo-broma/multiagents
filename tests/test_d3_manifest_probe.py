"""D3 layering (DM-R1), digests (DM-R3a), verified (DM-R3) and the host probe
with its state precedence (DM-R6), driven through `probe()` against fake CLIs.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent / "support"))
import d3_support as s  # noqa: E402

VERSIONS_CLI = """\
case "$1" in
  --ver-project) echo "fakecli 3.0.0" ;;
  --ver-global) echo "fakecli 2.0.0" ;;
  --version) echo "fakecli 1.2.3" ;;
  *) exit 2 ;;
esac
"""


@pytest.fixture
def proj(tmp_path, monkeypatch):
    """A project with a working fake CLI (version 1.2.3) and no manifest yet."""
    cli = s.write_cli(tmp_path / "bin", body=VERSIONS_CLI)
    paths, gdir = s.make_project(tmp_path, monkeypatch, bin_path=cli)
    return paths, gdir, cli


def probe(paths, name="fakecli", context="host"):
    return s.api().probe(name, paths, context)


def put_project(paths, doc=None, name="fakecli"):
    return s.write_yaml(s.project_manifest_path(paths, name), doc or s.runtime_manifest(name))


def put_global(gdir, doc=None, name="fakecli"):
    return s.write_yaml(s.global_manifest_path(gdir, name), doc or s.runtime_manifest(name))


def digests(paths, name="fakecli"):
    return probe(paths, name).digests


# ============================================================ DM-R1 layering

def test_dm_r1_project_layer_beats_global_and_shipped(proj):
    paths, gdir, _ = proj
    g = s.runtime_manifest()
    g["binary"]["version_command"] = ["--ver-global"]
    p = s.runtime_manifest()
    p["binary"]["version_command"] = ["--ver-project"]
    put_global(gdir, g)
    put_project(paths, p)
    result = probe(paths)
    assert result.version == "3.0.0"
    assert str(s.project_manifest_path(paths)) in str(result.manifest_source)


def test_dm_r1_global_layer_is_used_when_the_project_has_none(proj):
    paths, gdir, _ = proj
    g = s.runtime_manifest()
    g["binary"]["version_command"] = ["--ver-global"]
    put_global(gdir, g)
    result = probe(paths)
    assert result.version == "2.0.0"
    assert str(s.global_manifest_path(gdir)) in str(result.manifest_source)


def test_dm_r1_shipped_layer_is_the_fallback(tmp_path, monkeypatch):
    cli = s.write_cli(tmp_path / "bin", "claude", body='echo "claude 2.0.0"\n')
    paths, _ = s.make_project(tmp_path, monkeypatch, provider="claude", bin_path=cli)
    result = probe(paths, "claude")
    assert str(s.SHIPPED / "providers" / "claude.dependencies.yaml") in str(result.manifest_source)


def test_dm_r1_the_first_file_found_wins_whole_and_layers_are_never_merged(proj):
    paths, gdir, _ = proj
    put_global(gdir)
    first = probe(paths)                      # global manifest, unverified
    assert first.state == "unverified"
    g = s.runtime_manifest()
    g["verified"] = [s.verified_entry(first.digests)]
    put_global(gdir, g)
    assert probe(paths).state == "verified"
    put_project(paths, s.runtime_manifest())  # same dependencies, verified: []
    assert probe(paths).state == "unverified"


def test_dm_r1_a_malformed_project_manifest_does_not_fall_through_to_a_good_global_one(proj):
    paths, gdir, _ = proj
    put_global(gdir)
    put_project(paths, "schema: 2\n")
    assert probe(paths).state == "malformed"


def test_dm_r1_a_provider_with_no_manifest_in_any_layer_is_reported_as_such(proj):
    paths, _, _ = proj
    result = probe(paths)
    assert result.state == "no manifest"


def test_dm_r1_a_manifest_named_for_another_provider_is_not_picked_up(proj):
    paths, _, _ = proj
    put_project(paths, s.runtime_manifest("other"), name="other")
    assert probe(paths).state == "no manifest"


# =============================================================== DM-R6 states

def test_dm_r6_unverified(proj):
    paths, _, _ = proj
    put_project(paths)
    result = probe(paths)
    assert result.state == "unverified"
    assert result.version == "1.2.3"


def test_dm_r6_probe_result_carries_all_documented_fields(proj):
    paths, _, _ = proj
    put_project(paths)
    r = probe(paths)
    for field in ("state", "version", "detail", "manifest_source",
                  "integration_sources", "digests"):
        assert hasattr(r, field), field
    assert set(r.digests) >= {"integration_digest", "dependencies_digest"}
    assert len(r.digests["integration_digest"]) == 64
    assert len(r.digests["dependencies_digest"]) == 64


def test_dm_r6_verified_when_an_entry_applies(proj):
    paths, _, _ = proj
    doc = s.runtime_manifest()
    put_project(paths, doc)
    doc["verified"] = [s.verified_entry(digests(paths))]
    put_project(paths, doc)
    r = probe(paths)
    assert r.state == "verified"


def test_dm_r3_a_partial_scope_never_yields_plain_verified(proj):
    paths, _, _ = proj
    doc = s.runtime_manifest()
    put_project(paths, doc)
    doc["verified"] = [s.verified_entry(digests(paths), scope=["flag.go"])]
    put_project(paths, doc)
    r = probe(paths)
    assert r.state.startswith("verified (partial")
    assert "1 of 2" in r.state or "1 of 2" in str(r.detail)


def test_dm_r3_a_scope_listing_every_id_is_still_partial(proj):
    paths, _, _ = proj
    doc = s.runtime_manifest()
    put_project(paths, doc)
    doc["verified"] = [s.verified_entry(digests(paths), scope=["flag.go", "flag.stop"])]
    put_project(paths, doc)
    assert probe(paths).state.startswith("verified (partial")


@pytest.mark.parametrize("field,value", [
    ("version", "1.2.4"),
    ("version", "1.2"),                        # exact, never a prefix or an ordering
    ("version", "2.0.0"),                      # newer is not "at least"
    ("platform", "plan9-mips"),
    ("executor", "docker"),
    ("integration_digest", "0" * 64),
    ("dependencies_digest", "0" * 64),
])
def test_dm_r3_an_entry_applies_only_if_every_condition_holds(proj, field, value):
    paths, _, _ = proj
    doc = s.runtime_manifest()
    put_project(paths, doc)
    doc["verified"] = [s.verified_entry(digests(paths), **{field: value})]
    put_project(paths, doc)
    assert probe(paths).state == "unverified"


def test_dm_r3_a_non_applicable_entry_beside_an_applicable_one_still_verifies(proj):
    paths, _, _ = proj
    doc = s.runtime_manifest()
    put_project(paths, doc)
    good = s.verified_entry(digests(paths))
    doc["verified"] = [s.verified_entry(digests(paths), version="0.0.1"), good]
    put_project(paths, doc)
    assert probe(paths).state == "verified"


def test_dm_r3_editing_a_dependency_voids_earlier_verification(proj):
    paths, _, _ = proj
    doc = s.runtime_manifest()
    put_project(paths, doc)
    doc["verified"] = [s.verified_entry(digests(paths))]
    put_project(paths, doc)
    assert probe(paths).state == "verified"
    doc["dependencies"][0]["value"] = "go2"
    put_project(paths, doc)
    assert probe(paths).state == "unverified"


def test_dm_r3_a_version_that_is_not_verified_is_a_warning_and_never_a_refusal(proj):
    paths, _, _ = proj
    put_project(paths)
    r = probe(paths)                            # returns a result; nothing raised
    assert r.state == "unverified"


def test_dm_r6_state_shows_the_version_found_and_verified_versions_appear_in_detail(proj):
    paths, _, _ = proj
    doc = s.runtime_manifest()
    put_project(paths, doc)
    doc["verified"] = [s.verified_entry(digests(paths), version="0.9.0")]
    put_project(paths, doc)
    r = probe(paths)
    assert r.version == "1.2.3"
    assert "0.9.0" in str(r.detail)


@pytest.mark.parametrize("stream,emits,ok", [
    ("stdout", "stdout", True), ("stderr", "stderr", True),
    ("either", "stdout", True), ("either", "stderr", True),
    ("stdout", "stderr", False), ("stderr", "stdout", False),
    (None, "stdout", True), (None, "stderr", False),      # default is stdout
])
def test_dm_r2_the_version_is_read_from_the_stream_that_version_stream_names(
        tmp_path, monkeypatch, stream, emits, ok):
    redirect = "" if emits == "stdout" else " >&2"
    cli = s.write_cli(tmp_path / "bin", body=f'echo "fakecli 1.2.3"{redirect}\n')
    paths, _ = s.make_project(tmp_path, monkeypatch, bin_path=cli)
    doc = s.runtime_manifest()
    if stream is None:
        del doc["binary"]["version_stream"]
    else:
        doc["binary"]["version_stream"] = stream
    put_project(paths, doc)
    r = probe(paths)
    assert (r.state == "unverified") is ok
    if ok:
        assert r.version == "1.2.3"
    else:
        assert r.state == "probe_failed"


def test_dm_r2_group_1_of_the_regex_is_the_version(tmp_path, monkeypatch):
    cli = s.write_cli(tmp_path / "bin", body='echo "fakecli build 7 version=4.5.6-rc1 (x)"\n')
    paths, _ = s.make_project(tmp_path, monkeypatch, bin_path=cli)
    doc = s.runtime_manifest()
    doc["binary"]["version_regex"] = r"build (\d+) version=(\S+)"
    put_project(paths, doc)
    assert probe(paths).version == "7"             # group 1, not group 2 nor the match


def test_dm_r2_version_command_is_the_argv_after_the_binary(tmp_path, monkeypatch):
    cli = s.write_cli(tmp_path / "bin", body='echo "fakecli $1-$2"\n')
    paths, _ = s.make_project(tmp_path, monkeypatch, bin_path=cli)
    doc = s.runtime_manifest()
    doc["binary"]["version_command"] = ["sub", "cmd"]
    doc["binary"]["version_regex"] = r"fakecli (\S+)"
    put_project(paths, doc)
    assert probe(paths).version == "sub-cmd"


@pytest.mark.parametrize("body", [
    'exit 3\n',                                    # non-zero exit
    'echo "no version here"\n',                    # regex does not match
    'true\n',                                      # no output
    'echo "fakecli 1.2.3"; exit 1\n',              # output, but failed
])
def test_dm_r6_a_version_command_that_fails_or_does_not_match_is_probe_failed(
        tmp_path, monkeypatch, body):
    cli = s.write_cli(tmp_path / "bin", body=body)
    paths, _ = s.make_project(tmp_path, monkeypatch, bin_path=cli)
    put_project(paths)
    assert probe(paths).state == "probe_failed"


def test_dm_r6_a_binary_that_cannot_be_found_is_missing(tmp_path, monkeypatch):
    paths, _ = s.make_project(tmp_path, monkeypatch, bin_path=None)
    put_project(paths)
    assert probe(paths).state == "missing"


def test_dm_r6_the_probe_has_no_tty_and_stdin_is_dev_null(tmp_path, monkeypatch):
    body = ('[ -t 0 ] && exit 4\n[ -t 1 ] && exit 5\n'
            'data=$(cat); [ -z "$data" ] || exit 6\necho "fakecli 1.2.3"\n')
    cli = s.write_cli(tmp_path / "bin", body=body)
    paths, _ = s.make_project(tmp_path, monkeypatch, bin_path=cli)
    put_project(paths)
    assert probe(paths).state == "unverified"      # a cat on an open pipe would time out


def test_dm_r6_a_hung_binary_yields_timeout_within_the_default_deadline(tmp_path, monkeypatch):
    cli = s.write_cli(tmp_path / "bin", body='sleep 60\n')
    paths, _ = s.make_project(tmp_path, monkeypatch, bin_path=cli)
    put_project(paths)
    start = time.monotonic()
    r = probe(paths)
    elapsed = time.monotonic() - start
    assert r.state == "timeout"
    assert 5 <= elapsed < 10 + 5            # default 10 s, cleanup included within +5 s


def test_dm_r6_a_hung_child_holding_the_pipe_open_still_times_out_in_time(tmp_path, monkeypatch):
    cli = s.write_cli(tmp_path / "bin", body='sleep 60 &\nsleep 60\n')
    paths, _ = s.make_project(tmp_path, monkeypatch, bin_path=cli)
    put_project(paths)
    start = time.monotonic()
    assert probe(paths).state == "timeout"
    assert time.monotonic() - start < 15


def test_dm_r6_disabled_provider_is_not_probed(tmp_path, monkeypatch):
    marker = tmp_path / "ran"
    cli = s.write_cli(tmp_path / "bin", body=f'touch {marker}\necho "fakecli 1.2.3"\n')
    paths, _ = s.make_project(tmp_path, monkeypatch, bin_path=cli, enabled=False)
    put_project(paths)
    assert probe(paths).state == "disabled"
    assert not marker.exists()


# --------------------------------------------------------- state precedence

def test_dm_r6_precedence_disabled_beats_malformed(tmp_path, monkeypatch):
    paths, _ = s.make_project(tmp_path, monkeypatch, enabled=False)
    put_project(paths, "schema: 2\n")
    assert probe(paths).state == "disabled"


def test_dm_r6_precedence_disabled_beats_no_manifest_and_missing(tmp_path, monkeypatch):
    paths, _ = s.make_project(tmp_path, monkeypatch, enabled=False, bin_path=None)
    assert probe(paths).state == "disabled"


def test_dm_r6_precedence_malformed_beats_missing(tmp_path, monkeypatch):
    paths, _ = s.make_project(tmp_path, monkeypatch, bin_path=None)
    put_project(paths, "schema: 2\n")
    assert probe(paths).state == "malformed"


def test_dm_r6_precedence_no_manifest_beats_missing(tmp_path, monkeypatch):
    paths, _ = s.make_project(tmp_path, monkeypatch, bin_path=None)
    assert probe(paths).state == "no manifest"


def test_dm_r6_precedence_missing_beats_verified(proj):
    paths, _, cli = proj
    doc = s.runtime_manifest()
    put_project(paths, doc)
    doc["verified"] = [s.verified_entry(digests(paths))]
    put_project(paths, doc)
    assert probe(paths).state == "verified"
    cli.unlink()                                   # same config, binary gone
    assert probe(paths).state == "missing"


def test_dm_r6_precedence_malformed_beats_a_working_binary(proj):
    paths, _, _ = proj
    put_project(paths, "schema: 2\n")
    assert probe(paths).state == "malformed"


def _override_claude(tmp_path, monkeypatch, cli_body):
    """`claude` with a project providers.yaml override, so its integration
    differs from the shipped one, and a project manifest for the runtime."""
    cli = s.write_cli(tmp_path / "bin", "claude", body=cli_body)
    paths, gdir = s.make_project(tmp_path, monkeypatch, provider="claude", bin_path=cli)
    put_project(paths, s.runtime_manifest("claude"), name="claude")
    return paths


def test_dm_r6_an_overridden_integration_without_an_applicable_entry_is_overridden(
        tmp_path, monkeypatch):
    paths = _override_claude(tmp_path, monkeypatch, 'echo "claude 1.2.3"\n')
    assert probe(paths, "claude").state == "overridden"


def test_dm_r6_precedence_probe_failed_beats_overridden(tmp_path, monkeypatch):
    paths = _override_claude(tmp_path, monkeypatch, "exit 3\n")
    assert probe(paths, "claude").state == "probe_failed"


def test_dm_r6_precedence_an_applicable_entry_beats_overridden(tmp_path, monkeypatch):
    paths = _override_claude(tmp_path, monkeypatch, 'echo "claude 1.2.3"\n')
    doc = s.runtime_manifest("claude")
    doc["verified"] = [s.verified_entry(digests(paths, "claude"))]
    s.write_yaml(s.project_manifest_path(paths, "claude"), doc)
    assert probe(paths, "claude").state == "verified"


def test_dm_r6_a_custom_provider_has_no_shipped_integration_to_differ_from(proj):
    paths, _, _ = proj
    put_project(paths)
    assert probe(paths).state == "unverified"


def test_dm_r3_an_entry_for_the_docker_executor_does_not_apply_to_a_host_probe(proj):
    paths, _, _ = proj
    doc = s.runtime_manifest()
    put_project(paths, doc)
    doc["verified"] = [s.verified_entry(digests(paths), executor="docker")]
    put_project(paths, doc)
    assert probe(paths, context="host").state == "unverified"   # docker entry, host probe


# ================================================================ DM-R3a digests

@pytest.fixture
def integ(proj):
    """Provider with a script in the GLOBAL layer, so overrides can be layered."""
    paths, gdir, cli = proj
    put_project(paths)
    script = gdir / "providers" / "fakecli.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\necho check\n")
    return paths, gdir, script


def idigest(paths, name="fakecli"):
    return s.api().integration_digest(name, paths)


def test_dm_r3a_integration_digest_is_a_stable_sha256_hex(integ):
    paths, _, _ = integ
    a = idigest(paths)
    assert a == idigest(paths)
    assert len(a) == 64 and int(a, 16) >= 0


def test_dm_r3a_probe_prints_the_same_digest_the_function_computes(integ):
    paths, _, _ = integ
    assert probe(paths).digests["integration_digest"] == idigest(paths)


def test_dm_r3a_a_content_changing_project_override_changes_the_digest(integ):
    paths, _, _ = integ
    before = idigest(paths)
    (paths.config / "providers" / "fakecli.sh").write_text("#!/bin/sh\necho different\n")
    assert idigest(paths) != before


def test_dm_r3a_a_byte_identical_override_keeps_the_digest_but_reports_provenance(integ):
    paths, gdir, script = integ
    before = idigest(paths)
    before_sources = str(probe(paths).integration_sources)
    override = paths.config / "providers" / "fakecli.sh"
    override.write_bytes(script.read_bytes())
    assert idigest(paths) == before
    after_sources = str(probe(paths).integration_sources)
    assert str(override) in after_sources
    assert after_sources != before_sources


def test_dm_r3a_a_missing_script_is_framed_not_skipped(tmp_path, monkeypatch):
    cli = s.write_cli(tmp_path / "bin", body=VERSIONS_CLI)
    paths, gdir = s.make_project(tmp_path, monkeypatch, bin_path=cli)
    missing = idigest(paths)
    (gdir / "providers").mkdir(parents=True, exist_ok=True)
    (gdir / "providers" / "fakecli.sh").write_text("")        # present but empty
    assert idigest(paths) != missing


def test_dm_r3a_a_comment_only_providers_yaml_edit_keeps_the_digest(integ):
    paths, _, _ = integ
    before = idigest(paths)
    file = paths.config / "providers.yaml"
    file.write_text("# a new comment\n" + file.read_text().replace("\n  ", "\n\n  ", 1)
                    + "\n# trailing\n")
    assert idigest(paths) == before


def test_dm_r3a_key_order_and_quoting_do_not_change_the_digest(integ):
    paths, _, _ = integ
    before = idigest(paths)
    entry = yaml.safe_load((paths.config / "providers.yaml").read_text())["providers"]["fakecli"]
    reordered = dict(reversed(list(entry.items())))
    (paths.config / "providers.yaml").write_text(
        yaml.safe_dump({"providers": {"fakecli": reordered}}, sort_keys=False,
                       default_style='"'))
    assert idigest(paths) == before


def test_dm_r3a_a_value_edit_of_providers_yaml_changes_the_digest(integ):
    paths, _, _ = integ
    before = idigest(paths)
    file = paths.config / "providers.yaml"
    file.write_text(file.read_text().replace("go", "went"))
    assert idigest(paths) != before


def test_dm_r3a_only_the_providers_own_entry_counts(integ):
    paths, _, _ = integ
    before = idigest(paths)
    doc = yaml.safe_load((paths.config / "providers.yaml").read_text())
    doc["providers"]["unrelated"] = {"bin": "u", "spawn": {"args": ["x"]}}
    (paths.config / "providers.yaml").write_text(yaml.safe_dump(doc))
    assert idigest(paths) == before


def test_dm_r3a_the_entry_is_taken_after_extends_is_resolved(tmp_path, monkeypatch):
    cli = s.write_cli(tmp_path / "bin", body=VERSIONS_CLI)
    paths, _ = s.make_project(
        tmp_path, monkeypatch, provider="basecli", bin_path=cli,
        providers_extra={"childcli": {"extends": "basecli"}})
    before = idigest(paths, "childcli")
    doc = yaml.safe_load((paths.config / "providers.yaml").read_text())
    doc["providers"]["basecli"]["spawn"]["args"] = ["changed"]
    (paths.config / "providers.yaml").write_text(yaml.safe_dump(doc))
    assert idigest(paths, "childcli") != before          # the inherited value moved


def test_dm_r3a_the_digest_depends_on_the_provider(integ):
    paths, _, _ = integ
    doc = yaml.safe_load((paths.config / "providers.yaml").read_text())
    doc["providers"]["twin"] = dict(doc["providers"]["fakecli"])
    (paths.config / "providers.yaml").write_text(yaml.safe_dump(doc))
    assert idigest(paths, "twin") != idigest(paths, "fakecli")


def test_dm_r3a_dependencies_digest_is_stable_and_ignores_verified_and_formatting(proj):
    paths, _, _ = proj
    doc = s.runtime_manifest()
    put_project(paths, doc)
    a = digests(paths)["dependencies_digest"]
    assert a == digests(paths)["dependencies_digest"]
    doc["verified"] = [s.verified_entry(digests(paths))]
    doc["not_dependencies"] = []
    s.write_yaml(s.project_manifest_path(paths), "# comment\n" + yaml.safe_dump(
        dict(reversed(list(doc.items()))), sort_keys=False, default_style='"') + "# more\n")
    assert digests(paths)["dependencies_digest"] == a


@pytest.mark.parametrize("edit", [
    lambda d: d["dependencies"][0].update(value="other"),
    lambda d: d["dependencies"][0].update(match="fragment"),
    lambda d: d["dependencies"][0].update(used_by=["providers.yaml#/fakecli/bin"]),
    lambda d: d["dependencies"].pop(),
    lambda d: d["dependencies"].reverse(),
    lambda d: d["binary"].update(version_regex=r"(\d+)"),
    lambda d: d["binary"].update(version_command=["--version", "-x"]),
    lambda d: d["binary"].update(version_stream="either"),
])
def test_dm_r3a_editing_binary_or_a_dependency_changes_dependencies_digest(proj, edit):
    paths, _, _ = proj
    doc = s.runtime_manifest()
    put_project(paths, doc)
    before = digests(paths)["dependencies_digest"]
    edit(doc)
    put_project(paths, doc)
    assert digests(paths)["dependencies_digest"] != before


def test_dm_r3a_a_dependencies_edit_does_not_touch_the_integration_digest(proj):
    paths, _, _ = proj
    doc = s.runtime_manifest()
    put_project(paths, doc)
    before = digests(paths)["integration_digest"]
    doc["dependencies"][0]["value"] = "changed"
    put_project(paths, doc)
    assert digests(paths)["integration_digest"] == before


def test_dm_r3a_shared_integration_code_outside_the_providers_files_is_not_in_the_digest(
        integ):
    """Boundary, documented: an unrelated file in the layer does not move it."""
    paths, gdir, _ = integ
    before = idigest(paths)
    (gdir / "providers" / "other.sh").write_text("#!/bin/sh\necho other\n")
    assert idigest(paths) == before
