"""Adversary, user-decisions slice 3 (UD-C12): what the wake report says.

A free-text decision may be answered with free text AND a comment (slice 1's
`decisions.answer` accepts both). The report must carry the answer itself; a
line that shows only the comment tells the orchestrator the user answered
"(free text)" and never says what they answered.
"""
from support.ud_s3_support import SESSION, answered_decision, bind, stored, tree_of, watch
from test_dj_seams import live_pid  # noqa: F401  (fixture)
from test_dj_support import jobs  # noqa: F401  (fixture)


def test_adv_a_free_text_answer_with_a_comment_still_reports_the_free_text(jobs, live_pid):
    tree = tree_of(jobs)
    rec = answered_decision(tree, mode="free_text", free_text="ship it on Friday",
                            comment="only if CI is green")
    assert rec["answer"]["free_text"] == "ship it on Friday"   # stored as given
    bind(jobs, live_pid.pid)
    result = watch(jobs)
    assert result.returncode == 2, result.stderr
    [line] = [l for l in result.stderr.splitlines() if rec["id"] in l]
    # the delivery is recorded, so this is the only time the session is told
    assert stored(tree, rec["id"])["delivery"]["session"] == SESSION
    assert "ship it on Friday" in line, line
