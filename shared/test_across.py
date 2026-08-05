"""Tests for the cross-target reading.

The assertions worth stating up front, because they are the design:

- **A currency is never converted, so it is never compared.** A USD row and an
  ILS threshold do not meet.
- **A checked target that read nothing has no price**, and must not quietly
  fall back to what it was worth at plan time.
- **A stale reading is shown and never wins.** Hiding it loses a shop; letting
  it win reports last week as today.
"""

from datetime import datetime, timedelta, timezone

import across

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
CHEAPER = {"metric": "price", "op": "<", "value": 2000,
           "currency": "ILS", "across": "best"}


def ago(minutes):
    return (NOW - timedelta(minutes=minutes)).isoformat()


def target(target_id, value, *, currency="ILS", minutes=5, status="ok",
           shop=None, **extra):
    row = {
        "target_id": target_id,
        "currency": currency,
        "url": f"https://{shop or target_id}.example/search",
        "last_value": value,
        "last_status": status,
        "last_checked_at": ago(minutes),
    }
    if shop:
        row["shop"] = shop
    row.update(extra)
    return row


# --- direction and mode ------------------------------------------------------

def test_ordered_ops_have_a_best_end():
    assert across.direction({"op": "<"}) == "min"
    assert across.direction({"op": "<="}) == "min"
    assert across.direction({"op": ">"}) == "max"
    assert across.direction({"op": ">="}) == "max"


def test_equality_has_no_best():
    assert across.direction({"op": "=="}) is None
    assert across.direction({"op": "!="}) is None
    assert across.mode({"op": "==", "across": "best"}) == across.ANY


def test_mode_is_any_unless_the_watch_asked_for_best():
    assert across.mode({"op": "<"}) == across.ANY
    assert across.mode({"op": "<", "across": "best"}) == across.BEST
    assert across.mode(None) == across.ANY


# --- currency ----------------------------------------------------------------

def test_a_different_currency_is_not_compared():
    """The bug this closes: `price < 2000` (shekels) was applied to $34.99."""
    rows = [target("t1", 1890.0), target("t2", 34.99, currency="USD")]
    found = across.readings(rows, CHEAPER, now=NOW)
    assert [r.target_id for r in found] == ["t1"]


def test_a_target_with_no_currency_is_included():
    """Every kind but `product` stores none, and has one target anyway."""
    rows = [target("t1", 306.4, currency=None)]
    assert len(across.readings(rows, CHEAPER, now=NOW)) == 1


def test_no_currency_on_the_condition_compares_everything():
    rows = [target("t1", 1890.0), target("t2", 34.99, currency="USD")]
    found = across.readings(rows, {"op": "<", "across": "best"}, now=NOW)
    assert len(found) == 2


# --- which number a target reports -------------------------------------------

def test_an_unavailable_target_has_no_price():
    """It was checked and found nothing. The plan-time value is weeks old and
    describes a page that has since said otherwise."""
    row = target("t1", None, status="unavailable",
                 verified_value=1890.0, verified_at=ago(10))
    assert across.reading_of(row, now=NOW, interval_min=60) is None


def test_a_failed_target_has_no_price():
    row = target("t1", 1890.0, status="failed")
    assert across.reading_of(row, now=NOW, interval_min=60) is None


def test_an_unchecked_target_uses_its_plan_time_reading():
    """Otherwise the first tick after confirm compares one shop out of three."""
    row = {"target_id": "t1", "currency": "ILS", "url": "https://x/",
           "verified_value": 1890.0, "verified_at": ago(3)}
    reading = across.reading_of(row, now=NOW, interval_min=60)
    assert reading.value == 1890.0
    assert reading.source == "plan"
    assert not reading.stale


def test_a_plan_time_reading_can_be_stale_too():
    row = {"target_id": "t1", "verified_value": 1890.0, "verified_at": ago(600)}
    assert across.reading_of(row, now=NOW, interval_min=60).stale


def test_a_non_numeric_reading_is_not_a_price():
    assert across.reading_of(target("t1", "in stock"),
                             now=NOW, interval_min=60) is None
    assert across.reading_of(target("t1", True),
                             now=NOW, interval_min=60) is None


# --- staleness ---------------------------------------------------------------

def test_two_intervals_is_still_fresh():
    row = target("t1", 1890.0, minutes=110)
    assert not across.reading_of(row, now=NOW, interval_min=60).stale


def test_past_two_intervals_is_stale():
    row = target("t1", 1890.0, minutes=200)
    assert across.reading_of(row, now=NOW, interval_min=60).stale


def test_an_unparseable_timestamp_is_stale_not_a_crash():
    row = target("t1", 1890.0)
    row["last_checked_at"] = "yesterday"
    assert across.reading_of(row, now=NOW, interval_min=60).stale


def test_stale_readings_sort_last_however_cheap():
    rows = [target("t1", 1890.0, shop="bug"),
            target("t2", 100.0, minutes=999, shop="ivory")]
    found = across.readings(rows, CHEAPER, now=NOW, interval_min=60)
    assert [r.shop for r in found] == ["bug", "ivory"]


def test_best_ignores_stale_readings():
    rows = [target("t1", 1890.0), target("t2", 100.0, minutes=999)]
    found = across.readings(rows, CHEAPER, now=NOW, interval_min=60)
    assert across.best(found, CHEAPER).value == 1890.0


def test_best_is_none_when_every_shop_has_gone_quiet():
    rows = [target("t1", 1890.0, minutes=999)]
    found = across.readings(rows, CHEAPER, now=NOW, interval_min=60)
    assert across.best(found, CHEAPER) is None


def test_best_is_none_for_an_operator_with_no_direction():
    rows = [target("t1", 1890.0)]
    condition = dict(CHEAPER, op="!=")
    found = across.readings(rows, condition, now=NOW)
    assert found and across.best(found, condition) is None


# --- ordering ----------------------------------------------------------------

def test_cheapest_first_for_a_less_than_watch():
    rows = [target("t1", 2100.0, shop="bug"), target("t2", 1890.0, shop="ivory")]
    found = across.readings(rows, CHEAPER, now=NOW)
    assert [r.shop for r in found] == ["ivory", "bug"]
    assert across.best(found, CHEAPER).shop == "ivory"


def test_highest_first_for_a_greater_than_watch():
    condition = dict(CHEAPER, op=">")
    rows = [target("t1", 2100.0, shop="bug"), target("t2", 1890.0, shop="ivory")]
    found = across.readings(rows, condition, now=NOW)
    assert [r.shop for r in found] == ["bug", "ivory"]


# --- the running tick's own reading ------------------------------------------

def test_override_replaces_the_row_being_written():
    """The tick compares the number it just read, not the one it is replacing."""
    rows = [target("t1", 2100.0, shop="bug"), target("t2", 1890.0, shop="ivory")]
    fresh = across.Reading(target_id="t1", value=999.0, currency="ILS",
                           shop="bug", at=NOW.isoformat())
    found = across.readings(rows, CHEAPER, now=NOW, override={"t1": fresh})
    assert across.best(found, CHEAPER).value == 999.0


def test_override_is_used_even_when_the_stored_row_had_no_reading():
    rows = [target("t1", None, status="failed", shop="bug")]
    fresh = across.Reading(target_id="t1", value=999.0, shop="bug")
    found = across.readings(rows, CHEAPER, now=NOW, override={"t1": fresh})
    assert [r.value for r in found] == [999.0]


# --- shape -------------------------------------------------------------------

def test_a_reading_serialises_for_the_event():
    rows = [target("t1", 1890.0, shop="ivory")]
    payload = across.readings(rows, CHEAPER, now=NOW)[0].as_dict()
    assert payload["shop"] == "ivory"
    assert payload["value"] == 1890.0
    assert payload["currency"] == "ILS"
    assert payload["stale"] is False


def test_the_shop_name_falls_back_to_the_host():
    row = {"target_id": "t1", "url": "https://www.ivory.co.il/catalog.php",
           "last_value": 10, "last_status": "ok", "last_checked_at": ago(1)}
    assert across.reading_of(row, now=NOW, interval_min=60).shop == "ivory.co.il"


def test_describe_names_every_shop_and_flags_the_stale_ones():
    rows = [target("t1", 1890.0, shop="ivory"),
            target("t2", 2100.0, shop="bug", minutes=999)]
    line = across.describe(across.readings(rows, CHEAPER, now=NOW,
                                          interval_min=60))
    assert "ivory 1890.0 ILS" in line
    assert "bug 2100.0 ILS (stale)" in line


# --- delivery, carried through to the report ---------------------------------
#
# `value` is already landed where delivery is known, so this is not a second
# number: it is whether the number is a total or a floor. Measured, it is
# usually a floor, and the email has to say so rather than say "cheapest".

def test_a_reading_carries_the_delivery_terms_of_the_offer_it_is():
    row = target("t1", 2679.0)
    row["last_items"] = [{"price": 2679.0,
                          "shipping": {"state": "free", "amount": 0.0,
                                       "why": "over the threshold"}}]
    assert across.reading_of(row, now=NOW, interval_min=60).shipping["state"] \
        == "free"


def test_the_cheapest_offer_is_the_one_whose_delivery_counts():
    """The reading *is* the cheapest offer, so its terms are the ones that
    apply. Items are stored cheapest-first by landed price."""
    row = target("t1", 54.0)
    row["last_items"] = [
        {"price": 29.0, "shipping": {"state": "extra", "amount": 25.0, "why": ""}},
        {"price": 2679.0, "shipping": {"state": "free", "amount": 0.0, "why": ""}},
    ]
    reading = across.reading_of(row, now=NOW, interval_min=60)
    assert reading.shipping["amount"] == 25.0


def test_a_row_with_no_items_has_unknown_delivery_not_a_crash():
    """Every kind but `product`. A share price has no delivery, and `unknown`
    is the honest word for that rather than an omission."""
    assert across.reading_of(target("t1", 306.4),
                             now=NOW, interval_min=60).shipping["state"] \
        == "unknown"


def test_delivery_survives_into_the_event_payload():
    rows = [target("t1", 2679.0)]
    rows[0]["last_items"] = [{"price": 2679.0,
                              "shipping": {"state": "free", "amount": 0.0,
                                           "why": "over the threshold"}}]
    payload = across.readings(rows, CHEAPER, now=NOW)[0].as_dict()
    assert payload["shipping"]["state"] == "free"


def test_an_overridden_reading_carries_its_own_delivery():
    rows = [target("t1", 2679.0, shop="bug")]
    fresh = across.Reading(target_id="t1", value=999.0, shop="bug",
                           shipping={"state": "extra", "amount": 40.0, "why": ""})
    found = across.readings(rows, CHEAPER, now=NOW, override={"t1": fresh})
    assert found[0].shipping["amount"] == 40.0
