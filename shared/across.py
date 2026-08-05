"""What a watch reads when it has more than one target.

Until now a watch with three shops had three readings and no *reading*. Each
target was checked alone, evaluated alone and fired alone, so "the price" was
whichever shop happened to tick last, and nothing in the system could answer
the question a shopper actually asks: **where is it cheapest right now?**

This module is the missing noun. Given a watch's target rows it produces one
ordered list of comparable readings and names the best of them.

## Why the *condition* is still evaluated per target

The obvious next step -- evaluate the condition against the aggregate -- turns
out to be the wrong one, for two reasons that are worth writing down because
the code looks like an omission otherwise.

**It changes nothing for a threshold.** `min(prices) < X` is true exactly when
some price is under X, and every shop is checked on the same interval. So the
watch fires at the same instant either way. This is not an approximation; it is
the same statement.

**And where it would differ, it would be worse.** A sibling's price is from
*its* last check, up to an interval old. Firing this tick on a neighbour's
older number means emailing "cheapest ILS 1,890 at Bug" about a price that may
already be gone, when Bug's own tick would confirm or correct it within
minutes. A watch about money should fire on a number it just read.

So: **fire on your own fresh reading, report the whole picture.** The aggregate
is what the email says, what the plan card explains, and what a relative
threshold is measured from -- not a second place where firing is decided.

Aggregation is only defined for the ordered operators. "Cheapest" means nothing
for `==` or `!=`, so those keep the per-target shape and say so.

## Staleness is reported, never hidden

A shop that has not answered for three intervals is not evidence about today's
price. It is dropped from the comparison and carried separately, so the email
can say "Amazon last answered 6 h ago" instead of quietly listing two shops
where the user asked about three. The same rule as `unchanged_checks`: report
it, do not act on it.
"""

from datetime import datetime, timedelta, timezone

BEST = "best"
ANY = "any"

# Which end of the range is "best", per operator. A watch for a price *under*
# something wants the cheapest offer; one for a price *over* something -- "tell
# me when it finally goes above 3000" -- is asking about the top of the market.
# Anything not listed here has no best, which is the honest answer for `==`.
DIRECTION = {"<": "min", "<=": "min", ">": "max", ">=": "max"}

# How many check intervals a reading stays usable for. Two, so a single missed
# tick -- a transient fetch failure, a shop timing out once -- does not make a
# shop vanish from the comparison, while a shop that has genuinely stopped
# answering drops out quickly enough that nobody is shown a stale price as
# though it were today's.
STALE_AFTER_INTERVALS = 2


class Reading:
    """One shop's current price, and how much to trust it."""

    __slots__ = ("target_id", "value", "currency", "url", "shop", "at",
                 "source", "stale")

    def __init__(self, *, target_id, value, currency=None, url=None, shop=None,
                 at=None, source="check", stale=False):
        self.target_id = target_id
        self.value = value
        self.currency = currency or ""
        self.url = url
        self.shop = shop
        self.at = at
        # "check" is a scheduled reading; "plan" is the one taken while the
        # plan was being verified. The second is a real reading of a real page
        # at a known time -- it is what makes the first tick after confirm able
        # to compare shops at all, instead of reporting one shop out of three.
        self.source = source
        self.stale = stale

    def as_dict(self) -> dict:
        return {
            "target_id": self.target_id,
            "value": self.value,
            "currency": self.currency,
            "url": self.url,
            "shop": self.shop,
            "at": self.at,
            "source": self.source,
            "stale": self.stale,
        }

    def __repr__(self):  # pragma: no cover -- debugging aid
        return (f"Reading({self.shop or self.target_id}, {self.value}"
                f"{' ' + self.currency if self.currency else ''}"
                f"{', stale' if self.stale else ''})")


def mode(condition) -> str:
    """Does this watch have a single best reading, or only separate ones?"""
    if not isinstance(condition, dict):
        return ANY
    if condition.get("across") != BEST:
        return ANY
    return BEST if direction(condition) else ANY


def direction(condition):
    """"min", "max", or None when the operator has no best."""
    if not isinstance(condition, dict):
        return None
    op = condition.get("op")
    if not isinstance(op, str):
        return None
    # Deliberately not `condition.normalise_op`: an unknown operator must reach
    # the Checker's evaluation and raise there, where it degrades the watch
    # loudly. Silently having "no direction" here would be the quiet answer
    # this project keeps removing.
    return DIRECTION.get(op.strip())


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _parse(stamp):
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def comparable(target, condition) -> bool:
    """Is this target priced in the same money the condition is written in?

    Currencies are never converted here. An ILS threshold compared against a
    USD price is how a watch reports a bargain that is not one, and a exchange
    rate is a second thing to be wrong about -- a stale rate produces a
    confident email about money. The Planner refuses to create a target that
    cannot be compared; this is the same rule applied to rows written before
    it did.

    A target with no currency at all is included: that is every kind but
    `product`, where there is one target and the question does not arise.
    """
    if not isinstance(condition, dict):
        return True
    wanted = (condition.get("currency") or "").strip().upper()
    mine = (target.get("currency") or "").strip().upper()
    return not wanted or not mine or wanted == mine


def reading_of(target, *, now, interval_min):
    """This target's current price, or None if it has none worth comparing.

    Order matters. A target that has been checked reports what that check
    found, and if that check found nothing -- out of stock, none of the pinned
    offers listed, a broken extractor -- then it has no price, full stop. It
    must **not** fall back to what it was worth at plan time; that number is
    weeks old and describes a page that has since said otherwise. The plan-time
    reading is used only by a target that has never been checked, which is the
    first tick after confirm and nothing else.
    """
    checked_at = target.get("last_checked_at")
    if checked_at:
        if target.get("last_status") != "ok":
            return None
        value, at, source = target.get("last_value"), checked_at, "check"
    else:
        value, at, source = (target.get("verified_value"),
                             target.get("verified_at"), "plan")

    number = _number(value)
    if number is None:
        return None

    taken = _parse(at)
    horizon = timedelta(minutes=max(1, int(interval_min or 60))
                        * STALE_AFTER_INTERVALS)
    # No timestamp is treated as stale rather than fresh. The reading is still
    # shown -- dropping it would hide a shop -- but it can never be the answer
    # to "what does this cost today".
    stale = taken is None or (now - taken) > horizon

    return Reading(
        target_id=target.get("target_id"),
        value=number,
        currency=target.get("currency"),
        url=target.get("url"),
        shop=target.get("shop") or _shop_of(target.get("url")),
        at=at,
        source=source,
        stale=stale,
    )


def _shop_of(url):
    """A name for the email when the row predates `shop` being stored."""
    if not isinstance(url, str) or "//" not in url:
        return None
    host = url.split("//", 1)[1].split("/", 1)[0]
    return host[4:] if host.startswith("www.") else host


def readings(targets, condition, *, now=None, interval_min=60, override=None):
    """Every comparable reading this watch has, best first.

    `override` replaces one target's stored row with the reading just taken, so
    the tick that is running compares its own fresh number rather than the one
    it is about to write. Without it the caller would be reading its own past.
    """
    now = now or datetime.now(timezone.utc)
    override = override or {}

    found = []
    for target in targets:
        if not comparable(target, condition):
            continue
        if target.get("target_id") in override:
            found.append(override[target["target_id"]])
            continue
        reading = reading_of(target, now=now, interval_min=interval_min)
        if reading is not None:
            found.append(reading)

    return order(found, condition)


def order(found, condition) -> list:
    """Best first, then stale ones, then by value.

    Stale readings sort last regardless of price, because a cheap price nobody
    has confirmed for hours is not a better answer than a real one -- it is a
    less certain one, and putting it at the top of an email would read as a
    recommendation.
    """
    reverse = direction(condition) == "max"
    return sorted(found, key=lambda r: (r.stale, -r.value if reverse else r.value))


def best(found, condition):
    """The single reading this watch's value is, or None.

    Never a stale one. If every shop has gone quiet there is no current price,
    and saying so is the point -- inventing one out of the freshest of the
    stale readings is how a watch reports last week as today.
    """
    if not direction(condition):
        return None
    fresh = [r for r in found if not r.stale]
    if not fresh:
        return None
    return order(fresh, condition)[0]


def describe(found) -> str:
    """One line for a log. The email formats these properly; this is for CloudWatch."""
    if not found:
        return "no comparable readings"
    return " | ".join(
        f"{r.shop or r.target_id} {r.value}{' ' + r.currency if r.currency else ''}"
        f"{' (stale)' if r.stale else ''}"
        for r in found
    )
