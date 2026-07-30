"""Tests for deterministic extraction.

Two things are worth more than coverage here.

First, the three-way outcome. `unavailable` and `failed` must never collapse
into each other: one means "not yet", the other means "this extractor is
broken". Phase 8d escalates the second and must ignore the first, so the
distinction is asserted directly and repeatedly.

Second, real page shapes. The fixtures below are the ones this project has
actually met -- a Steam refurbished listing that shows a price *and* "Out of
Stock", and prices in both `$629.00` and `779,00€` form, which the same page
served depending on where the fetch came from.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from extract import (  # noqa: E402
    FAILED,
    parse_currency,
    OK,
    UNAVAILABLE,
    Extraction,
    SpecError,
    coerce,
    extract,
    parse_number,
    plausible,
    validate_spec,
)

# --- fixtures drawn from pages this project has really fetched ---------------

STEAM_HTML = """
<html><body>
  <div class="sale_item" data-sku="512-oled">
    <span class="title">Steam Deck 512 GB OLED - Valve Certified Refurbished</span>
    <span class="discount_final_price" data-price="629.00">$629.00</span>
    <div class="availability">Out of Stock</div>
  </div>
  <div class="sale_item" data-sku="1tb-oled">
    <span class="title">Steam Deck 1TB OLED - Valve Certified Refurbished</span>
    <span class="discount_final_price" data-price="759.00">$759.00</span>
    <div class="availability">In Stock</div>
  </div>
</body></html>
"""

QUOTE_JSON = """
{"chart": {"result": [{"meta": {"currency": "USD",
 "symbol": "AAPL", "regularMarketPrice": 271.42, "previousClose": 268.9}}]}}
"""


def spec(**kwargs):
    base = {"kind": "css", "selector": ".discount_final_price", "parse": "currency"}
    base.update(kwargs)
    return base


# --- number parsing ----------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("$629.00", 629.00),
    ("629.00", 629.00),
    ("779,00€", 779.00),          # the Codespace-geography form from Phase 6
    ("1.234,56 €", 1234.56),      # European grouped
    ("$1,234.56", 1234.56),       # US grouped
    ("1,234", 1234.0),            # comma as thousands, not decimal
    ("1 234,50", 1234.50),        # space as thousands
    ("USD 42", 42.0),
    ("  -12.5  ", -12.5),
    ("Price: $99.99 today only", 99.99),
])
def test_prices_parse_in_both_conventions(raw, expected):
    assert parse_number(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["", "out of stock", "N/A", "--", "$"])
def test_text_with_no_number_is_an_error(raw):
    with pytest.raises(ValueError):
        parse_number(raw)


# --- money is stricter than a number ----------------------------------------

@pytest.mark.parametrize("raw", [
    "Steam Deck 512 GB OLED - Valve Certified Refurbished",  # the real one
    "1TB SSD",
    "4 reviews",
    "512",
])
def test_a_bare_number_is_refused_as_money(raw):
    """Found by this suite: the permissive parser read 512 out of "512 GB OLED"
    and would have reported it as a price. Money needs a symbol or a minor
    unit."""
    with pytest.raises(ValueError):
        parse_currency(raw)


@pytest.mark.parametrize("raw,expected", [
    ("$629.00", 629.00),
    ("779,00€", 779.00),
    ("629.00", 629.00),        # minor unit, no symbol
    ("USD 42", 42.0),          # code, no minor unit
    ("£1,234.56", 1234.56),
])
def test_things_that_really_are_money_still_parse(raw, expected):
    assert parse_currency(raw) == pytest.approx(expected)


def test_float_stays_permissive_for_non_money_values():
    """Points, counts and temperatures are bare numbers on purpose."""
    assert coerce("404 points", "float") == pytest.approx(404.0)


def test_int_rounds_rather_than_truncating():
    assert coerce("404.6 points", "int") == 405


@pytest.mark.parametrize("raw,expected", [
    ("In Stock", True), ("true", True), ("available", True),
    ("Out of Stock", False), ("no", False), ("", False),
])
def test_bool_reads_availability_words(raw, expected):
    assert coerce(raw, "bool") is expected


# --- jsonpath ----------------------------------------------------------------

def test_jsonpath_reads_a_quote_endpoint():
    """The cheapest possible check: a JSON API and one float comparison."""
    result = extract({
        "kind": "jsonpath",
        "path": "$.chart.result[0].meta.regularMarketPrice",
        "parse": "float",
    }, QUOTE_JSON)

    assert result.status == OK
    assert result.value == pytest.approx(271.42)


def test_jsonpath_works_without_the_dollar_prefix():
    result = extract({"kind": "jsonpath", "path": "chart.result[0].meta.symbol",
                      "parse": "text"}, QUOTE_JSON)
    assert result.value == "AAPL"


def test_jsonpath_accepts_bracket_quoted_keys():
    result = extract({"kind": "jsonpath",
                      "path": "$.chart.result[0]['meta']['currency']",
                      "parse": "text"}, QUOTE_JSON)
    assert result.value == "USD"


@pytest.mark.parametrize("path,fragment", [
    ("$.chart.result[0].meta.nope", "no key"),
    ("$.chart.result[9].meta.symbol", "out of range"),
    ("$.chart.meta.symbol", "no key"),
    ("$.chart.result[0].meta", "container"),
])
def test_a_jsonpath_that_does_not_resolve_is_failed_not_unavailable(path, fragment):
    """Because a path that stopped resolving means the API changed shape --
    something to repair, not a normal 'no value today'."""
    result = extract({"kind": "jsonpath", "path": path}, QUOTE_JSON)
    assert result.status == FAILED
    assert fragment in result.error


def test_non_json_body_fails_cleanly():
    result = extract({"kind": "jsonpath", "path": "$.a"}, "<html>nope</html>")
    assert result.status == FAILED
    assert "not JSON" in result.error


# --- css ---------------------------------------------------------------------

def test_css_reads_the_first_matching_price():
    result = extract(spec(), STEAM_HTML)
    assert result.status == OK
    assert result.value == pytest.approx(629.00)
    assert result.raw == "$629.00"


def test_css_can_target_a_specific_item_rather_than_the_first():
    """The Steam page lists five Decks. A spec that means the 1TB has to say so."""
    result = extract(spec(selector='[data-sku="1tb-oled"] .discount_final_price'),
                     STEAM_HTML)
    assert result.value == pytest.approx(759.00)


def test_css_can_read_an_attribute_instead_of_rendered_text():
    """Often the honest source -- an attribute is not decorated with currency
    symbols or promotional text."""
    result = extract(spec(selector='[data-sku="512-oled"] .discount_final_price',
                          attribute="data-price", parse="float"), STEAM_HTML)
    assert result.value == pytest.approx(629.00)


def test_a_selector_that_matches_nothing_is_failed():
    result = extract(spec(selector=".price-was-renamed"), STEAM_HTML)
    assert result.status == FAILED
    assert "matched nothing" in result.error


def test_a_missing_attribute_is_failed():
    result = extract(spec(selector=".discount_final_price", attribute="data-nope"),
                     STEAM_HTML)
    assert result.status == FAILED
    assert "attribute" in result.error


# --- regex -------------------------------------------------------------------

def test_regex_capture_group_selects_the_value():
    result = extract({"kind": "regex", "pattern": r"regularMarketPrice\"?:\s*([\d.]+)",
                      "parse": "float"}, QUOTE_JSON)
    assert result.value == pytest.approx(271.42)


def test_regex_without_a_group_uses_the_whole_match():
    result = extract({"kind": "regex", "pattern": r"\$\d+\.\d{2}",
                      "parse": "currency"}, STEAM_HTML)
    assert result.value == pytest.approx(629.00)


def test_a_pattern_that_matches_nothing_is_failed():
    result = extract({"kind": "regex", "pattern": r"zzz(\d+)"}, STEAM_HTML)
    assert result.status == FAILED


# --- the three-way outcome, which is the whole point -------------------------

def test_out_of_stock_is_unavailable_not_a_price():
    """The observation that motivated all of this. Haiku read the Steam page,
    saw 'Out of Stock' and declined to report a price. A naive extractor would
    have returned $629.00 for something that cannot be bought."""
    result = extract(spec(
        selector='[data-sku="512-oled"] .discount_final_price',
        unavailable_if={"kind": "regex", "pattern": r"out\s+of\s+stock"},
    ), STEAM_HTML)

    assert result.status == UNAVAILABLE
    assert result.value is None
    assert result.error is None          # nothing is broken


def test_unavailable_is_checked_before_the_value_is_read():
    """An out-of-stock page usually still shows a price, so reading first and
    asking afterwards would report a number that should not exist."""
    result = extract(spec(unavailable_if={"kind": "css",
                                          "selector": ".availability"}),
                     STEAM_HTML)
    assert result.status == UNAVAILABLE
    assert result.raw is None


def test_available_stock_still_yields_a_value():
    result = extract(spec(
        selector='[data-sku="1tb-oled"] .discount_final_price',
        unavailable_if={"kind": "css", "selector": ".nothing-matches-this"},
    ), STEAM_HTML)
    assert result.status == OK
    assert result.value == pytest.approx(759.00)


def test_unavailable_and_failed_are_never_the_same_thing():
    """Phase 8d escalates FAILED to a repair and must ignore UNAVAILABLE. If
    these ever collapse, a dead watch becomes indistinguishable from a patient
    one."""
    unavailable = extract(spec(unavailable_if={"kind": "regex",
                                               "pattern": "out of stock"}),
                          STEAM_HTML)
    broken = extract(spec(selector=".renamed-by-a-redesign"), STEAM_HTML)

    assert unavailable.status == UNAVAILABLE and unavailable.error is None
    assert broken.status == FAILED and broken.error is not None
    assert unavailable.status != broken.status


def test_an_empty_body_is_failed():
    assert extract(spec(), "").status == FAILED


def test_unparseable_text_is_failed_and_keeps_what_it_saw():
    """The raw text matters for a repair prompt: it is the evidence of what the
    selector actually pointed at."""
    result = extract({"kind": "css", "selector": ".title", "parse": "currency"},
                     STEAM_HTML)
    assert result.status == FAILED
    assert "Steam Deck" in result.raw


# --- spec validation ---------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "not an object",
    {},
    {"kind": "xpath", "path": "//a"},
    {"kind": "css"},
    {"kind": "css", "selector": "   "},
    {"kind": "regex", "pattern": "([unclosed"},
    {"kind": "regex", "pattern": r"(a)(b)"},          # two capture groups
    {"kind": "css", "selector": ".p", "parse": "guess"},
])
def test_a_malformed_spec_is_rejected_loudly(bad):
    """A bad spec is a planning bug and must surface at plan time, not become a
    silently failing tick."""
    with pytest.raises(SpecError):
        validate_spec(bad)


def test_unavailable_if_cannot_nest():
    with pytest.raises(SpecError):
        validate_spec(spec(unavailable_if={
            "kind": "regex", "pattern": "x",
            "unavailable_if": {"kind": "regex", "pattern": "y"},
        }))


def test_extract_validates_before_running():
    with pytest.raises(SpecError):
        extract({"kind": "nonsense"}, STEAM_HTML)


def test_an_invalid_css_selector_raises_rather_than_failing_quietly():
    with pytest.raises(SpecError):
        extract(spec(selector="div[[["), STEAM_HTML)


# --- plausibility against the plan-time baseline -----------------------------

def test_a_similar_value_is_plausible():
    assert plausible(640.0, baseline=629.0)


def test_a_wildly_different_value_is_not():
    """A selector surviving a redesign but now pointing at a review count is
    the failure a missing selector does not produce."""
    assert not plausible(4.7, baseline=629.0)
    assert not plausible(120_000.0, baseline=629.0)


def test_plausibility_is_deliberately_loose():
    """It exists to catch the wrong element, not a genuine price move."""
    assert plausible(300.0, baseline=629.0)
    assert plausible(1200.0, baseline=629.0)


@pytest.mark.parametrize("value,baseline", [
    ("in stock", 629.0), (True, 629.0), (629.0, None), (629.0, 0),
])
def test_plausibility_abstains_when_it_cannot_judge(value, baseline):
    assert plausible(value, baseline)


# --- the result object -------------------------------------------------------

def test_result_serialises_for_storage():
    import json
    payload = json.dumps(extract(spec(), STEAM_HTML).as_dict())
    assert json.loads(payload)["status"] == OK


def test_ok_is_a_convenience_for_the_common_check():
    assert Extraction(OK, value=1).ok
    assert not Extraction(FAILED, error="x").ok
    assert not Extraction(UNAVAILABLE).ok
