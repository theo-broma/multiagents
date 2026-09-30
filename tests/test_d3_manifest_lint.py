"""D3 lint: schema (DM-R2), references (DM-R4), coverage (DM-R5), layout of
the shipped manifests (DM-R1). Contract: context/specs/d3-cli-dependency-manifest.md.

Fixture trees are built by tests/support/d3_support.py; see there for the two
assumptions the contract leaves open (module name, `lint(root=)` seam).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent / "support"))
import d3_support as s  # noqa: E402

SH = f"{s.PROVIDERS_REL}/fake.sh"
PY = f"{s.PROVIDERS_REL}/fake.py"
PROVIDERS = ("claude", "codex", "opencode", "agy")


def forward(tmp_path, ref, value="--flag", match=None, files=None, entry=None):
    """Findings from a one-dependency manifest whose only reference is `ref`."""
    manifest = s.base_manifest()
    manifest["dependencies"] = [s.dep("flag.x", value, ref, match=match)]
    root = s.make_repo(tmp_path / "repo", manifest=manifest, files=files, entry=entry)
    return s.run_lint(root)


def unresolved(findings):
    return [f for f in findings if f.code == "unresolved_ref"]


# ============================================================ DM-R1 shipped

def test_dm_r1_shipped_manifests_exist_for_the_four_providers():
    for name in PROVIDERS:
        assert (s.SHIPPED / "providers" / f"{name}.dependencies.yaml").is_file(), name


@pytest.mark.parametrize("name", PROVIDERS)
def test_dm_r1_shipped_manifest_starts_unverified_and_names_its_provider(name):
    doc = yaml.safe_load((s.SHIPPED / "providers" / f"{name}.dependencies.yaml").read_text())
    assert doc["schema"] == 1
    assert doc["provider"] == name
    assert doc["verified"] == []          # DM-R3: the implementer invents no entries


def test_dm_r5_the_shipped_tree_lints_clean():
    assert s.api().lint() == []


def test_dm_r5_findings_carry_provider_dependency_id_code_and_message(tmp_path):
    findings = forward(tmp_path, f"{SH}::fn:nope")
    (finding,) = unresolved(findings)
    assert finding.provider == "fake"
    assert finding.dependency_id == "flag.x"
    assert isinstance(finding.message, str) and finding.message


def test_dm_r5_codex_yaml_surfaces_are_declared_not_dependencies():
    doc = yaml.safe_load((s.SHIPPED / "providers" / "codex.dependencies.yaml").read_text())
    selectors = [n["selector"] for n in doc.get("not_dependencies", [])]
    assert any(x.startswith("/spawn") for x in selectors)
    assert any(x.startswith("/stream") for x in selectors)
    assert all(n["reason"].strip() for n in doc["not_dependencies"])


@pytest.mark.parametrize("name", PROVIDERS)
def test_dm_r2_shipped_dependencies_never_declare_multiagents_own_variables(name):
    doc = yaml.safe_load((s.SHIPPED / "providers" / f"{name}.dependencies.yaml").read_text())
    ids = [d["id"] for d in doc["dependencies"]]
    assert len(ids) == len(set(ids))
    assert not [d for d in doc["dependencies"] if "MULTIAGENTS_" in str(d["value"])]


def test_dm_r1_lint_ignores_user_overrides(tmp_path, monkeypatch):
    paths, gdir = s.make_project(tmp_path, monkeypatch)
    for name in PROVIDERS:
        s.write_yaml(s.global_manifest_path(gdir, name), "not: [valid")
        s.write_yaml(s.project_manifest_path(paths, name), "schema: 99\n")
    s.write_yaml(paths.config / "providers.yaml",
                 {"providers": {"claude": {"bin": "x", "spawn": {"args": ["zzz"]}}}})
    assert s.api().lint() == []


def test_dm_r1_lint_skips_a_provider_with_no_manifest(tmp_path):
    root = s.make_repo(tmp_path / "repo")
    (root / s.DEFAULTS / "providers.yaml").write_text(yaml.safe_dump(
        {"providers": {"fake": {"bin": "fakecli", "notes": "go"},
                       "bare": {"bin": "bare", "spawn": {"args": ["--x"]}}}}))
    assert s.run_lint(root) == []


# ================================================================ DM-R2

def test_dm_r2_a_minimal_document_is_accepted(tmp_path):
    root = s.make_repo(tmp_path / "repo")
    assert s.run_lint(root) == []


def test_dm_r2_optional_keys_may_be_absent(tmp_path):
    manifest = s.base_manifest()
    del manifest["binary"]["version_stream"]          # defaults to stdout
    del manifest["dependencies"][0]["match"]           # defaults to exact
    assert s.run_lint(s.make_repo(tmp_path / "repo", manifest=manifest)) == []


def test_dm_r2_note_and_not_dependencies_are_accepted(tmp_path):
    manifest = s.base_manifest()
    manifest["dependencies"][0]["note"] = "why"
    manifest["not_dependencies"] = []
    assert s.run_lint(s.make_repo(tmp_path / "repo", manifest=manifest)) == []


def _mut(fn):
    def apply(doc):
        fn(doc)
        return doc
    return apply


MALFORMED = {
    "schema_missing": _mut(lambda d: d.pop("schema")),
    "schema_2": _mut(lambda d: d.update(schema=2)),
    "schema_0": _mut(lambda d: d.update(schema=0)),
    "schema_string": _mut(lambda d: d.update(schema="1")),
    "provider_mismatch": _mut(lambda d: d.update(provider="other")),
    "provider_missing": _mut(lambda d: d.pop("provider")),
    "binary_missing": _mut(lambda d: d.pop("binary")),
    "binary_not_mapping": _mut(lambda d: d.update(binary="fakecli")),
    "binary_name_missing": _mut(lambda d: d["binary"].pop("name")),
    "version_command_missing": _mut(lambda d: d["binary"].pop("version_command")),
    "version_command_string": _mut(lambda d: d["binary"].update(version_command="--version")),
    "version_regex_missing": _mut(lambda d: d["binary"].pop("version_regex")),
    "version_regex_uncompilable": _mut(lambda d: d["binary"].update(version_regex="(")),
    "version_stream_unknown": _mut(lambda d: d["binary"].update(version_stream="both")),
    "dependencies_missing": _mut(lambda d: d.pop("dependencies")),
    "dependencies_not_list": _mut(lambda d: d.update(dependencies={"id": "flag.go"})),
    "dep_id_missing": _mut(lambda d: d["dependencies"][0].pop("id")),
    "dep_kind_missing": _mut(lambda d: d["dependencies"][0].pop("kind")),
    "dep_value_missing": _mut(lambda d: d["dependencies"][0].pop("value")),
    "dep_used_by_missing": _mut(lambda d: d["dependencies"][0].pop("used_by")),
    "dep_used_by_empty": _mut(lambda d: d["dependencies"][0].update(used_by=[])),
    "dep_used_by_string": _mut(lambda d: d["dependencies"][0].update(used_by="providers.yaml#/fake/notes")),
    "dep_kind_unknown": _mut(lambda d: d["dependencies"][0].update(kind="option", id="option.go")),
    "dep_id_without_kind_prefix": _mut(lambda d: d["dependencies"][0].update(id="go")),
    "dep_id_kind_disagrees": _mut(lambda d: d["dependencies"][0].update(id="env.go")),
    "dep_match_unknown": _mut(lambda d: d["dependencies"][0].update(match="regex")),
    "dep_value_not_string": _mut(lambda d: d["dependencies"][0].update(value=["go"])),
    "verified_not_list": _mut(lambda d: d.update(verified="none")),
    "not_dependencies_not_list": _mut(lambda d: d.update(not_dependencies="/spawn/args")),
    "not_dependency_without_reason": _mut(
        lambda d: d.update(not_dependencies=[{"selector": "/spawn/args"}])),
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_dm_r2_rejects_each_missing_or_ill_typed_key(tmp_path, case):
    doc = MALFORMED[case](s.base_manifest())
    findings = s.run_lint(s.make_repo(tmp_path / "repo", manifest=doc))
    assert "malformed" in s.codes(findings, "fake"), findings


@pytest.mark.parametrize("text", ["", "- a\n- b\n", "not: [valid", "just a string"])
def test_dm_r2_a_non_mapping_or_unparseable_document_is_malformed_not_a_crash(tmp_path, text):
    findings = s.run_lint(s.make_repo(tmp_path / "repo", manifest=text))
    assert "malformed" in s.codes(findings, "fake")


@pytest.mark.parametrize("kind", ["subcommand", "flag", "env", "state_path", "stream_field",
                                  "text_match", "exit_code", "endpoint", "layout"])
def test_dm_r2_every_listed_kind_is_accepted(tmp_path, kind):
    manifest = s.base_manifest()
    manifest["dependencies"] = [s.dep(f"{kind}.go", "go", "providers.yaml#/fake/notes")]
    assert s.run_lint(s.make_repo(tmp_path / "repo", manifest=manifest)) == []


@pytest.mark.parametrize("stream", ["stdout", "stderr", "either"])
def test_dm_r2_every_version_stream_is_accepted(tmp_path, stream):
    manifest = s.base_manifest()
    manifest["binary"]["version_stream"] = stream
    assert s.run_lint(s.make_repo(tmp_path / "repo", manifest=manifest)) == []


def test_dm_r2_duplicate_ids_are_reported(tmp_path):
    manifest = s.base_manifest()
    manifest["dependencies"].append(dict(manifest["dependencies"][0]))
    findings = s.run_lint(s.make_repo(tmp_path / "repo", manifest=manifest))
    dup = [f for f in findings if f.code == "duplicate_id"]
    assert dup and dup[0].dependency_id == "flag.go"


def test_dm_r2_same_slug_under_different_kinds_is_not_a_duplicate(tmp_path):
    manifest = s.base_manifest()
    manifest["dependencies"] = [s.dep("flag.go", "go", "providers.yaml#/fake/notes"),
                                s.dep("env.go", "go", "providers.yaml#/fake/notes")]
    assert s.run_lint(s.make_repo(tmp_path / "repo", manifest=manifest)) == []


@pytest.mark.parametrize("value", ["MULTIAGENTS_PROVIDER", "MULTIAGENTS_"])
def test_dm_r2_multiagents_own_variables_are_internal_protocol(tmp_path, value):
    manifest = s.base_manifest()
    manifest["dependencies"] = [s.dep("env.mine", value, "providers.yaml#/fake/notes",
                                      match="fragment")]
    findings = s.run_lint(s.make_repo(tmp_path / "repo", manifest=manifest,
                                      entry={"bin": "x", "notes": value}))
    assert [f.dependency_id for f in findings if f.code == "internal_protocol"] == ["env.mine"]


def test_dm_r2_match_defaults_to_exact(tmp_path):
    entry = {"bin": "x", "notes": 'sandbox_mode="workspace-write"'}
    findings = forward(tmp_path, "providers.yaml#/fake/notes", value="sandbox_mode=", entry=entry)
    assert unresolved(findings)


def test_dm_r2_fragment_matches_inside_a_larger_literal(tmp_path):
    entry = {"bin": "x", "notes": 'sandbox_mode="workspace-write"'}
    findings = forward(tmp_path, "providers.yaml#/fake/notes", value="sandbox_mode=",
                       match="fragment", entry=entry)
    assert unresolved(findings) == []


def test_dm_r2_explicit_exact_does_not_match_inside_a_larger_literal(tmp_path):
    entry = {"bin": "x", "notes": "prefix-go-suffix"}
    assert unresolved(forward(tmp_path, "providers.yaml#/fake/notes", "go", "exact", entry=entry))


# ============================================================ DM-R4: yaml

YAML_ENTRY = {"bin": "x", "notes": "go",
              "env": {"OPENCODE_CONFIG": "{mcp_config}", "a/b": "slash", "t~x": "tilde"},
              "extra": {"deep": {"leaf": "--deep"}, "list": ["--zero", "--one"]}}


@pytest.mark.parametrize("pointer,value", [
    ("/fake/notes", "go"),
    ("/fake/extra/list/0", "--zero"),
    ("/fake/extra/list/1", "--one"),
    ("/fake/extra/deep", "--deep"),                 # any scalar beneath
    ("/fake/extra", "--deep"),
    ("/fake/env", "OPENCODE_CONFIG"),               # a mapping's keys count
    ("/fake/env/a~1b", "slash"),                    # ~1 escapes "/"
    ("/fake/env/t~0x", "tilde"),                    # ~0 escapes "~"
])
def test_dm_r4_yaml_pointer_resolves(tmp_path, pointer, value):
    findings = forward(tmp_path, f"providers.yaml#{pointer}", value, entry=YAML_ENTRY)
    assert unresolved(findings) == [], findings


@pytest.mark.parametrize("pointer,value", [
    ("/fake/nothing", "go"),                        # dangling key
    ("/fake/extra/list/2", "--one"),                # index one past the end
    ("/fake/extra/list/-1", "--one"),               # negative index
    ("/fake/extra/list/01", "--zero"),              # not a canonical index
    ("/fake/extra/list/x", "--zero"),
    ("/other/notes", "go"),                         # unknown provider
    ("/fake/env/a/b", "slash"),                     # unescaped slash
    ("/fake/notes", "absent-value"),                # target exists, value does not
    ("/fake/extra/list/0", "--one"),                # value elsewhere, not at target
    ("/fake/notes/deeper", "go"),                   # descends into a scalar
])
def test_dm_r4_yaml_pointer_that_does_not_resolve_is_reported(tmp_path, pointer, value):
    findings = forward(tmp_path, f"providers.yaml#{pointer}", value, entry=YAML_ENTRY)
    assert [f.dependency_id for f in unresolved(findings)] == ["flag.x"]


def test_dm_r4_a_mapping_key_is_not_a_scalar_beneath_a_scalar_pointer(tmp_path):
    findings = forward(tmp_path, "providers.yaml#/fake/env/OPENCODE_CONFIG",
                       "OPENCODE_CONFIG", entry=YAML_ENTRY)
    assert unresolved(findings)          # the pointer designates the value, not its key


@pytest.mark.parametrize("ref", [
    "", "no-form-at-all", "providers.yaml", "providers.yaml#", "providers.yaml#fake/notes",
    f"{SH}", f"{SH}::", f"{SH}::wat", f"{SH}::case:", f"{SH}::fn:", f"{PY}::",
    "fake.txt::name", f"{s.PROVIDERS_REL}/fake.rb::x",
])
def test_dm_r4_malformed_references_are_reported(tmp_path, ref):
    files = {SH: "run() { echo --flag; }\n", PY: "def f():\n    return '--flag'\n"}
    findings = forward(tmp_path, ref, files=files)
    assert [f for f in findings if f.code in ("unresolved_ref", "malformed")
            and f.dependency_id in ("flag.x", None)], findings


def test_dm_r4_a_reference_may_not_escape_the_repository(tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("def f():\n    return '--flag'\n")
    findings = forward(tmp_path, "../outside.py::f")
    assert unresolved(findings)


def test_dm_r4_every_reference_of_a_dependency_is_checked(tmp_path):
    manifest = s.base_manifest()
    manifest["dependencies"] = [s.dep("flag.x", "go", ["providers.yaml#/fake/notes",
                                                        "providers.yaml#/fake/missing"])]
    findings = s.run_lint(s.make_repo(tmp_path / "repo", manifest=manifest))
    assert [f.dependency_id for f in unresolved(findings)] == ["flag.x"]


# ============================================================ DM-R4: python

PYSRC = '''\
"""Module docstring mentioning --module-doc."""
import os


def top():
    """Docstring mentioning --top-doc."""
    x = "--top-literal"  # comment mentioning --top-comment
    return x


def other():
    return "--other-literal"


def fmt(v):
    return f"--fmt={v}"


class Adapter:
    """Class docstring mentioning --cls-doc."""
    LIMIT = "--class-literal"

    def method(self):
        return ["--method-literal", 'sandbox_mode="workspace-write"']

    def helper(self):
        def inner():
            return "--inner-literal"
        return inner
'''


@pytest.mark.parametrize("qual,value,match,ok", [
    ("top", "--top-literal", None, True),
    ("Adapter.method", "--method-literal", None, True),
    ("Adapter", "--class-literal", None, True),              # a class names its body
    ("Adapter", "--method-literal", None, True),
    ("Adapter.method", "sandbox_mode=", "fragment", True),
    ("Adapter.method", "sandbox_mode=", None, False),         # exact needs the whole literal
    ("fmt", "--fmt=", "fragment", True),                      # f-string constant part
    ("fmt", "--fmt=", None, False),
    ("top", "--other-literal", None, False),                  # another definition's literal
    ("Adapter.method", "--class-literal", None, False),       # class attr is not the method's
    ("top", "--top-doc", None, False),                        # docstring
    ("top", "--top-doc", "fragment", False),
    ("Adapter", "--cls-doc", "fragment", False),
    ("top", "--module-doc", "fragment", False),
    ("top", "--top-comment", "fragment", False),              # comment
    ("missing", "--top-literal", None, False),                # no such definition
    ("Adapter.missing", "--method-literal", None, False),
    ("Nope.method", "--method-literal", None, False),
    ("top.inner", "--inner-literal", None, False),
])
def test_dm_r4_python_reference(tmp_path, qual, value, match, ok):
    findings = forward(tmp_path, f"{PY}::{qual}", value, match=match, files={PY: PYSRC})
    assert (unresolved(findings) == []) is ok, findings


def test_dm_r4_python_nested_definition_body_counts_for_its_enclosing_definition(tmp_path):
    findings = forward(tmp_path, f"{PY}::Adapter.helper", "--inner-literal", files={PY: PYSRC})
    assert unresolved(findings) == []


def test_dm_r4_python_missing_file_is_reported(tmp_path):
    assert unresolved(forward(tmp_path, f"{PY}::top", "--top-literal"))


def test_dm_r4_python_file_that_does_not_parse_is_reported_not_a_crash(tmp_path):
    findings = forward(tmp_path, f"{PY}::top", "x", files={PY: "def top(:\n"})
    assert unresolved(findings)


# ============================================================= DM-R4: shell

SHSRC = '''\
#!/bin/sh
# header comment mentioning --header-comment
helper() {
    echo "--fn-literal"   # trailing mentioning --fn-comment
}
function keyword_style {
    echo "--keyword-fn"
}

case "$1" in
  check)
    opencode auth list --check-literal
    # comment-only mention: --arm-comment
    ;;
  login|signin)
    echo "a # b --hash-in-double"
    echo 'c # d --hash-in-single'
    v=${x#pre}; run --after-expansion
    n=${x##*/}; run --after-greedy-expansion # real comment --after-expansion-comment
    ;;
  heredoc)
    cat <<'EOF'
{"k": "--heredoc-literal"}
# looks like a comment --heredoc-hash
EOF
    ;;
  *)
    echo "--default-arm"
    ;;
esac
'''


@pytest.mark.parametrize("ref,value,ok", [
    ("case:check", "--check-literal", True),
    ("case:login", "--hash-in-double", True),        # '#' inside double quotes
    ("case:login", "--hash-in-single", True),        # '#' inside single quotes
    ("case:login", "--after-expansion", True),       # '#' inside ${x#y}
    ("case:login", "--after-greedy-expansion", True),  # '##' inside ${x##y}
    ("case:signin", "--hash-in-double", True),       # any alternative label names the arm
    ("case:heredoc", "--heredoc-literal", True),     # here-doc body is a literal
    ("case:heredoc", "--heredoc-hash", True),        # ... even a body line starting with '#'
    ("case:*", "--default-arm", True),
    ("case:check", "--arm-comment", False),          # comment only
    ("case:login", "--after-expansion-comment", False),
    ("case:check", "--hash-in-double", False),       # another arm's literal
    ("case:login", "--check-literal", False),
    ("case:absent", "--check-literal", False),
    ("fn:helper", "--fn-literal", True),
    ("fn:keyword_style", "--keyword-fn", True),
    ("fn:helper", "--fn-comment", False),
    ("fn:helper", "--keyword-fn", False),
    ("fn:absent", "--fn-literal", False),
])
def test_dm_r4_shell_reference(tmp_path, ref, value, ok):
    findings = forward(tmp_path, f"{SH}::{ref}", value, files={SH: SHSRC})
    assert (unresolved(findings) == []) is ok, findings


def test_dm_r4_shell_header_comment_is_not_a_literal_of_any_target(tmp_path):
    for ref in ("case:check", "fn:helper"):
        findings = forward(tmp_path, f"{SH}::{ref}", "--header-comment", files={SH: SHSRC})
        assert unresolved(findings)


def test_dm_r4_shell_dotted_field_path_may_be_a_fragment(tmp_path):
    src = 'case "$1" in\n  usage)\n    jq -r ".part.state.input.x" \n    ;;\nesac\n'
    ok = forward(tmp_path, f"{SH}::case:usage", "part.state.input", match="fragment",
                 files={SH: src})
    assert unresolved(ok) == []
    exact = forward(tmp_path / "b", f"{SH}::case:usage", "part.state.input", match="exact",
                    files={SH: src})
    assert unresolved(exact)


def test_dm_r4_shell_file_form_on_a_script_without_case_or_functions(tmp_path):
    src = '#!/bin/sh\n# comment --only-comment\nexec fakecli --flat-literal "$@"\n'
    assert unresolved(forward(tmp_path, f"{SH}::file", "--flat-literal", files={SH: src})) == []
    assert unresolved(forward(tmp_path / "b", f"{SH}::file", "--only-comment", files={SH: src}))


@pytest.mark.parametrize("src", [
    'case "$1" in\n  a) echo --flag ;;\nesac\n',
    'f() { echo --flag; }\n',
])
def test_dm_r4_shell_file_form_is_refused_for_a_script_with_case_arms_or_functions(tmp_path, src):
    findings = forward(tmp_path, f"{SH}::file", "--flag", files={SH: src})
    assert [f for f in findings if f.code in ("unresolved_ref", "malformed")], findings


def test_dm_r4_shell_missing_file_is_reported(tmp_path):
    assert unresolved(forward(tmp_path, f"{SH}::fn:helper", "--fn-literal"))


# ================================================================ DM-R5

SPAWN = {"bin": "x", "spawn": {"args": ["run", "--go", "{prompt}"]}}


def coverage(tmp_path, entry, deps, not_deps=None, name="repo"):
    manifest = s.base_manifest()
    manifest["dependencies"] = deps
    if not_deps is not None:
        manifest["not_dependencies"] = not_deps
    root = s.make_repo(tmp_path / name, manifest=manifest, entry=entry)
    return s.run_lint(root)


def uncovered(findings):
    return [f for f in findings if f.code == "uncovered_leaf"]


def test_dm_r5_every_leaf_covered_by_its_own_pointer_is_clean(tmp_path):
    deps = [s.dep("subcommand.run", "run", "providers.yaml#/fake/spawn/args/0"),
            s.dep("flag.go", "--go", "providers.yaml#/fake/spawn/args/1")]
    assert coverage(tmp_path, SPAWN, deps) == []          # {prompt} is exempt


def test_dm_r5_an_uncovered_leaf_is_reported(tmp_path):
    deps = [s.dep("subcommand.run", "run", "providers.yaml#/fake/spawn/args/0")]
    findings = uncovered(coverage(tmp_path, SPAWN, deps))
    assert len(findings) == 1 and findings[0].provider == "fake"
    assert "--go" in findings[0].message or "args/1" in findings[0].message


def test_dm_r5_placeholders_alone_need_no_coverage(tmp_path):
    entry = {"bin": "x", "notes": "go",
             "spawn": {"args": ["{prompt}", "{model}", "{workdir}"]}}
    deps = [s.dep("flag.go", "go", "providers.yaml#/fake/notes")]
    assert uncovered(coverage(tmp_path, entry, deps)) == []


def test_dm_r5_a_pointer_to_a_larger_subtree_covers_nothing(tmp_path):
    deps = [s.dep("flag.go", "--go", "providers.yaml#/fake/spawn", match="fragment")]
    assert len(uncovered(coverage(tmp_path, SPAWN, deps))) == 2      # run and --go


def test_dm_r5_a_pointer_to_the_whole_entry_covers_nothing(tmp_path):
    deps = [s.dep("flag.go", "--go", "providers.yaml#/fake")]
    assert len(uncovered(coverage(tmp_path, SPAWN, deps))) == 2


def test_dm_r5_a_list_of_plain_scalars_is_covered_through_its_parent_pointer(tmp_path):
    entry = {"bin": "x", "models_cmd": ["--models"]}
    deps = [s.dep("subcommand.models", "--models", "providers.yaml#/fake/models_cmd")]
    assert uncovered(coverage(tmp_path, entry, deps)) == []


def test_dm_r5_the_parent_pointer_still_needs_the_value_to_occur(tmp_path):
    entry = {"bin": "x", "models_cmd": ["--models"]}
    deps = [s.dep("subcommand.models", "--wrong", "providers.yaml#/fake/models_cmd")]
    assert coverage(tmp_path, entry, deps)          # unresolved, and the leaf stays uncovered


@pytest.mark.parametrize("entry,leaf", [
    ({"spawn": {"args": ["--a"]}}, "spawn/args"),
    ({"spawn": {"resume": ["--r"]}}, "spawn/resume"),
    ({"spawn": {"permission": {"full": ["--p"]}}}, "spawn/permission"),
    ({"spawn": {"optional": {"variant": ["--v"]}}}, "spawn/optional"),
    ({"models_cmd": ["--m"]}, "models_cmd"),
    ({"mcp": {"args": ["--mcp"]}}, "mcp/args"),
    ({"stream": {"session_id_paths": ["session.id"]}}, "session_id_paths"),
    ({"stream": {"rules": [{"match": {"type": "result"}, "fields": {"x": "a.b"}}]}}, "rules"),
    ({"stream": {"status_map": {"content-filter": "refused"}}}, "status_map"),
    ({"refusal_markers": ["refused"]}, "refusal_markers"),
    ({"truncation_markers": ["truncated"]}, "truncation_markers"),
    ({"transcript": {"limit_markers": [{"match": "limit reached"}]}}, "limit_markers"),
    ({"home_links": [".local/share/x"]}, "home_links"),
    ({"bin_versions_depth": 3}, "bin_versions_depth"),
])
def test_dm_r5_each_listed_selector_demands_coverage(tmp_path, entry, leaf):
    findings = uncovered(coverage(tmp_path, {"bin": "x", **entry}, []))
    assert findings, f"{leaf}: nothing demanded"


def test_dm_r5_surfaces_outside_the_selector_list_demand_nothing(tmp_path):
    entry = {"bin": "x", "notes": "free text", "usage_mode": "delta", "family": "f",
             "billing": "plan", "bin_search": ["~/.x/bin"], "models_parse": "lines",
             "models_include": ["opencode/*"], "auth": {"script": "fake.sh"},
             "agent_guidance": "be nice"}
    findings = coverage(tmp_path, entry,
                        [s.dep("flag.go", "free text", "providers.yaml#/fake/notes")])
    assert uncovered(findings) == []


def test_dm_r5_stream_rule_match_keys_and_values_are_separate_leaves(tmp_path):
    entry = {"bin": "x", "stream": {"rules": [{"match": {"part.reason": "stop"}}]}}
    only_key = [s.dep("stream_field.reason", "part.reason",
                      "providers.yaml#/fake/stream/rules/0/match")]
    assert len(uncovered(coverage(tmp_path, entry, only_key))) >= 1


def test_dm_r5_status_map_keys_are_leaves_its_values_are_not(tmp_path):
    entry = {"bin": "x", "stream": {"status_map": {"content-filter": "refused"}}}
    assert len(uncovered(coverage(tmp_path, entry, []))) == 1


def test_dm_r5_not_dependencies_waives_the_leaves_it_names(tmp_path):
    findings = coverage(tmp_path, SPAWN, [],
                        not_deps=[{"selector": "/spawn/args", "reason": "adapter does it"}])
    assert findings == []


def test_dm_r5_a_stale_not_dependency_is_reported(tmp_path):
    findings = coverage(tmp_path, {"bin": "x", "notes": "go"},
                        [s.dep("flag.go", "go", "providers.yaml#/fake/notes")],
                        not_deps=[{"selector": "/spawn/args", "reason": "gone"}])
    stale = [f for f in findings if f.code == "stale_not_dependency"]
    assert len(stale) == 1 and stale[0].provider == "fake"


def test_dm_r5_a_live_not_dependency_is_not_stale(tmp_path):
    findings = coverage(tmp_path, SPAWN, [],
                        not_deps=[{"selector": "/spawn/args", "reason": "adapter"}])
    assert [f for f in findings if f.code == "stale_not_dependency"] == []


def test_dm_r5_findings_are_per_provider(tmp_path):
    root = s.make_repo(tmp_path / "repo", entry={"bin": "x", "notes": "go"})
    (root / s.DEFAULTS / "providers.yaml").write_text(yaml.safe_dump({"providers": {
        "fake": {"bin": "x", "notes": "go"},
        "other": {"bin": "y", "models_cmd": ["--m"]}}}))
    other = s.base_manifest("other")
    other["dependencies"] = []
    (root / s.DEFAULTS / "providers" / "other.dependencies.yaml").write_text(yaml.safe_dump(other))
    findings = s.run_lint(root)
    assert {f.provider for f in findings} == {"other"}
