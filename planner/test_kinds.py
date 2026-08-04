"""Tests for the kind registry and for `quote`, the kind that compiles nothing.

`value` and `presence` are exercised through `test_planner.py`, which drives
them via the compile-and-verify pipeline they share. This file covers the two
things that are new in Phase 9 step 2: the registry itself, and a kind whose
whole point is that no model is involved.

The property worth protecting for `quote`: **it must still be verified.** The
temptation with a canned source is to trust it, because we wrote it. But a
registry entry can go stale -- CNBC reshaping its payload -- and the difference
between finding that out at plan time, in front of the user, and finding it out
at 3am three weeks later is the difference between a good watch and a lie.
"""

import importlib.util
import json
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

if "anthropic" not in sys.modules:
    _anthropic = types.ModuleType("anthropic")
    _anthropic.Anthropic = MagicMock()
    sys.modules["anthropic"] = _anthropic

sys.path.insert(0, os.path.join(_ROOT, "shared"))
sys.path.insert(0, _HERE)

import kinds  # noqa: E402
from kinds import jobs as jobs_mod  # noqa: E402
from kinds import quote as quote_mod  # noqa: E402

QUOTE = kinds.get("quote")


def cnbc(last="333.43", symbol="AAPL", name="Apple Inc",
         exchange="NASDAQ", currency="USD"):
    """The shape shared/sources.py's canned jsonpath expects.

    Carries the exchange and currency because those are no longer decoration:
    the window a quote watch runs in is looked up from `exchange`, and asking
    for a foreign company by its bare ticker returns the US listing.
    """
    quote = {"symbol": symbol, "last": last, "name": name,
             "exchange": exchange, "currencyCode": currency}
    return json.dumps({"FormattedQuoteResult": {"FormattedQuote": [quote]}})


@pytest.fixture
def wire(monkeypatch):
    """Stand in for the network. Records what was asked for."""

    def build(body=None, fails=False):
        calls = []

        def fetch_raw(url):
            calls.append(url)
            if fails:
                raise RuntimeError("connection reset")
            return cnbc() if body is None else body

        monkeypatch.setattr(quote_mod, "fetch_raw", fetch_raw)
        return calls

    return build


def scripted(*replies):
    """An Anthropic client that returns queued JSON replies, as in
    test_planner.py -- the model plumbing is the same everywhere."""
    from types import SimpleNamespace

    queue = list(replies)

    def create(**_kwargs):
        block = SimpleNamespace(type="text", text=json.dumps(queue.pop(0)))
        return SimpleNamespace(content=[block])

    return SimpleNamespace(messages=SimpleNamespace(create=create))


def never(*_args, **_kwargs):
    raise AssertionError("this kind must not fetch pages that way")


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------

def test_every_registered_kind_can_resolve():
    for name in kinds.names():
        assert hasattr(kinds.get(name), "resolve")


def test_an_unknown_kind_degrades_to_value_rather_than_raising():
    """The classifier is the newest and least-proven part of this design. A
    wrong guess must cost a suboptimal plan, never a rejected request."""
    assert kinds.get("nonsense").name == "value"
    assert kinds.get(None).name == "value"
    assert kinds.get("").name == "value"


def test_quote_is_not_a_compiled_kind():
    """The structural claim of step 2. If `quote` ever inherits the four
    compile methods, it has been bent to fit the wrong base class and the
    null-object problem is back."""
    assert isinstance(kinds.get("value"), kinds.CompiledKind)
    assert isinstance(kinds.get("presence"), kinds.CompiledKind)
    assert not isinstance(QUOTE, kinds.CompiledKind)
    assert not hasattr(QUOTE, "compile_prompt")


# --------------------------------------------------------------------------
# Resolving a quote
# --------------------------------------------------------------------------

def test_a_symbol_becomes_a_verified_target(wire):
    calls = wire()
    resolved = QUOTE.resolve({"known_source": "stock_quote", "symbol": "AAPL"},
                             {"metric": "price", "op": "<", "value": 300},
                             fetch_http=never, fetch_browser=never)

    assert resolved["verified_value"] == 333.43
    assert resolved["verified_raw"] == "333.43"
    assert resolved["fetch_method"] == "http"
    assert "AAPL" in calls[0]


def test_no_model_is_consulted(wire):
    """The whole point of the kind. `client` is accepted and must go unused --
    if a model call appears here, planning a stock watch has silently gone back
    to costing what it used to."""
    wire()
    client = MagicMock()
    QUOTE.resolve({"known_source": "stock_quote", "symbol": "AAPL"}, {},
                  fetch_http=never, fetch_browser=never, client=client)

    client.messages.create.assert_not_called()


def test_the_browser_is_never_reached(wire):
    """A quote is a JSON endpoint. Rendering one in Chromium would cost 45x
    for nothing, so the fetchers are accepted and deliberately ignored."""
    wire()
    QUOTE.resolve({"known_source": "stock_quote", "symbol": "AAPL"}, {},
                  fetch_http=never, fetch_browser=never)


def test_the_hint_that_survives_is_a_repair_instruction(wire):
    """The stored hint is no longer a reading instruction -- nothing reads it
    on a tick. It exists so a human can see where the number comes from."""
    wire()
    resolved = QUOTE.resolve({"known_source": "stock_quote", "symbol": "AAPL"},
                             {}, fetch_http=never, fetch_browser=never)

    assert "AAPL" in resolved["extract_hint"]
    assert "FormattedQuote" in resolved["extract_hint"]


# --------------------------------------------------------------------------
# It is canned, not trusted
# --------------------------------------------------------------------------

def test_a_reshaped_payload_is_refused(wire):
    """CNBC reshaping its response must fail here, loudly, with nothing
    stored -- not silently at 3am three weeks later."""
    wire(body=json.dumps({"quotes": [{"price": "333.43"}]}))

    with pytest.raises(ValueError, match="could not read a quote"):
        QUOTE.resolve({"known_source": "stock_quote", "symbol": "AAPL"}, {},
                      fetch_http=never, fetch_browser=never)


def test_a_price_that_is_present_but_unreadable_still_names_the_extractor(wire):
    """The two failures are different and must stay distinguishable. A missing
    quote is a coverage limit; a quote whose price will not parse is our spec
    being wrong about a payload that does exist."""
    wire(body=cnbc(last="N/A"))

    with pytest.raises(ValueError, match="canned extractor"):
        QUOTE.resolve({"known_source": "stock_quote", "symbol": "AAPL"}, {},
                      fetch_http=never, fetch_browser=never)


def test_an_endpoint_that_will_not_answer_is_not_swallowed(wire):
    wire(fails=True)

    with pytest.raises(RuntimeError, match="connection reset"):
        QUOTE.resolve({"known_source": "stock_quote", "symbol": "AAPL"}, {},
                      fetch_http=never, fetch_browser=never)


@pytest.mark.parametrize("symbol", ["", "not a ticker", "AAPL; DROP", "../etc"])
def test_a_symbol_that_is_not_a_symbol_is_rejected_before_any_request(wire, symbol):
    """The symbol arrives from a model and is spliced into a URL, so the
    character set is a hard gate rather than a formality."""
    calls = wire()

    with pytest.raises(ValueError, match="plausible market symbol"):
        QUOTE.resolve({"known_source": "stock_quote", "symbol": symbol}, {},
                      fetch_http=never, fetch_browser=never)
    assert calls == []


def test_an_unknown_registry_kind_is_rejected(wire):
    calls = wire()

    with pytest.raises(ValueError, match="unknown known_source"):
        QUOTE.resolve({"known_source": "weather", "symbol": "AAPL"}, {},
                      fetch_http=never, fetch_browser=never)
    assert calls == []


# --------------------------------------------------------------------------
# Self-healing
# --------------------------------------------------------------------------

def test_a_quote_does_not_self_heal():
    """8d exists because a site we do not control can be redesigned under a
    compiled extractor. Here the extractor is ours: if it breaks, the fix is
    one line in sources.py for every watch at once, and paying Haiku to
    rediscover it per watch would be slower and wrong."""
    assert QUOTE.self_heals is False
    assert kinds.get("value").self_heals is True
    assert kinds.get("presence").self_heals is True


# --------------------------------------------------------------------------
# Planning a quote: condition and cadence only, and no web search
# --------------------------------------------------------------------------

class Scripted:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text",
                                           text=json.dumps(self.payload))])


def quote_plan(payload, request="tell me when Apple goes down"):
    client = Scripted(payload)
    return QUOTE.plan(request, "AAPL", client=client), client


def test_planning_a_quote_never_reaches_the_web_search():
    """The saving that made classification worth doing on its own. A quote used
    to pay for Sonnet *with* web search before anyone noticed the answer was a
    registry lookup."""
    _, client = quote_plan({"condition": {"metric": "price", "op": "<"},
                            "relative_change_pct": 0,
                            "check_interval_min": 5})

    assert len(client.calls) == 1
    assert "tools" not in client.calls[0]
    assert client.calls[0]["model"] == "claude-haiku-4-5-20251001"


def test_the_target_is_the_symbol_and_nothing_else_is_chosen():
    result, _ = quote_plan({"condition": {}, "relative_change_pct": None,
                            "check_interval_min": 5})
    assert result["targets"] == [{"known_source": "stock_quote",
                                  "symbol": "AAPL"}]


def test_a_relative_request_leaves_the_threshold_unset():
    """Same rule as the searching path, and for the same reason: the model has
    no current price, so any absolute threshold it writes is invented."""
    result, _ = quote_plan({"condition": {"metric": "price", "op": "<",
                                          "value": None},
                            "relative_change_pct": 0,
                            "check_interval_min": 1})
    assert result["relative_change_pct"] == 0
    assert result["condition"]["value"] is None


@pytest.mark.parametrize("proposed,expected", [(1, 1), (30, 30), (59, 59),
                                               (60, 59), (240, 59), (1440, 59)])
def test_the_interval_is_clamped_below_an_hour(proposed, expected):
    """A windowed schedule is a cron step inside the hour, so `*/60` would
    silently fire hourly on the hour. Clamped rather than refused -- the model
    picked a cadence, not a contract."""
    result, _ = quote_plan({"condition": {}, "relative_change_pct": None,
                            "check_interval_min": proposed})
    assert result["check_interval_min"] == expected


def test_a_missing_interval_gets_a_usable_default():
    result, _ = quote_plan({"condition": {}, "relative_change_pct": None})
    assert 1 <= result["check_interval_min"] <= 59


def test_the_planned_interval_is_expressible_as_a_windowed_schedule():
    """Ties the clamp to the thing it protects: whatever the model proposes,
    shared/schedules.py must be able to build a cron for it."""
    import schedules
    result, _ = quote_plan({"condition": {}, "check_interval_min": 720})
    schedules.expression(result["check_interval_min"], QUOTE.window)


# --------------------------------------------------------------------------
# Which instrument, on which exchange, in which currency
#
# The feature's worst behaviour was answering a question about one security
# with a confident number from another. Every test here is about that.
# --------------------------------------------------------------------------

def test_the_instrument_that_answered_is_reported(wire):
    """A bare ticker for a foreign company returns the US depositary receipt.
    Probed live: SAP comes back $193.50 NYSE USD, not Frankfurt. The plan card
    showed a bare number and there was no way to notice."""
    wire(body=cnbc(symbol="SAP", name="SAP SE", exchange="NYSE",
                   currency="USD", last="193.50"))
    resolved = QUOTE.resolve({"known_source": "stock_quote", "symbol": "SAP"},
                             {}, fetch_http=never, fetch_browser=never)

    assert resolved["instrument_name"] == "SAP SE"
    assert resolved["exchange"] == "NYSE"
    assert resolved["currency"] == "USD"
    # And it reaches the human-readable line the Planner logs and stores.
    assert "SAP SE" in resolved["why"]
    assert "NYSE" in resolved["why"]


def test_a_tel_aviv_listing_gets_the_tel_aviv_window(wire):
    """The reason windows stopped being a constant. TASE trades Sunday to
    Thursday, so a MON-FRI schedule misses Sunday's session entirely and then
    polls all Friday while the exchange is shut."""
    wire(body=cnbc(symbol="LUMI-IL", name="Bank Leumi Le Israel BM",
                   exchange="Tel Aviv Stock Exchange", currency="ILS",
                   last="7,377.00"))
    resolved = QUOTE.resolve({"known_source": "stock_quote", "symbol": "LUMI-IL"},
                             {}, fetch_http=never, fetch_browser=never)

    assert resolved["window"] == "tase_hours"
    # Agorot, and deliberately not converted to shekels: 7,377 is what TASE
    # itself displays, and a relative condition has no units anyway.
    assert resolved["verified_value"] == 7377.0


@pytest.mark.parametrize("exchange,window", [
    ("NASDAQ", "us_market_hours"),
    ("NYSE", "us_market_hours"),
    ("Tel Aviv Stock Exchange", "tase_hours"),
    ("XETRA", "xetra_hours"),
    ("London Stock Exchange", "lse_hours"),
])
def test_the_window_follows_the_exchange(wire, exchange, window):
    wire(body=cnbc(exchange=exchange))
    resolved = QUOTE.resolve({"known_source": "stock_quote", "symbol": "X"},
                             {}, fetch_http=never, fetch_browser=never)
    assert resolved["window"] == window


def test_an_unlisted_exchange_falls_back_to_us_hours_rather_than_failing(wire):
    """The table cannot be complete. Checking a shut market too often is a
    small cost problem; refusing to plan the watch is a broken product."""
    wire(body=cnbc(exchange="Bourse de Casablanca"))
    resolved = QUOTE.resolve({"known_source": "stock_quote", "symbol": "X"},
                             {}, fetch_http=never, fetch_browser=never)
    assert resolved["window"] == "us_market_hours"


def test_an_uncovered_symbol_says_so_in_words(wire):
    """What WALMEX did on 2026-08-03. CNBC does not carry the Bolsa Mexicana,
    and the user was told `no key 'last' at
    '.FormattedQuoteResult.FormattedQuote[0].last'` -- a sentence about our
    jsonpath, not about their problem."""
    wire(body=json.dumps({"FormattedQuoteResult": {
        "FormattedQuote": [{"symbol": "WALMEX-MX"}]}}))

    with pytest.raises(ValueError, match="no quote for WALMEX-MX"):
        QUOTE.resolve({"known_source": "stock_quote", "symbol": "WALMEX-MX"},
                      {}, fetch_http=never, fetch_browser=never)


def test_the_symbol_suffix_survives_into_the_request(wire):
    """-IL is what selects the home listing over the ADR. If it were stripped
    or upper-cased away, every Israeli watch would silently track New York."""
    calls = wire(body=cnbc(symbol="TEVA-IL", exchange="Tel Aviv Stock Exchange",
                           currency="ILS", last="10,350.00"))
    QUOTE.resolve({"known_source": "stock_quote", "symbol": "TEVA-IL"}, {},
                  fetch_http=never, fetch_browser=never)

    assert "TEVA-IL" in calls[0]


# --------------------------------------------------------------------------
# The `jobs` kind
#
# It exists because the searching Planner could not plan the request the
# `presence` kind was built for: asked about a student cloud engineer vacancy
# in Beer Sheva it chose a cookie-walled careers page and LinkedIn's ordinary
# search page, and failed on both.
# --------------------------------------------------------------------------

JOBS = kinds.get("jobs")

JOB_CARDS = """<!DOCTYPE html>
<li><div class="base-card job-search-card" data-entity-urn="urn:li:jobPosting:111">
  <a href="https://il.linkedin.com/jobs/view/devops-111?refId=AAA">DevOps Engineer</a>
  <h3>DevOps Engineer</h3></div></li>
<li><div class="base-card job-search-card" data-entity-urn="urn:li:jobPosting:222">
  <a href="https://il.linkedin.com/jobs/view/cloud-222?refId=BBB">Cloud Engineer</a>
  <h3>Cloud Engineer</h3></div></li>"""


@pytest.fixture
def board(monkeypatch):
    def build(body=JOB_CARDS):
        calls = []

        def fetch_raw(url):
            calls.append(url)
            return body

        monkeypatch.setattr(jobs_mod, "fetch_raw", fetch_raw)
        return calls

    return build


def test_a_job_search_never_touches_the_web_search(board):
    """Same saving as `quote`: the boards are decided, so there is nothing to
    choose and no Sonnet-with-search to pay for."""
    board()
    client = scripted({"keywords": "cloud engineer", "location": "Beer Sheva, Israel",
                       "country": "IL", "check_interval_min": 30})
    plan = JOBS.plan("a student cloud engineer job in Beer Sheva", client=client)

    assert len(plan["targets"]) == 2          # linkedin + drushim
    assert plan["check_interval_min"] == 30
    assert plan["condition"]["op"] == ">"


def test_a_job_watch_repeats(board):
    """A job search is a stream. It matters more here than anywhere: LinkedIn's
    guest endpoint alternates between two result sets, so without per-item
    deduplication this would email every other tick forever."""
    assert JOBS.repeating is True


def test_the_registry_extractor_is_never_repaired():
    """If a board reshapes, the fix is one line in job_boards.py for every
    watch at once -- paying Haiku to rediscover it per watch would be slower
    and would produce inconsistent extractors."""
    assert JOBS.self_heals is False


def test_a_board_is_proved_against_the_wire_before_the_plan_is_offered(board):
    calls = board()
    resolved = JOBS.resolve(
        {"url": "https://www.linkedin.com/jobs-guest/x", "board": "linkedin",
         "fetch_method": "http", "extract_hint": "jobs",
         "extractor": {"kind": "count", "selector": "li div.job-search-card",
                       "parse": "int"}},
        {}, fetch_http=never, fetch_browser=never)

    assert resolved["verified_value"] == 2
    assert len(resolved["verified_items"]) == 2
    assert resolved["fetch_method"] == "http"
    assert calls == ["https://www.linkedin.com/jobs-guest/x"]


def test_identity_survives_linkedins_changing_links(board):
    """Every response carries a fresh refId, so identity built from the raw URL
    would change on every check and a repeating watch would re-report every
    job, every tick, forever. Keyed on data-entity-urn instead."""
    board()
    first = JOBS.resolve(
        {"url": "u", "board": "linkedin", "fetch_method": "http",
         "extract_hint": "jobs",
         "extractor": {"kind": "count", "selector": "li div.job-search-card",
                       "parse": "int"}},
        {}, fetch_http=never, fetch_browser=never)

    monkeypatched = JOB_CARDS.replace("refId=AAA", "refId=ZZZ").replace(
        "refId=BBB", "refId=YYY")
    board(monkeypatched)
    second = JOBS.resolve(
        {"url": "u", "board": "linkedin", "fetch_method": "http",
         "extract_hint": "jobs",
         "extractor": {"kind": "count", "selector": "li div.job-search-card",
                       "parse": "int"}},
        {}, fetch_http=never, fetch_browser=never)

    assert ([i["id"] for i in first["verified_items"]]
            == [i["id"] for i in second["verified_items"]])


def test_a_board_returning_nothing_is_refused(board):
    """Zero is legitimate for a `presence` watch -- the job is not posted yet.
    Here it means the board was reshaped or the query is malformed, and a watch
    built on it could never fire."""
    board("<!DOCTYPE html><p>no results</p>")

    with pytest.raises(ValueError, match="no listings"):
        JOBS.resolve(
            {"url": "u", "board": "linkedin", "fetch_method": "http",
             "extract_hint": "jobs",
             "extractor": {"kind": "count", "selector": "li div.job-search-card",
                           "parse": "int"}},
            {}, fetch_http=never, fetch_browser=never)


def test_a_request_with_no_searchable_role_is_refused(board):
    board()
    client = scripted({"keywords": "  ", "location": "Israel", "country": "IL",
                       "check_interval_min": 30})

    with pytest.raises(ValueError, match="could not work out"):
        JOBS.plan("something about work", client=client)


def test_jobs_is_not_a_compiled_kind():
    """Nothing to anchor, nothing to compile -- the same structural claim as
    `quote`. If it ever inherits the four compile methods it has been bent to
    fit the wrong base class."""
    assert not isinstance(JOBS, kinds.CompiledKind)
    assert not hasattr(JOBS, "compile_prompt")
