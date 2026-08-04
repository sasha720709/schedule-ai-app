"""Tests for questions built from what a search actually returned.

Two properties carry the design.

**A question must split the list.** One that every result answers the same way
tells the user they have a choice when they do not, and a form nobody needed is
worse than no questions at all.

**Answers must not become a filter on the future.** They were given about
today's postings. Turning them into a rule for tomorrow's is the bug class this
codebase keeps rediscovering -- the too-strict filter that counts zero forever
while looking healthy. `as_criteria` produces a *preference* for the ranker,
which scores rather than excludes.
"""

import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import questions  # noqa: E402
from questions import as_criteria, build  # noqa: E402

REQUEST = "a student job for a cloud engineer in Beer Sheva"

ITEMS = [
    {"id": "a", "text": "Senior DevOps Engineer, Elbit, Be'er Sheva"},
    {"id": "b", "text": "Student Cloud Engineer, Amdocs, Be'er Sheva"},
    {"id": "c", "text": "Junior Cloud Engineer, Leidos, Tel Aviv"},
]

REPLY = {"questions": [{
    "id": "seniority",
    "question": "Which level suits you?",
    "options": [
        {"value": "junior", "label": "Junior or student", "items": [2, 3]},
        {"value": "senior", "label": "Senior", "items": [1]},
    ],
}]}


def scripted(payload=None, *, fail=False):
    def create(**_kwargs):
        if fail:
            raise RuntimeError("overloaded")
        block = SimpleNamespace(type="text", text=json.dumps(payload))
        return SimpleNamespace(content=[block])

    return SimpleNamespace(messages=SimpleNamespace(create=create))


def test_item_numbers_become_the_stable_ids_everything_else_uses():
    """So narrowing the list later is an exact set operation rather than
    re-matching text that may have been truncated or reworded."""
    built, _ = build(REQUEST, ITEMS, client=scripted(REPLY))

    assert built[0]["options"][0]["items"] == ["b", "c"]
    assert built[0]["options"][1]["items"] == ["a"]


def test_a_question_that_splits_nothing_is_dropped():
    """Every option covering every item is not a choice."""
    useless = {"questions": [{
        "id": "city", "question": "Which city?",
        "options": [
            {"value": "bs", "label": "Be'er Sheva", "items": [1, 2, 3]},
            {"value": "any", "label": "Anywhere", "items": [1, 2, 3]},
        ],
    }]}
    assert build(REQUEST, ITEMS, client=scripted(useless))[0] == []


def test_a_question_with_one_usable_option_is_dropped():
    lonely = {"questions": [{
        "id": "x", "question": "Which?",
        "options": [{"value": "a", "label": "Only this", "items": [1]}],
    }]}
    assert build(REQUEST, ITEMS, client=scripted(lonely))[0] == []


def test_an_option_matching_nothing_is_dropped():
    """A model may offer an option it cannot point at. It must not become a
    button that empties the list."""
    reply = {"questions": [{
        "id": "x", "question": "Which?",
        "options": [
            {"value": "a", "label": "Real", "items": [1]},
            {"value": "b", "label": "Imagined", "items": []},
        ],
    }]}
    assert build(REQUEST, ITEMS, client=scripted(reply))[0] == []


def test_item_numbers_outside_the_list_are_ignored():
    reply = {"questions": [{
        "id": "x", "question": "Which?",
        "options": [
            {"value": "a", "label": "One", "items": [1, 99, -3, "nine"]},
            {"value": "b", "label": "Two", "items": [2]},
        ],
    }]}
    built, _ = build(REQUEST, ITEMS, client=scripted(reply))

    assert built[0]["options"][0]["items"] == ["a"]


def test_no_questions_is_a_good_answer_not_a_failure():
    """The results may genuinely be all of a kind."""
    built, spend = build(REQUEST, ITEMS, client=scripted({"questions": []}))
    assert built == []
    assert spend > 0          # the call happened; the tokens were real


def test_a_model_failure_never_blocks_creating_a_watch():
    built, spend = build(REQUEST, ITEMS, client=scripted(fail=True))
    assert built == []
    assert spend == 0.0


def test_a_malformed_reply_is_survived():
    for payload in ({"questions": "yes"}, {"questions": [7, None]}, {}):
        assert build(REQUEST, ITEMS, client=scripted(payload))[0] == []


def test_nothing_found_means_nothing_to_ask_about():
    assert build(REQUEST, [], client=scripted(REPLY)) == ([], 0.0)
    assert build("", ITEMS, client=scripted(REPLY)) == ([], 0.0)


def test_at_most_three_questions_survive():
    """More than three is a form, and a form does not get filled in."""
    many = {"questions": [
        {"id": f"q{n}", "question": f"Question {n}?",
         "options": [{"value": "a", "label": "A", "items": [1]},
                     {"value": "b", "label": "B", "items": [2]}]}
        for n in range(6)
    ]}
    built, _ = build(REQUEST, ITEMS, client=scripted(many))
    assert len(built) == questions.MAX_QUESTIONS


# --------------------------------------------------------------------------
# Answers become preferences, never rules
# --------------------------------------------------------------------------

def test_answers_read_back_as_a_sentence_for_the_ranker():
    built, _ = build(REQUEST, ITEMS, client=scripted(REPLY))
    criteria = as_criteria(built, {"seniority": ["junior"]})

    assert "Which level suits you?" in criteria
    assert "Junior or student" in criteria
    assert "Senior" not in criteria


def test_several_choices_on_one_question_are_joined():
    built, _ = build(REQUEST, ITEMS, client=scripted(REPLY))
    criteria = as_criteria(built, {"seniority": ["junior", "senior"]})

    assert "Junior or student or Senior" in criteria


def test_a_single_value_is_accepted_as_well_as_a_list():
    built, _ = build(REQUEST, ITEMS, client=scripted(REPLY))
    assert "Junior" in as_criteria(built, {"seniority": "junior"})


def test_no_answer_produces_no_criteria():
    """An unanswered question is not a constraint. Confirming without
    answering must behave exactly as it did before questions existed."""
    built, _ = build(REQUEST, ITEMS, client=scripted(REPLY))

    assert as_criteria(built, {}) == ""
    assert as_criteria(built, {"seniority": []}) == ""
    assert as_criteria([], {"seniority": ["junior"]}) == ""


def test_an_answer_to_a_question_that_no_longer_exists_is_ignored():
    built, _ = build(REQUEST, ITEMS, client=scripted(REPLY))
    assert as_criteria(built, {"deleted_question": ["x"]}) == ""


def test_garbage_answers_do_not_raise():
    built, _ = build(REQUEST, ITEMS, client=scripted(REPLY))
    for junk in (None, "text", 7, []):
        assert as_criteria(built, junk) == ""


# --------------------------------------------------------------------------
# Which items survive the answers
#
# For a price watch this is not a nicety. The cheapest thing a shop lists for
# "xbox series x" is a headset, so a watch that does not pin the product is
# confidently wrong about money.
# --------------------------------------------------------------------------

def test_answers_narrow_to_an_exact_set_of_items():
    built, _ = build(REQUEST, ITEMS, client=scripted(REPLY))
    assert questions.chosen_ids(built, {"seniority": ["junior"]}) == {"b", "c"}


def test_two_answered_questions_intersect():
    """Each question narrows further, rather than each adding more."""
    two = {"questions": [
        {"id": "level", "question": "Level?",
         "options": [{"value": "junior", "label": "Junior", "items": [2, 3]},
                     {"value": "senior", "label": "Senior", "items": [1]}]},
        {"id": "city", "question": "City?",
         "options": [{"value": "bs", "label": "Be'er Sheva", "items": [1, 2]},
                     {"value": "tlv", "label": "Tel Aviv", "items": [3]}]},
    ]}
    built, _ = build(REQUEST, ITEMS, client=scripted(two))

    assert questions.chosen_ids(
        built, {"level": ["junior"], "city": ["bs"]}) == {"b"}


def test_several_options_on_one_question_are_a_union():
    built, _ = build(REQUEST, ITEMS, client=scripted(REPLY))
    assert questions.chosen_ids(
        built, {"seniority": ["junior", "senior"]}) == {"a", "b", "c"}


def test_no_answer_means_no_preference_not_an_empty_choice():
    """The two must not be confused. No preference leaves the watch exactly as
    it was; an empty result is a real answer that happens to match nothing."""
    built, _ = build(REQUEST, ITEMS, client=scripted(REPLY))

    assert questions.chosen_ids(built, {}) is None
    assert questions.chosen_ids(built, {"seniority": []}) is None
    assert questions.chosen_ids([], {"seniority": ["junior"]}) is None


def test_an_answer_that_matches_nothing_is_an_empty_set_not_none():
    built, _ = build(REQUEST, ITEMS, client=scripted(REPLY))
    assert questions.chosen_ids(built, {"seniority": ["nonexistent"]}) == set()


def test_garbage_answers_do_not_raise_here_either():
    built, _ = build(REQUEST, ITEMS, client=scripted(REPLY))
    for junk in (None, "text", 7, []):
        assert questions.chosen_ids(built, junk) is None
