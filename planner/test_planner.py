"""Tests for plan-time compilation and verification.

The behaviour worth protecting: **a spec that does not reproduce the value it
was compiled from is never stored.** Everything downstream trusts the stored
extractor for months without a model ever looking at the page again, so a
plausible-looking selector that was never actually run is the most expensive
mistake this system can make.

Loaded by explicit path, since `api/handler.py` and `checker/handler.py` also
exist and a bare `import handler` would resolve to whichever came first.
"""

import importlib.util
import json
import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

if "anthropic" not in sys.modules:
    _anthropic = types.ModuleType("anthropic")
    _anthropic.Anthropic = MagicMock()
    sys.modules["anthropic"] = _anthropic

if not hasattr(sys.modules.get("boto3", object()), "resource"):
    _boto3 = types.ModuleType("boto3")
    _boto3.resource = MagicMock()
    _boto3.client = MagicMock()
    sys.modules["boto3"] = _boto3

os.environ.setdefault("WATCHES_TABLE", "watches")
os.environ.setdefault("WATCH_TARGETS_TABLE", "targets")
os.environ.setdefault("FETCHER_FUNCTION_ARN", "arn:aws:lambda:::function:fetcher")

sys.path.insert(0, os.path.join(_ROOT, "shared"))
sys.path.insert(0, _HERE)

_spec = importlib.util.spec_from_file_location(
    "planner_plan", os.path.join(_HERE, "plan.py"))
plan_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plan_mod)


PAGE = """
<html><body>
  <div class="sku" data-model="512">
    <span class="title">Steam Deck 512 GB OLED</span>
    <span class="price">$789.00</span>
  </div>
  <div class="sku" data-model="1tb">
    <span class="title">Steam Deck 1TB OLED</span>
    <span class="price">$949.00</span>
  </div>
</body></html>
"""

CONDITION = {"metric": "price", "op": "<", "value": 700}


class ScriptedClient:
    """An Anthropic client that returns a queued list of JSON replies."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.prompts.append(kwargs)
        payload = self.replies.pop(0)
        block = SimpleNamespace(type="text", text=json.dumps(payload))
        return SimpleNamespace(content=[block])


GOOD_SPEC = {"scope": '[data-model="512"]', "kind": "css",
             "selector": ".price", "parse": "currency",
             "pattern": None, "path": None, "attribute": None,
             "unavailable_if": None}


def test_a_working_spec_is_verified_and_returned():
    client = ScriptedClient(
        {"literal": "$789.00", "note": "found it"},
        GOOD_SPEC,
    )
    built = plan_mod.build_extractor(
        "https://example.com", "the 512GB price", CONDITION, PAGE, client=client)

    assert built["verified_value"] == 789.00
    assert built["verified_raw"] == "$789.00"
    # Nulls the model filled in are dropped rather than stored and revalidated.
    assert "pattern" not in built["extractor"]
    assert built["extractor"]["scope"] == '[data-model="512"]'


def test_a_spec_that_does_not_reproduce_the_value_is_retried_with_the_reason():
    """The first selector points at the wrong SKU's box, so it reads 949 for a
    value the page shows as $789.00. That has to be caught here, not in
    production three weeks later."""
    wrong = {**GOOD_SPEC, "scope": '[data-model="1tb"]', "selector": ".title"}
    client = ScriptedClient(
        {"literal": "$789.00", "note": "found it"},
        wrong,
        GOOD_SPEC,
    )
    built = plan_mod.build_extractor(
        "https://example.com", "the 512GB price", CONDITION, PAGE, client=client)

    assert built["verified_value"] == 789.00
    # The retry was told what went wrong rather than asked again blindly.
    retry_prompt = client.prompts[-1]["messages"][0]["content"]
    assert "A previous attempt failed" in retry_prompt


def test_a_spec_that_never_works_is_refused_rather_than_stored():
    broken = {**GOOD_SPEC, "scope": ".nonexistent"}
    client = ScriptedClient(
        {"literal": "$789.00", "note": "found it"}, broken, broken)

    with pytest.raises(ValueError, match="could not compile"):
        plan_mod.build_extractor(
            "https://example.com", "the price", CONDITION, PAGE, client=client)


def test_a_malformed_spec_is_fed_back_rather_than_crashing():
    client = ScriptedClient(
        {"literal": "$789.00", "note": "found it"},
        {"kind": "telepathy", "selector": ".price"},
        GOOD_SPEC,
    )
    built = plan_mod.build_extractor(
        "https://example.com", "the price", CONDITION, PAGE, client=client)
    assert built["verified_value"] == 789.00
    assert "malformed" in client.prompts[-1]["messages"][0]["content"]


def test_a_page_with_nothing_to_read_is_rejected_before_compiling():
    """No compile call is made at all -- there is nothing to anchor it to."""
    client = ScriptedClient({"literal": None, "note": "page is a login wall"})

    with pytest.raises(ValueError, match="nothing to watch"):
        plan_mod.build_extractor(
            "https://example.com", "the price", CONDITION, PAGE, client=client)
    assert len(client.prompts) == 1


def test_only_the_markup_around_the_value_is_sent_to_the_compiler():
    """The whole reason for three calls rather than one. A 1.5MB page is
    ~375,000 tokens; the fragments that matter are a few hundred characters."""
    big = PAGE + "<div>filler</div>" * 50000
    client = ScriptedClient({"literal": "$789.00", "note": "ok"}, GOOD_SPEC)
    plan_mod.build_extractor(
        "https://example.com", "the price", CONDITION, big, client=client)

    compile_prompt = client.prompts[-1]["messages"][0]["content"]
    assert len(big) > 800_000
    assert len(compile_prompt) < 12_000
    assert "$789.00" in compile_prompt


def test_a_count_spec_verifies_through_the_same_path():
    """Vacancy-shaped watches are compiled and verified like any other."""
    jobs = ('<div id="list"><a>Go Engineer</a><a>Rust Engineer</a></div>')
    client = ScriptedClient(
        {"literal": "Rust Engineer", "note": "one listing"},
        {"scope": "#list", "kind": "count",
         "selector": 'a:-soup-contains("Rust")', "parse": "int"},
    )
    built = plan_mod.build_extractor(
        "https://jobs.example", "rust roles", {"op": ">=", "value": 1},
        jobs, client=client)
    assert built["verified_value"] == 1


def test_tidy_keeps_only_recognised_keys():
    tidied = plan_mod._tidy(
        {"kind": "css", "selector": ".p", "confidence": 0.9, "scope": None})
    assert tidied == {"kind": "css", "selector": ".p"}


# --- presence watches: the thing is not there yet, and that is the point ------
#
# Found by the owner running a real request: "tell me when a student job vacancy
# for cloud engineer appears in Beer Sheva" failed to plan at all. The Planner
# demanded a literal value on the page before it would compile anything, so a
# watch for something that has not happened yet could never be created -- and
# the `count` kind was unreachable in exactly the case it was added for.

JOBS_PAGE = """
<html><body>
  <table id="results">
    <tr class="job"><td><a class="title">Senior DevOps Engineer, Tel Aviv</a></td></tr>
    <tr class="job"><td><a class="title">QA Automation Student, Beer Sheva</a></td></tr>
  </table>
</body></html>
"""

VACANCY_CONDITION = {"metric": "matching_vacancies", "op": ">=", "value": 1}

COUNT_SPEC = {
    "scope": "#results", "kind": "count",
    "selector": 'a.title:-soup-contains("Cloud Engineer")', "parse": "int",
}


def test_a_vacancy_that_does_not_exist_yet_still_produces_a_plan():
    """The regression. Counting zero is a passing verification, not a failure."""
    client = ScriptedClient(
        {"literal": None, "sample": "QA Automation Student, Beer Sheva",
         "note": "no cloud engineer roles listed today"},
        COUNT_SPEC,
    )
    built = plan_mod.build_extractor(
        "https://jobs.example", "student cloud engineer roles in Beer Sheva",
        VACANCY_CONDITION, JOBS_PAGE, shape="presence", client=client)

    assert built["verified_value"] == 0
    assert built["extractor"]["kind"] == "count"
    assert built["extractor"]["scope"] == "#results"


def test_a_presence_watch_anchors_on_a_neighbour_not_on_the_thing_wanted():
    """The compiler is shown another listing, and told plainly that it is not
    what the user asked for -- otherwise it would compile a spec for the
    neighbour."""
    client = ScriptedClient(
        {"literal": None, "sample": "QA Automation Student, Beer Sheva", "note": ""},
        COUNT_SPEC,
    )
    plan_mod.build_extractor(
        "https://jobs.example", "cloud engineer roles", VACANCY_CONDITION,
        JOBS_PAGE, shape="presence", client=client)

    prompt = client.prompts[-1]["messages"][0]["content"]
    assert "QA Automation Student" in prompt
    assert "neighbour" in prompt
    assert client.prompts[-1]["system"] is plan_mod.COUNT_PROMPT


def test_a_presence_watch_uses_a_matching_item_as_the_anchor_when_one_exists():
    """If a matching role happens to be posted today, its markup is just as
    good a guide to the list -- and the spec compiled is still a count, so the
    watch keeps working after that role is filled."""
    client = ScriptedClient(
        {"literal": "Cloud Engineer Student, Beer Sheva",
         "sample": "QA Automation Student, Beer Sheva", "note": "one match"},
        COUNT_SPEC,
    )
    page = JOBS_PAGE.replace("QA Automation Student, Beer Sheva",
                             "Cloud Engineer Student, Beer Sheva")
    built = plan_mod.build_extractor(
        "https://jobs.example", "cloud engineer roles", VACANCY_CONDITION,
        page, shape="presence", client=client)

    assert built["extractor"]["kind"] == "count"
    assert built["verified_value"] == 1
    assert "Cloud Engineer Student" in client.prompts[-1]["messages"][0]["content"]


def test_a_presence_watch_is_rejected_only_when_the_page_lists_nothing_at_all():
    client = ScriptedClient(
        {"literal": None, "sample": None, "note": "the search returned no rows"})
    with pytest.raises(ValueError, match="lists nothing that could be counted"):
        plan_mod.build_extractor(
            "https://jobs.example", "cloud engineer roles", VACANCY_CONDITION,
            JOBS_PAGE, shape="presence", client=client)


def test_a_value_watch_with_no_value_says_what_to_do_about_it():
    """The old message was "nothing to watch", which is wrong and unactionable
    when the user is waiting for the thing to appear."""
    client = ScriptedClient({"literal": None, "sample": "Some other product",
                             "note": "out of stock"})
    with pytest.raises(ValueError, match="presence watch"):
        plan_mod.build_extractor(
            "https://shop.example", "the price", CONDITION, PAGE,
            shape="value", client=client)


def test_a_broken_count_spec_is_retried_with_list_specific_feedback():
    client = ScriptedClient(
        {"literal": None, "sample": "QA Automation Student, Beer Sheva", "note": ""},
        {**COUNT_SPEC, "scope": "#nonexistent"},
        COUNT_SPEC,
    )
    built = plan_mod.build_extractor(
        "https://jobs.example", "cloud engineer roles", VACANCY_CONDITION,
        JOBS_PAGE, shape="presence", client=client)

    assert built["verified_value"] == 0
    assert "list container" in client.prompts[-1]["messages"][0]["content"]


def test_a_value_watch_is_unaffected_by_the_presence_path():
    """Regression guard: the price flow still takes the original branch."""
    client = ScriptedClient(
        {"literal": "$789.00", "sample": None, "note": "found it"}, GOOD_SPEC)
    built = plan_mod.build_extractor(
        "https://example.com", "the 512GB price", CONDITION, PAGE, client=client)

    assert built["verified_value"] == 789.00
    assert client.prompts[-1]["system"] is plan_mod.COMPILE_PROMPT


def test_a_count_selector_that_could_never_match_is_not_accepted():
    """The failure this guards against is invisible: a wrong item class counts
    zero today, counts zero forever, and never reports a fault."""
    doomed = {**COUNT_SPEC, "selector": 'a.headline:-soup-contains("Cloud Engineer")'}
    client = ScriptedClient(
        {"literal": None, "sample": "QA Automation Student, Beer Sheva", "note": ""},
        doomed,
        COUNT_SPEC,
    )
    built = plan_mod.build_extractor(
        "https://jobs.example", "cloud engineer roles", VACANCY_CONDITION,
        JOBS_PAGE, shape="presence", client=client)

    assert built["extractor"] == COUNT_SPEC
    assert "can never match" in client.prompts[-1]["messages"][0]["content"]


def test_the_text_filter_is_what_gets_stripped_to_probe():
    assert plan_mod.unfiltered('a.title:-soup-contains("Cloud")') == "a.title"
    assert plan_mod.unfiltered('div:contains("x") > a') == "div > a"
    assert plan_mod.unfiltered("a.title") == "a.title"


def test_an_unfiltered_count_needs_no_probe():
    """Counting every row in a list is its own proof -- there is no filter that
    could silently exclude everything."""
    spec = {"scope": "#results", "kind": "count", "selector": "tr.job"}
    assert plan_mod.prove_the_item_selector(spec, JOBS_PAGE) is None


# --- the fetch method is decided by trying, not by asking --------------------
#
# A browser check costs 45x a plain GET ($8.05/month against $0.18 at one-minute
# intervals). The prompt asks the model to prefer http; these tests make it a
# guarantee instead, in both directions.

JS_SHELL = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'


def test_a_page_readable_without_javascript_never_reaches_the_browser():
    """The saving. If a spec verifies against raw HTML the page did not need
    rendering, whatever the model believed."""
    client = ScriptedClient({"literal": "$789.00", "sample": None, "note": ""},
                            GOOD_SPEC)
    browser_calls = []

    built, method, why = plan_mod.build_with_cheapest_fetch(
        "https://example.com", "the price", CONDITION,
        fetch_http=lambda: PAGE,
        fetch_browser=lambda: browser_calls.append(1) or PAGE,
        client=client,
    )

    assert method == "http"
    assert browser_calls == []
    assert built["verified_value"] == 789.00
    assert "no browser needed" in why


def test_a_javascript_rendered_page_escalates_and_is_kept():
    """The other direction: a target the model marked `http` that turns out to
    need rendering used to be rejected outright. Now it is retried and kept."""
    client = ScriptedClient(
        {"literal": None, "sample": None, "note": "page is an empty shell"},
        {"literal": "$789.00", "sample": None, "note": "found after render"},
        GOOD_SPEC,
    )

    built, method, why = plan_mod.build_with_cheapest_fetch(
        "https://example.com", "the price", CONDITION,
        fetch_http=lambda: JS_SHELL,
        fetch_browser=lambda: PAGE,
        client=client,
    )

    assert method == "browser"
    assert built["verified_value"] == 789.00
    assert "needed rendering" in why


def test_a_blocked_plain_get_escalates_rather_than_failing_the_target():
    """403s and TLS errors are a reason to render, not to give up."""
    client = ScriptedClient({"literal": "$789.00", "sample": None, "note": ""},
                            GOOD_SPEC)

    def blocked():
        raise OSError("HTTP Error 403: Forbidden")

    built, method, why = plan_mod.build_with_cheapest_fetch(
        "https://example.com", "the price", CONDITION,
        fetch_http=blocked, fetch_browser=lambda: PAGE, client=client,
    )

    assert method == "browser"
    assert "403" in why


def test_only_one_cheap_model_call_is_wasted_when_rendering_is_needed():
    """The cost of always trying http first. build_extractor gives up before
    compiling when the value is not in the text at all -- which is the shape of
    a JS-rendered page -- so the waste is one Haiku read, not a Sonnet compile."""
    client = ScriptedClient(
        {"literal": None, "sample": None, "note": "empty shell"},
        {"literal": "$789.00", "sample": None, "note": "found"},
        GOOD_SPEC,
    )
    plan_mod.build_with_cheapest_fetch(
        "https://example.com", "the price", CONDITION,
        fetch_http=lambda: JS_SHELL, fetch_browser=lambda: PAGE, client=client)

    # read(http) + read(browser) + compile(browser) -- no compile was attempted
    # against the shell.
    assert len(client.prompts) == 3


def test_a_presence_watch_escalates_the_same_way():
    client = ScriptedClient(
        {"literal": None, "sample": None, "note": "no listings rendered yet"},
        {"literal": None, "sample": "QA Automation Student, Beer Sheva", "note": ""},
        COUNT_SPEC,
    )
    built, method, _ = plan_mod.build_with_cheapest_fetch(
        "https://jobs.example", "cloud engineer roles", VACANCY_CONDITION,
        fetch_http=lambda: JS_SHELL, fetch_browser=lambda: JOBS_PAGE,
        shape="presence", client=client)

    assert method == "browser"
    assert built["verified_value"] == 0


def test_a_target_that_works_in_neither_mode_still_raises():
    client = ScriptedClient(
        {"literal": None, "sample": None, "note": "login wall"},
        {"literal": None, "sample": None, "note": "login wall after render too"},
    )
    with pytest.raises(ValueError):
        plan_mod.build_with_cheapest_fetch(
            "https://example.com", "the price", CONDITION,
            fetch_http=lambda: JS_SHELL, fetch_browser=lambda: JS_SHELL,
            client=client)


def test_the_read_step_is_told_not_to_judge():
    """A real failure: Haiku returned `literal: null` with the note "priced at
    $949.00, which does not meet the condition of price < $700", so the watch
    could not be planned at all. Reading and judging were conflated -- a
    leftover of the pre-8b design where one model call did both. Judging now
    happens in Python, for free, on every tick.
    """
    assert "NOT JUDGING" in plan_mod.READ_PROMPT
    assert "Never withhold a value because it fails the condition" in plan_mod.READ_PROMPT


def test_a_value_that_fails_the_condition_still_plans():
    """$789 against "under $700" is the normal state of a new watch: the whole
    point is that the condition is not satisfied yet."""
    client = ScriptedClient({"literal": "$789.00", "sample": None, "note": ""},
                            GOOD_SPEC)
    built = plan_mod.build_extractor(
        "https://example.com", "the price",
        {"metric": "price", "op": "<", "value": 700}, PAGE, client=client)
    assert built["verified_value"] == 789.00


# --- relative conditions: "goes down from the current" ------------------------
#
# Found by the owner. Asked "tell me when prices for Apple shares go down from
# the current", the Planner produced `price < 313.93` while the page said
# $333.43. That threshold is 5% below $330.45 -- a figure from search results,
# not from the page -- and the 5% was invented outright. "Goes down" means any
# decrease. The condition is written before the page is ever opened, so asking
# for an absolute threshold there guarantees a fabricated one.

def test_goes_down_means_any_decrease_from_what_was_actually_read():
    resolved = plan_mod.resolve_relative_condition(
        {"metric": "price", "op": "<", "value": None, "currency": "USD"},
        0, 333.43)

    assert resolved["value"] == 333.43
    assert resolved["baseline"] == 333.43
    assert resolved["op"] == "<"


def test_a_percentage_drop_is_computed_from_the_verified_value():
    resolved = plan_mod.resolve_relative_condition(
        {"metric": "price", "op": "<", "value": None}, -5, 333.43)
    assert resolved["value"] == round(333.43 * 0.95, 4)
    assert resolved["relative_change_pct"] == -5.0


def test_the_fabricated_threshold_is_what_this_replaces():
    """Regression, stated as the arithmetic that gave it away."""
    fabricated = 313.93
    stale_search_price = round(fabricated / 0.95, 2)
    assert stale_search_price == 330.45          # never appeared on the page

    honest = plan_mod.resolve_relative_condition(
        {"metric": "price", "op": "<", "value": fabricated}, 0, 333.43)
    assert honest["value"] == 333.43
    assert honest["value"] != fabricated


def test_a_rise_gets_the_right_direction_when_no_op_was_given():
    resolved = plan_mod.resolve_relative_condition(
        {"metric": "price", "op": None, "value": None}, 10, 100.0)
    assert resolved["op"] == ">"
    assert resolved["value"] == 110.0


def test_an_absolute_condition_is_left_completely_alone():
    """"drops below $300" is already a real threshold and must not be rebased."""
    original = {"metric": "price", "op": "<", "value": 300}
    assert plan_mod.resolve_relative_condition(original, None, 333.43) == original


def test_a_missing_baseline_cannot_produce_a_threshold():
    """Better an unresolved condition than a confidently wrong one."""
    original = {"metric": "price", "op": "<", "value": None}
    assert plan_mod.resolve_relative_condition(original, -5, None) == original
    assert plan_mod.resolve_relative_condition(original, -5, "n/a") == original


def test_a_boolean_baseline_is_not_arithmetic():
    original = {"metric": "in_stock", "op": "==", "value": True}
    assert plan_mod.resolve_relative_condition(original, -5, True) == original
