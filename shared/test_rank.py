"""Tests for Tier 1 over a stream.

Two properties matter more than the ranking itself.

**It must never withhold a job.** Only an item the model calls outright
irrelevant is held back; everything else goes out with a score. A near-miss
costs the reader two seconds, a missed job is the thing the watch exists to
prevent, and a confident filter is worse than a vague one.

**It must never block a notification.** Slow, malformed, over budget, down --
whatever happens, the items go out. An unranked email is a small
disappointment; a missing one is a missed job.
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cost  # noqa: E402
import rank as rank_mod  # noqa: E402
from rank import rank  # noqa: E402

REQUEST = "a student job for a cloud engineer in Beer Sheva"

ITEMS = [
    {"id": "a", "text": "Senior DevOps Engineer, Elbit, Be'er Sheva", "href": "/1"},
    {"id": "b", "text": "Student Cloud Engineer, Amdocs, Be'er Sheva", "href": "/2"},
    {"id": "c", "text": "Barista, Cafe Greg, Tel Aviv", "href": "/3"},
]


def scripted(payload=None, *, fail=False):
    """A client returning one JSON reply, or exploding."""
    def create(**_kwargs):
        if fail:
            raise RuntimeError("overloaded")
        import json
        block = SimpleNamespace(type="text", text=json.dumps(payload))
        return SimpleNamespace(content=[block])

    return SimpleNamespace(messages=SimpleNamespace(create=create))


VERDICTS = {"items": [
    {"n": 1, "score": 5, "why": "senior, not a student role", "keep": True},
    {"n": 2, "score": 10, "why": "student cloud role in Beer Sheva", "keep": True},
    {"n": 3, "score": 0, "why": "hospitality, not engineering", "keep": False},
]}


def test_the_best_match_comes_first():
    ranked, _ = rank(REQUEST, ITEMS, client=scripted(VERDICTS))

    assert [i["id"] for i in ranked] == ["b", "a"]
    assert ranked[0]["score"] == 10
    assert "student" in ranked[0]["why"]


def test_only_an_outright_irrelevant_item_is_held_back():
    """A junior role when they wanted senior is still reported, with a low
    score. Only the barista goes."""
    ranked, _ = rank(REQUEST, ITEMS, client=scripted(VERDICTS))

    assert "c" not in [i["id"] for i in ranked]
    assert "a" in [i["id"] for i in ranked]


def test_the_original_fields_survive():
    """The link is the point of the whole email."""
    ranked, _ = rank(REQUEST, ITEMS, client=scripted(VERDICTS))
    assert ranked[0]["href"] == "/2"
    assert ranked[0]["id"] == "b"


def test_a_model_failure_sends_everything_unranked():
    """The single most important property here. An unranked email is a small
    disappointment; a missing one is a missed job."""
    ranked, spend = rank(REQUEST, ITEMS, client=scripted(fail=True))

    assert ranked == ITEMS
    assert spend == 0.0


def test_a_malformed_reply_sends_everything_unranked_and_still_costs():
    """The items survive in their original order -- but the call was made and
    the tokens were spent, so the budget must hear about it. Not charging for
    a model that answers uselessly would let it burn the allowance invisibly."""
    ranked, spend = rank(REQUEST, ITEMS, client=scripted({"nonsense": True}))

    assert [i["id"] for i in ranked] == ["a", "b", "c"]
    assert all("score" not in i for i in ranked)
    assert spend > 0


def test_an_item_the_model_skipped_is_reported_rather_than_lost():
    """Silence about an item is not a verdict on it."""
    partial = {"items": [{"n": 2, "score": 9, "why": "matches", "keep": True}]}
    ranked, _ = rank(REQUEST, ITEMS, client=scripted(partial))

    assert {i["id"] for i in ranked} == {"a", "b", "c"}
    assert ranked[0]["id"] == "b"          # judged, best first
    assert "score" not in ranked[1]         # unjudged, trailing


def test_a_nonsense_score_does_not_crash_the_ordering():
    weird = {"items": [
        {"n": 1, "score": "very good", "keep": True},
        {"n": 2, "score": 99, "keep": True},
        {"n": 3, "score": -5, "keep": True},
    ]}
    ranked, _ = rank(REQUEST, ITEMS, client=scripted(weird))

    assert [i["score"] for i in ranked] == [10, 0, 0]


def test_nothing_to_rank_costs_nothing():
    for items in ([], None):
        assert rank(REQUEST, items or [], client=scripted(VERDICTS)) == (items or [], 0.0)


def test_no_request_means_no_criteria_to_judge_against():
    assert rank("", ITEMS, client=scripted(VERDICTS)) == (ITEMS, 0.0)


def test_a_huge_batch_is_capped_but_nothing_is_dropped():
    """The cap bounds the call, it does not bound the email."""
    many = [{"id": str(n), "text": f"Engineer {n}", "href": f"/{n}"}
            for n in range(rank_mod.MAX_ITEMS + 7)]
    verdicts = {"items": [{"n": n, "score": 5, "keep": True}
                          for n in range(1, rank_mod.MAX_ITEMS + 1)]}

    ranked, _ = rank(REQUEST, many, client=scripted(verdicts))

    assert len(ranked) == len(many)


# --------------------------------------------------------------------------
# What it costs, which is the whole reason this is allowed to exist
# --------------------------------------------------------------------------

def test_ranking_is_paid_per_notification_not_per_check():
    """Phase 8b took a model out of every tick and that must stay out. A jobs
    watch at 15 minutes firing twice a day spends about 19 cents a month;
    judging every tick would spend over sixteen dollars."""
    per_fire = cost.rank_cost(10)
    monthly_if_ranked_per_fire = per_fire * 60          # twice a day
    monthly_if_ranked_per_tick = cost.judge_cost() * 2880

    assert monthly_if_ranked_per_fire < 0.25
    assert monthly_if_ranked_per_tick > 15
    assert monthly_if_ranked_per_tick / monthly_if_ranked_per_fire > 50


def test_the_spend_reported_matches_the_cost_model():
    _, spend = rank(REQUEST, ITEMS, client=scripted(VERDICTS))
    assert spend == cost.rank_cost(len(ITEMS))


def test_the_budget_gate_shares_the_one_budget():
    """Same $5 as checks and repairs. One guarantee, not three."""
    assert cost.can_afford_rank(15, spend_usd=0.0)
    assert not cost.can_afford_rank(15, spend_usd=cost.monthly_budget_usd())
