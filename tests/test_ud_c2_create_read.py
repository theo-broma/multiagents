"""UD-C2 (with P4 on create and P8 pagination): creating, reading, listing."""
import re

import pytest

from support.ud_support import (AGENT, INIT, ORCH, TOKEN, USER, D, assert_valid, clock,
                                code_of, create, decision_events, detail, events, in_status,
                                kwargs, make_tree)


# ----------------------------------------------------------------- create --

@pytest.mark.parametrize("mode", ["single", "multi", "free_text"])
def test_c2_round_trip_of_each_mode(tmp_path, mode):
    tree = make_tree(tmp_path)
    over = {"recommended": ["yes"]} if mode != "free_text" else {}
    rec = create(tree, mode, default_text="DEFAULT: yes", **over)
    assert re.fullmatch(r"d-[0-9a-f]{8}", rec["id"])
    assert rec["status"] == "open" and rec["revision"] == 1 and rec["schema_version"] == 1
    assert rec["mode"] == mode and rec["audience"] == "user"
    assert rec["topic"] == "Run D merge" and rec["title"] == "Merge run D?"
    assert rec["context"] == "Run D finished green. Merge it into main?"
    assert rec["implications"] == {"blocks": "the merge of run D",
                                   "if_unanswered": "the merge stays held"}
    assert rec["default_text"] == "DEFAULT: yes"
    assert rec["allow_free_text"] is True
    assert rec["answer"] is None and rec["handled"] is None and rec["delivery"] is None
    assert rec["asked_by"] == {"role": "orchestrator", "agent": ""}
    assert [h["status"] for h in rec["history"]] == ["open"]
    assert rec["history"][0]["by"] == ORCH
    if mode == "free_text":
        assert rec["options"] == []
    else:
        assert [o["id"] for o in rec["options"]] == ["yes", "no", "later"]
        assert rec["options"][0] == {"id": "yes", "label": "Yes",
                                     "description": "Go ahead", "implies": "merge runs"}
        assert rec["recommended"] == ["yes"]
    if mode == "multi":
        assert (rec["min"], rec["max"]) == (1, 2)
    assert_valid(rec)
    assert D.get(tree, rec["id"]) == rec


def test_c2_defaults(tmp_path):
    rec = D.create(make_tree(tmp_path), by=INIT, audience="user", topic="t", title="t",
                   context="c", implications={"blocks": "", "if_unanswered": ""},
                   mode="free_text")
    assert rec["allow_free_text"] is True and rec["default_text"] == ""
    assert rec["recommended"] == [] and rec["refs"] == [] and rec["options"] == []
    assert rec["asked_by"]["role"] == "initializer"
    assert_valid(rec)


def test_c2_asked_by_records_the_agent_of_an_agent_asker(tmp_path):
    rec = create(make_tree(tmp_path), "free_text", by=AGENT, audience="orchestrator")
    assert rec["asked_by"] == {"role": "agent", "agent": "ag-1"}


def test_c2_get_of_an_unknown_id_is_none(tmp_path):
    tree = make_tree(tmp_path)
    create(tree)
    assert D.get(tree, "d-00000000") is None
    assert D.get(tree, "q-nope00") is None
    assert D.get(tree, "") is None


def test_c2_ids_are_unique_and_survive_other_transitions(tmp_path):
    tree = make_tree(tmp_path)
    ids = [create(tree)["id"] for _ in range(25)]
    first = in_status(tree, "withdrawn")["id"]
    assert len(set(ids)) == 25 and first not in ids
    assert all(i.startswith("d-") for i in ids)


def test_c2_create_is_visible_to_another_handle_on_the_same_store(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree)
    from multiagents.tree import Tree
    other = Tree(tree.path, tree.events_path)
    assert D.get(other, rec["id"]) == rec


# ----------------------------------------------------- invalid records --

def _refused(tmp_path, field_ok, mode="single", **over):
    tree = make_tree(tmp_path)
    with pytest.raises(D.DecisionError) as err:
        D.create(tree, **kwargs(mode, **over))
    assert code_of(err.value) == "invalid_record"
    field = detail(err.value, "field")
    assert field_ok(field), f"field named {field!r}"
    assert D.list(tree)["decisions"] == [], "nothing may be stored"
    assert events(tree) == [], "no event for a refused record"


def _is(*names):
    return lambda field: field in names or any(
        isinstance(field, str) and field.startswith(n + "[") or
        isinstance(field, str) and field.startswith(n + ".") for n in names)


@pytest.mark.parametrize("topic", ["", "t" * 61, "two\nlines", "tab\rhere"])
def test_c2_invalid_topic(tmp_path, topic):
    _refused(tmp_path, _is("topic"), topic=topic)


@pytest.mark.parametrize("title", ["", "t" * 201, "two\nlines"])
def test_c2_invalid_title(tmp_path, title):
    _refused(tmp_path, _is("title"), title=title)


@pytest.mark.parametrize("context", ["", "c" * 20001])
def test_c2_invalid_context(tmp_path, context):
    _refused(tmp_path, _is("context"), context=context)


def test_c2_the_length_limits_are_inclusive(tmp_path):
    rec = create(make_tree(tmp_path), topic="t" * 60, title="t" * 200, context="c" * 20000)
    assert len(rec["topic"]) == 60 and len(rec["title"]) == 200
    assert len(rec["context"]) == 20000


def test_c2_the_first_offending_field_is_named(tmp_path):
    _refused(tmp_path, _is("topic"), topic="", context="")


@pytest.mark.parametrize("mode", ["radio", "", None])
def test_c2_invalid_mode(tmp_path, mode):
    _refused(tmp_path, _is("mode"), mode=mode)


@pytest.mark.parametrize("audience", ["everyone", "", None])
def test_c2_invalid_audience(tmp_path, audience):
    _refused(tmp_path, _is("audience"), audience=audience)


@pytest.mark.parametrize("options", [[], [{"id": "only", "label": "Only", "description": "",
                                           "implies": ""}]])
def test_c2_single_needs_two_options(tmp_path, options):
    _refused(tmp_path, _is("options"), options=options)


def test_c2_single_with_exactly_two_options_is_valid(tmp_path):
    rec = create(make_tree(tmp_path), options=kwargs()["options"][:2])
    assert len(rec["options"]) == 2


@pytest.mark.parametrize("lo,hi", [(0, 2), (2, 1), (1, 4), (3, 4), (-1, 2)])
def test_c2_multi_breaks_min_max_bounds(tmp_path, lo, hi):
    _refused(tmp_path, _is("min", "max"), "multi", min=lo, max=hi)


@pytest.mark.parametrize("lo,hi", [(None, None), (1, None), (None, 2)])
def test_c2_multi_needs_both_min_and_max(tmp_path, lo, hi):
    _refused(tmp_path, _is("min", "max"), "multi", min=lo, max=hi)


@pytest.mark.parametrize("lo,hi", [(1, 1), (1, 3), (3, 3), (2, 2)])
def test_c2_multi_bounds_that_are_valid(tmp_path, lo, hi):
    rec = create(make_tree(tmp_path), "multi", min=lo, max=hi)
    assert (rec["min"], rec["max"]) == (lo, hi)


def test_c2_free_text_mode_with_options_is_invalid(tmp_path):
    _refused(tmp_path, _is("options"), "free_text", options=kwargs()["options"])


def test_c2_duplicate_option_ids_are_invalid(tmp_path):
    opts = kwargs()["options"]
    opts[1]["id"] = opts[0]["id"]
    _refused(tmp_path, _is("options"), options=opts)


@pytest.mark.parametrize("bad", ["Yes", "-yes", "y" * 33, "", "a b"])
def test_c2_option_ids_must_match_the_pattern(tmp_path, bad):
    opts = kwargs()["options"]
    opts[0]["id"] = bad
    _refused(tmp_path, _is("options"), options=opts)


def test_c2_option_id_at_the_length_limit_is_valid(tmp_path):
    opts = kwargs()["options"]
    opts[0]["id"] = "y" * 32
    assert create(make_tree(tmp_path), options=opts)["options"][0]["id"] == "y" * 32


def test_c2_an_unknown_recommended_id_is_invalid(tmp_path):
    _refused(tmp_path, _is("recommended"), recommended=["maybe"])


def test_c2_single_recommends_at_most_one(tmp_path):
    _refused(tmp_path, _is("recommended"), recommended=["yes", "no"])


def test_c2_multi_may_recommend_several(tmp_path):
    rec = create(make_tree(tmp_path), "multi", recommended=["yes", "no", "later"])
    assert rec["recommended"] == ["yes", "no", "later"]


def test_c2_free_text_needs_allow_free_text(tmp_path):
    _refused(tmp_path, _is("allow_free_text"), "free_text", allow_free_text=False)


def test_c2_allow_free_text_false_is_kept_for_choice_modes(tmp_path):
    assert create(make_tree(tmp_path), allow_free_text=False)["allow_free_text"] is False


def test_c2_a_refused_create_does_not_consume_state_for_the_next(tmp_path):
    tree = make_tree(tmp_path)
    with pytest.raises(D.DecisionError):
        D.create(tree, **kwargs(topic=""))
    rec = create(tree)
    assert rec["revision"] == 1 and D.list(tree)["decisions"] == [rec]


# ---------------------------------------------------------- scrubbing (P4) --

def test_c2_p4_human_text_is_returned_and_stored_scrubbed(tmp_path):
    tree = make_tree(tmp_path)
    opts = kwargs()["options"]
    opts[0].update(label=f"use {TOKEN}", description=f"d {TOKEN}", implies=f"i {TOKEN}")
    rec = create(tree, topic=f"t {TOKEN}", title=f"ti {TOKEN}", context=f"ctx {TOKEN}",
                 implications={"blocks": f"b {TOKEN}", "if_unanswered": f"u {TOKEN}"},
                 options=opts)
    for stored in (rec, D.get(tree, rec["id"]), D.list(tree)["decisions"][0]):
        assert TOKEN not in str(stored)
        assert stored["topic"] == "t [redacted]" and stored["title"] == "ti [redacted]"
        assert stored["context"] == "ctx [redacted]"
        assert stored["implications"] == {"blocks": "b [redacted]",
                                          "if_unanswered": "u [redacted]"}
        assert stored["options"][0]["label"] == "use [redacted]"
        assert stored["options"][0]["description"] == "d [redacted]"
        assert stored["options"][0]["implies"] == "i [redacted]"
    assert TOKEN not in tree.path.read_text()
    assert TOKEN not in (tree.events_path.read_text() if tree.events_path.exists() else "")


def test_c2_p4_default_text_is_human_text_and_scrubbed(tmp_path):
    rec = create(make_tree(tmp_path), default_text=f"DEFAULT: {TOKEN}")
    assert TOKEN not in rec["default_text"]


def test_c2_p4_limits_apply_to_the_scrubbed_form(tmp_path):
    """Human text is scrubbed *before* validation: 20009 raw characters that
    scrub to 19996 are within the 20000 limit."""
    tree = make_tree(tmp_path)
    context = "x" * 19985 + " " + TOKEN
    assert len(context) > 20000
    rec = create(tree, context=context)
    assert len(rec["context"]) <= 20000 and "[redacted]" in rec["context"]
    title = "y" * 180 + " " + TOKEN
    assert len(title) > 200
    assert len(create(tree, title=title)["title"]) <= 200


def test_c2_p4_an_option_id_the_scrubber_would_alter_is_refused(tmp_path):
    opts = kwargs()["options"]
    opts[0]["id"] = TOKEN
    _refused(tmp_path, _is("options"), options=opts)


def test_c2_p4_a_recommended_id_the_scrubber_would_alter_is_refused(tmp_path):
    opts = kwargs()["options"]
    opts[0]["id"] = TOKEN
    # the option set itself is refused first or the recommendation is: either
    # way the refusal names a structural field and nothing is stored.
    _refused(tmp_path, _is("options", "recommended"), options=opts, recommended=[TOKEN])


@pytest.mark.parametrize("ref", [{"path": f"src/{TOKEN}", "sha256": "0" * 64},
                                 {"commit": TOKEN}])
def test_c2_p4_a_ref_the_scrubber_would_alter_is_refused(tmp_path, ref):
    _refused(tmp_path, _is("refs"), refs=[ref])


def test_c2_p4_structural_values_are_stored_exactly(tmp_path):
    ref = {"path": "docs/a-b_c.md", "sha256": "ab" * 32}
    rec = create(make_tree(tmp_path), "multi", recommended=["yes", "no"], refs=[ref, {"commit": "deadbeef"}])
    assert rec["refs"] == [ref, {"commit": "deadbeef"}]
    assert rec["recommended"] == ["yes", "no"]


# ---------------------------------------------------------------- listing --

def test_c2_list_shape_and_filters(tmp_path, clock):
    tree = make_tree(tmp_path)
    assert D.list(tree) == {"decisions": [], "next": None}
    a = create(tree, audience="user"); clock.tick()
    b = create(tree, audience="orchestrator", by=AGENT, mode="free_text", options=[]); clock.tick()
    c = in_status(tree, "withdrawn")
    page = D.list(tree)
    assert page["next"] is None
    assert {r["id"] for r in page["decisions"]} >= {a["id"], b["id"], c["id"]}
    assert [r["id"] for r in D.list(tree, audience="orchestrator")["decisions"]] == [b["id"]]
    assert {r["id"] for r in D.list(tree, status="withdrawn")["decisions"]} == {c["id"]}
    assert all(r["status"] == "open" for r in D.list(tree, status="open")["decisions"])
    assert {r["id"] for r in D.list(tree, status="open", audience="user")["decisions"]} >= {a["id"]}
    assert D.list(tree, status="handled")["decisions"] == []


def test_c2_open_first_oldest_first_then_others_most_recent_transition_first(tmp_path, clock):
    tree = make_tree(tmp_path)
    o1 = create(tree, topic="o1"); clock.tick()
    x1 = create(tree, topic="x1"); clock.tick()
    o2 = create(tree, topic="o2"); clock.tick()
    x2 = create(tree, topic="x2"); clock.tick()
    # x1 is answered, then x2 withdrawn later; then o1 is created-oldest still open
    D.answer(tree, x1["id"], revision=1, by=USER, options=["yes"]); clock.tick()
    D.withdraw(tree, x2["id"], revision=1, by=ORCH, reason="gone"); clock.tick()
    order = [r["id"] for r in D.list(tree)["decisions"]]
    assert order == [o1["id"], o2["id"], x2["id"], x1["id"]]


def test_c2_a_later_transition_moves_a_closed_decision_to_the_front_of_the_others(tmp_path, clock):
    tree = make_tree(tmp_path)
    a = create(tree); clock.tick()
    b = create(tree); clock.tick()
    D.withdraw(tree, a["id"], revision=1, by=ORCH, reason="r"); clock.tick()
    D.withdraw(tree, b["id"], revision=1, by=ORCH, reason="r"); clock.tick()
    assert [r["id"] for r in D.list(tree)["decisions"]] == [b["id"], a["id"]]
    rec = D.answer(tree, create(tree)["id"], revision=1, by=USER, options=["yes"])["decision"]
    clock.tick()
    # marking it handled makes it the most recent transition of all
    D.mark_handled(tree, rec["id"], revision=rec["revision"], by=ORCH, outcome="applied")
    assert D.list(tree)["decisions"][0]["id"] == rec["id"]


def test_c2_escalation_does_not_change_the_open_order_or_count(tmp_path, clock):
    tree = make_tree(tmp_path)
    a = create(tree, audience="orchestrator", by=AGENT); clock.tick()
    b = create(tree, audience="orchestrator", by=AGENT); clock.tick()
    D.escalate(tree, a["id"], revision=1, by=ORCH)
    page = D.list(tree)["decisions"]
    assert [r["id"] for r in page] == [a["id"], b["id"]]


@pytest.mark.parametrize("limit", [1, 50, 200])
def test_c2_limit_within_bounds(tmp_path, limit):
    tree = make_tree(tmp_path)
    for _ in range(3):
        create(tree)
    page = D.list(tree, limit=limit)
    assert len(page["decisions"]) == min(limit, 3)


@pytest.mark.parametrize("limit", [0, -1, 201, 10_000])
def test_c2_limit_out_of_bounds_is_refused(tmp_path, limit):
    tree = make_tree(tmp_path)
    create(tree)
    with pytest.raises((D.DecisionError, ValueError)):
        D.list(tree, limit=limit)


def test_c2_default_limit_is_fifty(tmp_path):
    tree = make_tree(tmp_path)
    for _ in range(52):
        create(tree)
    page = D.list(tree)
    assert len(page["decisions"]) == 50 and page["next"] is not None
    rest = D.list(tree, cursor=page["next"])
    assert len(rest["decisions"]) == 2 and rest["next"] is None


def _walk(tree, limit, **filters):
    seen, cursor = [], None
    for _ in range(100):
        page = D.list(tree, limit=limit, cursor=cursor, **filters)
        seen += [r["id"] for r in page["decisions"]]
        cursor = page["next"]
        if cursor is None:
            return seen
    raise AssertionError("pagination did not end")


def test_c2_paging_covers_everything_once_with_no_changes(tmp_path, clock):
    tree = make_tree(tmp_path)
    ids = []
    for i in range(7):
        ids.append(create(tree)["id"]); clock.tick()
    in_status(tree, "withdrawn"); in_status(tree, "answered")
    full = [r["id"] for r in D.list(tree, limit=200)["decisions"]]
    assert _walk(tree, 3) == full
    assert _walk(tree, 1) == full
    assert len(full) == len(set(full)) == 9


def test_c2_p8_ties_on_time_break_by_id_without_duplicates_or_gaps(tmp_path, clock):
    tree = make_tree(tmp_path)
    ids = [create(tree)["id"] for _ in range(7)]       # all asked at the same instant
    got = _walk(tree, 2)
    assert got == sorted(ids)


def test_c2_p8_a_transition_between_pages_causes_no_gap(tmp_path, clock):
    """Offset paging would skip d4 here: answering d1 moves it behind the open
    group and shifts everything after it up by one."""
    tree = make_tree(tmp_path)
    ids = []
    for _ in range(6):
        ids.append(create(tree)["id"]); clock.tick()
    first = D.list(tree, limit=3)
    assert [r["id"] for r in first["decisions"]] == ids[:3]
    D.answer(tree, ids[0], revision=1, by=USER, options=["yes"]); clock.tick()
    rest, cursor = [], first["next"]
    while cursor is not None:
        page = D.list(tree, limit=3, cursor=cursor)
        rest += [r["id"] for r in page["decisions"]]
        cursor = page["next"]
    for unchanged in ids[1:]:
        assert (ids[:3] + rest).count(unchanged) == 1, unchanged
    assert (ids[:3] + rest).count(ids[0]) <= 2
    assert rest.count(ids[0]) <= 1


def test_c2_p8_records_added_between_pages_cause_no_duplicate_or_gap(tmp_path, clock):
    tree = make_tree(tmp_path)
    ids = []
    for _ in range(6):
        ids.append(create(tree)["id"]); clock.tick()
    first = D.list(tree, limit=2)
    seen = [r["id"] for r in first["decisions"]]
    cursor = first["next"]
    added = []
    while cursor is not None:
        added.append(create(tree, topic="late")["id"]); clock.tick()
        page = D.list(tree, limit=2, cursor=cursor)
        seen += [r["id"] for r in page["decisions"]]
        cursor = page["next"]
    assert len(seen) == len(set(seen)), "a record was served twice"
    assert set(ids) <= set(seen), "an untouched record was skipped"
    assert set(seen) - set(ids) <= set(added)


def test_c2_p8_a_closed_group_page_is_stable_when_an_open_one_is_added(tmp_path, clock):
    tree = make_tree(tmp_path)
    closed = []
    for _ in range(5):
        r = in_status(tree, "withdrawn")
        closed.append(r["id"]); clock.tick()
    first = D.list(tree, limit=2, status="withdrawn")
    create(tree)
    D.withdraw(tree, create(tree)["id"], revision=1, by=ORCH, reason="x")
    seen = [r["id"] for r in first["decisions"]]
    cursor = first["next"]
    while cursor is not None:
        page = D.list(tree, limit=2, cursor=cursor, status="withdrawn")
        seen += [r["id"] for r in page["decisions"]]
        cursor = page["next"]
    assert len(seen) == len(set(seen))
    assert set(closed) <= set(seen)


def test_c2_p8_a_cursor_is_an_opaque_string(tmp_path):
    tree = make_tree(tmp_path)
    for _ in range(3):
        create(tree)
    assert isinstance(D.list(tree, limit=1)["next"], str)


@pytest.mark.parametrize("first,second", [
    ({}, {"status": "open"}),
    ({"status": "open"}, {}),
    ({"status": "open"}, {"status": "answered"}),
    ({}, {"audience": "user"}),
    ({"audience": "user"}, {"audience": "orchestrator"}),
])
def test_c2_p8_a_cursor_reused_with_other_filters_is_refused(tmp_path, first, second):
    tree = make_tree(tmp_path)
    for _ in range(4):
        create(tree)
    cursor = D.list(tree, limit=1, **first)["next"]
    with pytest.raises(D.DecisionError) as err:
        D.list(tree, limit=1, cursor=cursor, **second)
    assert code_of(err.value) == "invalid_cursor"


@pytest.mark.parametrize("cursor", ["garbage", "!!!", "e30=", "x" * 5000])
def test_c2_p8_a_malformed_cursor_is_refused(tmp_path, cursor):
    tree = make_tree(tmp_path)
    create(tree)
    with pytest.raises(D.DecisionError) as err:
        D.list(tree, cursor=cursor)
    assert code_of(err.value) == "invalid_cursor"


def test_c2_p8_the_same_cursor_may_be_used_again(tmp_path):
    tree = make_tree(tmp_path)
    for _ in range(4):
        create(tree)
    cursor = D.list(tree, limit=2)["next"]
    assert D.list(tree, limit=2, cursor=cursor) == D.list(tree, limit=2, cursor=cursor)
