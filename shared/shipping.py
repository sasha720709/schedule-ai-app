"""What an offer costs to actually receive -- or, honestly, what is knowable.

## The measurement this module is built on, not the assumption it replaced

The roadmap said: read the shipping cost and add it to the price, because an
item ILS 50 cheaper with ILS 60 shipping is not cheaper. That is right, and it
is only sometimes possible. Measured 2026-08-05 across two Israeli shops and 38
Amazon cards in two searches:

    ivory.co.il   nothing. `deliveryTime` is a *duration* (0-3 days handling,
                  1-5 in transit), and the one Hebrew free-shipping phrase on
                  the page appears inside three product *titles*
    bug.co.il     nothing per offer. A site-wide banner, in an image's alt
                  text: free over ILS 179, free same-day in the centre over 799
    amazon.com    a real delivery element on 12 of 16 console cards -- 11 free,
                  **one at $35** -- and on 22 of 22 cheap cards, where every
                  single one is conditional and therefore unknown

So a delivery cost is readable sometimes and absent most of the time, and the
absent case is the common one. This module therefore does not produce a number.
It produces **what is known**, in three states, and the product says which --
because the harm the roadmap named is caused by the claim as much as by the
arithmetic: an email saying "cheapest at Bug" reads as a finished comparison
whether or not delivery was ever considered.

## The trap that a substring match walks straight into

"FREE delivery" appears on 14 of 14 cheap Amazon cards. On every one of them
it is **false** for someone buying that item alone:

    Join Prime to get FREE delivery Fri, Aug 7
    Or Non-members get FREE delivery Mon, Aug 10 on $35 of items shipped by Amazon

Free *if you subscribe*, or free *if you spend $35*. A `"FREE delivery" in
text` check would have marked a $4.59 cable as free shipping, confidently, and
been wrong every time. So a claim of free delivery is only believed when it
carries no condition, and the conditions are listed rather than guessed at.

The same reasoning rules out reading Hebrew free-shipping text off a card:
Ivory sells three Thrustmaster wheels whose *names* end in "משלוח חינם". That
is the whole-document `unavailable_if` bug in a new costume -- a phrase found
somewhere on a page is not a fact about the thing next to it.

## What is believed, in order

1. **`schema.org` `shippingDetails`.** Nobody probed publishes it yet. It is
   read first anyway, because it is a published contract and the day a shop
   adds it this gets better with no code change. Same argument as `offers`.
2. **The shop's own delivery element**, where the shop has one that means
   only delivery -- Amazon's `[data-cy="delivery-block"]`. Not card text.
3. **The shop's published free-shipping threshold**, from `shops.py`, applied
   to the offer's own price. A human put that number there having read the
   shop's own page.

Anything else is `unknown`, and `unknown` is a first-class answer here for the
same reason `unavailable` is one in `extract.py`: the alternative is a system
that quietly reports a guess as a fact.
"""

import re

FREE = "free"
EXTRA = "extra"
UNKNOWN = "unknown"

# Phrases that turn "FREE delivery" into "free delivery, if...". Any of these
# in the delivery text means the claim is conditional on something this system
# cannot know -- a subscription, or a basket bigger than the one thing being
# watched. Measured on real Amazon cards; see the module docstring.
CONDITIONAL = (
    "join prime",
    "prime members",
    "non-members get",
    "members get",
    "of items",
    "on orders over",
)

_FREE = re.compile(r"\bfree\b[^.]{0,20}\b(delivery|shipping)\b", re.I)
_COST = re.compile(
    r"(?:[$€£₪]|USD|EUR|ILS|GBP)\s*([\d][\d,.]*)\s*(?:delivery|shipping)"
    r"|(?:delivery|shipping)[^.\d]{0,12}(?:[$€£₪]|USD|EUR|ILS|GBP)\s*([\d][\d,.]*)",
    re.I)


def fact(state=UNKNOWN, amount=None, why="") -> dict:
    """One shipping fact, in the shape stored on an offer."""
    return {"state": state, "amount": amount, "why": why}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def from_schema(offer) -> dict:
    """`schema.org/OfferShippingDetails`, the answer nobody publishes yet.

    Read first regardless, because it is a contract rather than a layout: a
    shop that adds it tomorrow makes every watch on it more accurate with no
    change here. A `shippingRate` of zero is a real statement of free delivery
    and is the one case where "free" is known rather than inferred.
    """
    if not isinstance(offer, dict):
        return fact()
    details = offer.get("shippingDetails")
    if isinstance(details, list):
        details = next((d for d in details if isinstance(d, dict)), None)
    if not isinstance(details, dict):
        return fact()

    rate = details.get("shippingRate")
    if isinstance(rate, list):
        rate = next((r for r in rate if isinstance(r, dict)), None)
    if not isinstance(rate, dict):
        return fact()

    try:
        value = float(str(rate.get("value")).replace(",", ""))
    except (TypeError, ValueError):
        return fact()

    if value == 0:
        return fact(FREE, 0.0, "the shop publishes free shipping")
    return fact(EXTRA, value, f"the shop publishes {value:g} shipping")


def from_text(text) -> dict:
    """A shop's own delivery element, read as a sentence rather than searched.

    Only ever called with text taken from an element that means delivery and
    nothing else. Handing it a whole product card is the bug in the module
    docstring, so callers pass the block, never the card.
    """
    if not isinstance(text, str) or not text.strip():
        return fact()
    line = " ".join(text.split())
    low = line.lower()

    match = _COST.search(line)
    if match:
        raw = (match.group(1) or match.group(2) or "").replace(",", "")
        try:
            return fact(EXTRA, float(raw), f"the page says {line[:60]}")
        except ValueError:
            pass

    if _FREE.search(low):
        condition = next((c for c in CONDITIONAL if c in low), None)
        if condition:
            # Free, but not for someone buying this one thing. Saying "free"
            # here is how a $4.59 cable acquires free delivery it does not have.
            return fact(UNKNOWN, None,
                        f"free delivery only {line[:60]}")
        return fact(FREE, 0.0, "the page says delivery is free")

    return fact()


def from_threshold(price, free_over, currency="") -> dict:
    """The shop's published free-shipping threshold, against this offer's price.

    A fact about the shop, put in `shops.py` by a human who read the shop's own
    page, applied to a number read off today's listing. It answers the case
    that matters most -- a ILS 2,679 console is far above every threshold
    probed, so its shipping is free and its sticker price *is* its landed
    price.

    It cannot answer the other direction. Below the threshold the shipping cost
    is a number this system has never seen, so the answer is `unknown` and not
    "the threshold minus the price" or any other invention.
    """
    amount = _number(price)
    limit = _number(free_over)
    if amount is None or limit is None:
        return fact()
    if amount >= limit:
        unit = f" {currency}" if currency else ""
        return fact(FREE, 0.0,
                    f"over this shop's{unit} {limit:g} free-shipping threshold")
    return fact(UNKNOWN, None,
                f"under this shop's free-shipping threshold of {limit:g}")


def best_known(*facts) -> dict:
    """The first fact that actually says something, in the caller's order.

    Deliberately first-wins rather than cheapest-wins. The order is a
    statement about *evidence* -- a published field beats an element beats a
    threshold -- and picking the most flattering answer from several sources
    is how a comparison site becomes an advertisement.
    """
    for one in facts:
        if isinstance(one, dict) and one.get("state") in (FREE, EXTRA):
            return one
    for one in facts:
        if isinstance(one, dict) and one.get("why"):
            return one
    return fact()


def landed(item) -> float:
    """Price plus shipping, where shipping is a number. Never a guess.

    An `unknown` shipping cost returns the price unchanged, which is
    deliberately the same value a free-shipping offer produces. The two are
    *not* the same claim, and nothing may present them as one -- that is what
    `state` is for and why `known()` exists next to this.
    """
    price = _number(item.get("price")) or 0.0
    ship = item.get("shipping") or {}
    if ship.get("state") == EXTRA:
        return round(price + (_number(ship.get("amount")) or 0.0), 4)
    return price


def known(item) -> bool:
    """Is this offer's landed price a fact rather than a floor?"""
    return (item.get("shipping") or {}).get("state") in (FREE, EXTRA)


def describe(item) -> str:
    """A phrase for the email, or empty when there is nothing worth saying."""
    ship = item.get("shipping") or {}
    state = ship.get("state")
    if state == FREE:
        return "free delivery"
    if state == EXTRA:
        amount = _number(ship.get("amount"))
        return f"+{amount:g} delivery" if amount else "delivery included"
    return "delivery not included"
