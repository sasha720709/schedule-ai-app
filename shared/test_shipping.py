"""Tests for what an offer costs to receive.

Two assertions carry this file, and both come from measurements rather than
from reasoning about how shops ought to work.

**"FREE delivery" is usually not free delivery.** It appears on 14 of 14 cheap
Amazon cards, and on every one of them it means "if you join Prime" or "on $35
of items". A substring match marks a $4.59 cable as free-shipping, confidently,
and is wrong every time.

**Unknown is an answer.** No shop probed publishes a delivery cost before
checkout, so this is the common case, not the edge. It must never quietly
become "free" or "zero" -- the whole point of the module is to stop a floor
being presented as a total.
"""

import shipping


# --- schema.org, the one that will age well ----------------------------------

def test_a_published_zero_rate_is_known_free():
    offer = {"shippingDetails": {"@type": "OfferShippingDetails",
                                 "shippingRate": {"value": 0,
                                                  "currency": "ILS"}}}
    assert shipping.from_schema(offer)["state"] == shipping.FREE


def test_a_published_rate_is_a_number_to_add():
    offer = {"shippingDetails": {"shippingRate": {"value": "29.90"}}}
    fact = shipping.from_schema(offer)
    assert fact["state"] == shipping.EXTRA
    assert fact["amount"] == 29.9


def test_a_shipping_details_list_takes_the_first_usable_one():
    offer = {"shippingDetails": [{"shippingRate": {"value": 15}}]}
    assert shipping.from_schema(offer)["amount"] == 15.0


def test_delivery_time_is_not_a_delivery_cost():
    """What Ivory actually publishes: 0-3 days handling, 1-5 in transit. A
    duration is not a price and must not be read as one."""
    offer = {"deliveryTime": {"@type": "ShippingDeliveryTime",
                             "handlingTime": {"minValue": 0, "maxValue": 3}}}
    assert shipping.from_schema(offer)["state"] == shipping.UNKNOWN


def test_no_shipping_details_at_all_is_unknown():
    assert shipping.from_schema({"price": 10})["state"] == shipping.UNKNOWN
    assert shipping.from_schema(None)["state"] == shipping.UNKNOWN


def test_an_unparseable_rate_is_unknown_not_zero():
    offer = {"shippingDetails": {"shippingRate": {"value": "call us"}}}
    assert shipping.from_schema(offer)["state"] == shipping.UNKNOWN


# --- the delivery element, and the trap in it --------------------------------

def test_an_unconditional_free_delivery_is_believed():
    """The console card, verbatim from a real render."""
    assert shipping.from_text("FREE delivery Aug 13 - 18")["state"] == shipping.FREE


def test_free_delivery_behind_a_subscription_is_not_free():
    """Verbatim from a $4.59 cable. This is the whole reason the module exists
    rather than a two-line `"FREE delivery" in text`."""
    fact = shipping.from_text("Join Prime to get FREE delivery Fri, Aug 7")
    assert fact["state"] == shipping.UNKNOWN
    assert "only" in fact["why"]


def test_free_delivery_conditional_on_a_bigger_basket_is_not_free():
    """A watch is about one thing, and $35 of items is not one thing."""
    text = ("Or Non-members get FREE delivery Mon, Aug 10 on $35 of items "
            "shipped by Amazon")
    assert shipping.from_text(text)["state"] == shipping.UNKNOWN


def test_a_delivery_price_is_read_when_a_shop_prints_one():
    assert shipping.from_text("$5.99 delivery Tuesday")["amount"] == 5.99
    assert shipping.from_text("delivery $12.50")["amount"] == 12.5


def test_delivery_text_that_says_nothing_useful_is_unknown():
    assert shipping.from_text("Arrives before Christmas")["state"] == shipping.UNKNOWN
    assert shipping.from_text("")["state"] == shipping.UNKNOWN
    assert shipping.from_text(None)["state"] == shipping.UNKNOWN


# --- the shop's own threshold ------------------------------------------------

def test_over_the_threshold_is_free():
    """Bug: free regular delivery over ILS 179. A ILS 2,679 console clears it
    comfortably, which is the case that matters most and the one this settles."""
    fact = shipping.from_threshold(2679, 179, "ILS")
    assert fact["state"] == shipping.FREE
    assert "179" in fact["why"]


def test_exactly_at_the_threshold_counts_as_over():
    assert shipping.from_threshold(179, 179)["state"] == shipping.FREE


def test_under_the_threshold_is_unknown_not_a_computed_cost():
    """The cost below the threshold is a number this system has never seen.
    Inventing "the difference" would be a confident email about money."""
    fact = shipping.from_threshold(29, 179, "ILS")
    assert fact["state"] == shipping.UNKNOWN
    assert fact["amount"] is None


def test_a_shop_with_no_published_threshold_says_nothing():
    """Ivory: not discoverable over plain HTTP, so it stays unknown rather
    than borrowing another shop's number."""
    assert shipping.from_threshold(2679, None)["state"] == shipping.UNKNOWN


# --- combining sources -------------------------------------------------------

def test_the_first_source_that_knows_something_wins():
    """Order is a statement about evidence, not a search for the best price:
    a published field beats an element beats a threshold."""
    published = shipping.fact(shipping.EXTRA, 29.9, "published")
    threshold = shipping.fact(shipping.FREE, 0.0, "over the threshold")
    assert shipping.best_known(published, threshold) is published


def test_an_unknown_source_is_skipped_for_one_that_knows():
    unknown = shipping.fact()
    known = shipping.fact(shipping.FREE, 0.0, "free")
    assert shipping.best_known(unknown, known) is known


def test_a_reason_survives_even_when_nothing_is_known():
    """"Free only if you join Prime" is worth keeping: it is why the answer is
    unknown, and a person reading the email can act on it."""
    explained = shipping.fact(shipping.UNKNOWN, None, "free delivery only with Prime")
    assert shipping.best_known(shipping.fact(), explained)["why"]


def test_nothing_known_anywhere_is_a_plain_unknown():
    assert shipping.best_known(shipping.fact(), None)["state"] == shipping.UNKNOWN


# --- landed price ------------------------------------------------------------

def test_a_known_cost_is_added():
    item = {"price": 100.0, "shipping": shipping.fact(shipping.EXTRA, 29.9, "")}
    assert shipping.landed(item) == 129.9


def test_free_delivery_leaves_the_price_alone():
    item = {"price": 100.0, "shipping": shipping.fact(shipping.FREE, 0.0, "")}
    assert shipping.landed(item) == 100.0


def test_unknown_delivery_leaves_the_price_alone_but_is_not_the_same_claim():
    """The two produce the same number and mean different things. `known()` is
    the only thing that tells them apart, and the email must use it."""
    free = {"price": 100.0, "shipping": shipping.fact(shipping.FREE, 0.0, "")}
    unknown = {"price": 100.0, "shipping": shipping.fact()}
    assert shipping.landed(free) == shipping.landed(unknown) == 100.0
    assert shipping.known(free) and not shipping.known(unknown)


def test_an_item_with_no_shipping_field_at_all_still_prices():
    assert shipping.landed({"price": 42.0}) == 42.0
    assert not shipping.known({"price": 42.0})


def test_describe_says_which_of_the_three_it_is():
    assert shipping.describe({"shipping": shipping.fact(shipping.FREE, 0.0, "")}) \
        == "free delivery"
    assert "29.9" in shipping.describe(
        {"shipping": shipping.fact(shipping.EXTRA, 29.9, "")})
    assert shipping.describe({}) == "delivery not included"
