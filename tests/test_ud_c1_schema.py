"""UD-C1 (and P3): the decision schema file, and every record validating against it."""
import copy
import json

import jsonschema
import pytest

from support.ud_support import (AGENT, LEGACY_ANSWERED, LEGACY_OPEN, ORCH, USER, D,
                                assert_valid, clock, create, in_status, kwargs, make_tree,
                                schema_path, seed_legacy, validator)

FIELDS = {"id", "schema_version", "revision", "asked_by", "audience", "asked_at", "topic",
          "title", "context", "implications", "mode", "options", "min", "max",
          "recommended", "allow_free_text", "default_text", "refs", "status", "history",
          "answer", "delivery", "handled"}


def _schema():
    return json.loads(schema_path().read_text())


@pytest.fixture
def valid_record(tmp_path):
    return create(make_tree(tmp_path))


def test_c1_the_schema_file_is_a_valid_2020_12_schema():
    schema = _schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    assert "2020-12" in schema.get("$schema", "")


def test_c1_schema_version_is_a_const_of_one():
    prop = _schema()["properties"]["schema_version"]
    assert prop.get("const") == 1


def test_c1_the_schema_declares_every_field_and_forbids_others():
    schema = _schema()
    assert set(schema["properties"]) == FIELDS
    assert schema.get("additionalProperties") is False


@pytest.mark.parametrize("status", ["open", "answered", "handled", "withdrawn", "superseded"])
def test_c1_a_record_in_every_status_validates(tmp_path, status):
    tree = make_tree(tmp_path)
    rec = in_status(tree, status)
    assert rec["status"] == status
    assert_valid(rec)
    assert_valid(D.get(tree, rec["id"]))


@pytest.mark.parametrize("mode", ["single", "multi", "free_text"])
def test_c1_a_record_in_every_mode_validates(tmp_path, mode):
    rec = create(make_tree(tmp_path), mode)
    assert rec["mode"] == mode
    assert_valid(rec)


def test_c1_an_escalated_record_validates(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree, audience="orchestrator", by=AGENT)
    rec = D.escalate(tree, rec["id"], revision=rec["revision"], by=ORCH)
    assert rec["audience"] == "user"
    assert_valid(rec)


def test_c1_a_record_with_every_optional_field_validates(tmp_path):
    # UD-C9 (slice 2): refs are real. The tree sits in a project layout, with the
    # file present and the commit resolvable.
    import os
    import subprocess

    from multiagents.tree import Tree
    root = tmp_path / "proj"
    (root / ".multiagents").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "docs" / "x.md").write_text("x")
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ["PATH"]}
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "m"]):
        subprocess.run(["git", "-C", str(root), *cmd], check=True, env=env, capture_output=True)
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, env=env,
                          capture_output=True, text=True).stdout.strip()
    tree = Tree(root / ".multiagents" / "tree.json", root / ".multiagents" / "events.jsonl")
    rec = create(tree, "multi", recommended=["yes", "no"],
                 default_text="DEFAULT: yes", allow_free_text=False,
                 refs=[{"path": "docs/x.md"}, {"commit": head[:7]}])
    assert_valid(rec)


def test_c1_legacy_records_in_every_status_validate(tmp_path):
    tree = make_tree(tmp_path)
    seed_legacy(tree, LEGACY_OPEN, LEGACY_ANSWERED)
    for q in (LEGACY_OPEN, LEGACY_ANSWERED):
        assert_valid(D.get(tree, q["id"]))
    listed = D.list(tree, limit=200)["decisions"]
    assert {r["id"] for r in listed} == {LEGACY_OPEN["id"], LEGACY_ANSWERED["id"]}
    for rec in listed:
        assert_valid(rec)


def test_c1_a_legacy_record_with_very_long_text_still_validates(tmp_path):
    """P3: context, comment and free text are bounded by create()/answer(),
    not by the schema, so an old long record is not invalid."""
    tree = make_tree(tmp_path)
    seed_legacy(tree, dict(LEGACY_ANSWERED, question="q" * 50_000, answer="a" * 50_000))
    assert_valid(D.get(tree, LEGACY_ANSWERED["id"]))


def test_c1_the_schema_does_not_bound_context_or_free_text(valid_record):
    rec = copy.deepcopy(valid_record)
    rec["context"] = "c" * 30_000
    assert_valid(rec)
    rec["answer"] = {"options": [], "comment": "m" * 9000, "free_text": "t" * 9000,
                     "answered_by": USER, "answered_at": 1.0, "revision": 1,
                     "request_id": None}
    assert_valid(rec)


def test_c1_every_field_the_module_returns_is_declared(tmp_path):
    tree = make_tree(tmp_path)
    declared = set(_schema()["properties"])
    for status in ("open", "answered", "handled", "withdrawn", "superseded"):
        assert set(in_status(tree, status)) <= declared


@pytest.mark.parametrize("path,value", [
    (("schema_version",), 2),
    (("revision",), 0),
    (("topic",), ""),
    (("topic",), "t" * 61),
    (("title",), ""),
    (("title",), "t" * 201),
    (("title",), "two\nlines"),
    (("topic",), "two\nlines"),
    (("mode",), "radio"),
    (("status",), "reopened"),
    (("audience",), "everyone"),
    (("asked_by", "role"), "user"),
    (("handled",), {"at": 1.0, "by": ORCH, "outcome": "maybe", "reason": ""}),
])
def test_c1_the_schema_rejects_out_of_range_values(valid_record, path, value):
    rec = copy.deepcopy(valid_record)
    target = rec
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert list(validator().iter_errors(rec)), f"{path}={value!r} must not validate"


def test_c1_the_schema_accepts_the_length_boundaries(valid_record):
    rec = copy.deepcopy(valid_record)
    rec["topic"] = "t" * 60
    rec["title"] = "t" * 200
    assert_valid(rec)


@pytest.mark.parametrize("opt_id", ["Yes", "-yes", "_yes", "y" * 33, "", "a b", "a.b"])
def test_c1_the_schema_rejects_a_bad_option_id(valid_record, opt_id):
    rec = copy.deepcopy(valid_record)
    rec["options"][0]["id"] = opt_id
    assert list(validator().iter_errors(rec))


@pytest.mark.parametrize("opt_id", ["a", "0", "a-b_c", "y" * 32])
def test_c1_the_schema_accepts_a_good_option_id(valid_record, opt_id):
    rec = copy.deepcopy(valid_record)
    rec["options"][0]["id"] = opt_id
    assert_valid(rec)


def test_c1_the_schema_rejects_an_undeclared_field(valid_record):
    rec = dict(valid_record, already_answered=False)
    assert list(validator().iter_errors(rec))


def test_c1_the_schema_rejects_an_undeclared_option_field(valid_record):
    rec = copy.deepcopy(valid_record)
    rec["options"][0]["colour"] = "red"
    assert list(validator().iter_errors(rec))


def test_c1_records_never_carry_operation_metadata(tmp_path):
    """P6: `already_answered` lives on answer()'s envelope, never on a record."""
    tree = make_tree(tmp_path)
    rec = create(tree)
    out = D.answer(tree, rec["id"], revision=1, by=USER, options=["yes"])
    assert set(out) == {"decision", "already_answered"}
    assert "already_answered" not in out["decision"]
    assert "already_answered" not in D.get(tree, rec["id"])
    assert "already_answered" not in D.list(tree)["decisions"][0]
