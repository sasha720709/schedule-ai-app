"""Deterministic extraction: read one value out of a page, with no model.

This is the core of Phase 8. Today a language model re-reads 20,000 characters
on every tick and re-solves a problem that was already solved once at planning
time. Here the Planner compiles a spec instead, and this module executes it for
roughly four millionths of a dollar.

## Three outcomes, not two

The single most important thing in this file is that extraction has *three*
results, not a value-or-nothing:

    ok            a value was read
    unavailable   there is deliberately no value right now -- out of stock,
                  not listed, closed. The watch is healthy; the answer is
                  simply "not yet".
    failed        the spec did not match. Something is wrong with the
                  extractor, not with the world.

Collapsing `unavailable` and `failed` into "no value" is exactly how a
deterministic checker rots silently: a selector whose page was redesigned
returns nothing, nothing reads as "condition not met", and a watch that died
weeks ago keeps reassuring you. Phase 8d escalates `failed` to a repair; it
must never escalate `unavailable`, which is a normal Tuesday.

## `scope`, and why absence needs an anchor

Telling those two apart is impossible from a miss alone. "This selector found
nothing" is the same observation whether the page was rebuilt or the job you
are waiting for simply has not been posted. `scope` is what resolves it: an
optional CSS selector that narrows the document first, and doubles as a
liveness test for the page's shape.

    scope matches nothing            -> FAILED. The page changed. Repair it.
    scope matches, target does not   -> UNAVAILABLE. Not yet. Leave it alone.

Without a scope nothing is anchored, so every miss stays FAILED -- the
conservative reading, and the behaviour this module had before scope existed.

Scoping is not a storefront concern. It was found on a 1.5MB product page
where "Out of stock" matched a *different product* and a table of localised
strings, but it matters at least as much for a vacancy board, where the
question is "is there a role in this country" and the page is a list of
hundreds of things that are not it.

## Why `count` exists

A price is always on the page; you are waiting for it to change. A vacancy, a
restock, an appointment slot or a new release is *absent by definition* until
the moment the watch is meant to fire. An engine that can only read values
that already exist cannot express that class of watch at all -- and worse,
reports it as a broken extractor forever, which 8d would answer by paying for
a repair on every single tick.

`count` makes zero an answer instead of a failure.

This distinction came from a real observation. Asked to read a Steam Deck
price, Haiku returned `null` and noted the item showed "Out of stock" -- while
a naive regex would have cheerfully extracted `$629.00` and reported a
purchasable item that cannot be bought. `unavailable_if` is how a spec keeps
that judgement without keeping the model.

## Why these three kinds

`jsonpath` is first among equals. A JSON endpoint is the cheapest, most stable
source of a fact, and the Planner is meant to prefer one -- once the model
leaves the hot path the browser becomes the dominant cost, ~80x a plain fetch.

`css` handles ordinary HTML. It uses BeautifulSoup with soupsieve, both pure
Python: `lxml` would be faster but ships compiled wheels, and this project
already has a documented fragility where `pip install -t` vendors
platform-specific binaries that work only because the Codespace happens to
match the Lambda runtime. Nothing here makes that worse.

`regex` is the escape hatch for text that is neither, and for `unavailable_if`
predicates like "out of stock" that are a phrase rather than an element.
"""

import hashlib
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from dataclasses import dataclass, field

OK = "ok"
UNAVAILABLE = "unavailable"
FAILED = "failed"

KINDS = ("jsonpath", "css", "regex", "count", "offers")
PARSERS = ("float", "currency", "int", "text", "bool")
# A count is a number of elements, so the only sensible coercions are the
# number itself or "is it more than none".
COUNT_PARSERS = ("int", "bool")
# An offer list yields money. The scalar it reduces to is the cheapest price.
OFFER_PARSERS = ("float", "currency")


class SpecError(ValueError):
    """The extractor spec itself is malformed -- a planning bug, not a page bug."""


class _Missed(str):
    """An error meaning "the target was not present", not "this extractor broke".

    The difference is the whole point of `scope`. Inside a proven anchor, a
    target that is simply not there is a legitimate absence; outside one it is
    indistinguishable from a redesign, and stays FAILED.
    """


# A count can match a page listing hundreds of things. Enough to show a person
# what appeared, bounded so a target row cannot grow without limit -- DynamoDB
# stops at 400KB per item and a watch that has been running for months would
# find that ceiling.
MAX_ITEMS = 25

# How much of one item's text is kept. A job title is short; the surrounding
# card can be a paragraph.
MAX_ITEM_TEXT = 200


@dataclass
class Extraction:
    status: str
    value: float | int | str | bool | None = None
    raw: str | None = None
    error: str | None = None
    notes: list = field(default_factory=list)
    # What a `count` actually matched, not merely how many. Empty for every
    # other kind. This is the difference between an email that says "1" and one
    # that says which job appeared and links to it.
    items: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == OK

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "value": self.value,
            "raw": self.raw,
            "error": self.error,
            "items": self.items,
        }


# ---------------------------------------------------------------------------
# Spec validation
# ---------------------------------------------------------------------------

_REQUIRED_FIELD = {
    "jsonpath": "path",
    "css": "selector",
    "regex": "pattern",
    "count": "selector",
    # `offers` reads a standard first; `selector` is an optional fallback for
    # shops that publish none, so nothing is required.
    "offers": None,
}


def default_parse(kind: str) -> str:
    if kind == "count":
        return "int"
    if kind == "offers":
        return "float"
    return "text"


def validate_spec(spec, *, _nested: bool = False) -> None:
    """Reject a malformed spec loudly, at plan time, not silently at tick time."""
    if not isinstance(spec, dict):
        raise SpecError("extractor must be an object")

    kind = spec.get("kind")
    if kind not in KINDS:
        raise SpecError(f"kind must be one of {', '.join(KINDS)}, got {kind!r}")

    field_name = _REQUIRED_FIELD[kind]
    if field_name is not None:
        value = spec.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise SpecError(f"{kind} extractor needs a non-empty {field_name!r}")

    if kind == "regex":
        try:
            compiled = re.compile(value)
        except re.error as exc:
            raise SpecError(f"invalid regex: {exc}") from exc
        # A capture group is how the pattern says which part is the value. With
        # none, the whole match is used -- allowed, but worth being explicit.
        if compiled.groups > 1:
            raise SpecError("regex may have at most one capture group")

    parse = spec.get("parse", default_parse(kind))
    allowed = (COUNT_PARSERS if kind == "count"
               else OFFER_PARSERS if kind == "offers"
               else PARSERS)
    if parse not in allowed:
        raise SpecError(f"parse must be one of {', '.join(allowed)}, got {parse!r}")

    scope = spec.get("scope")
    if scope is not None:
        if _nested:
            raise SpecError(
                "scope belongs on the outer spec -- unavailable_if is already "
                "evaluated inside it"
            )
        if not isinstance(scope, str) or not scope.strip():
            raise SpecError("scope must be a non-empty CSS selector")

    unavailable = spec.get("unavailable_if")
    if unavailable is not None:
        if _nested:
            raise SpecError("unavailable_if cannot itself contain unavailable_if")
        validate_spec(unavailable, _nested=True)


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------

_NUMBER_RUN = re.compile(r"-?\d[\d.,\s  ']*")


def parse_number(raw: str) -> float:
    """Turn "$1,234.56", "1.234,56 €" or "779,00€" into a float.

    Phase 6 saw both conventions from the same page depending on where the
    fetch came from, so neither can be assumed.

    The rule: whichever of `.` or `,` appears last is the decimal separator. A
    lone comma counts as decimal only when one or two digits follow it, since
    "1,234" is thousands and "779,00" is not. A lone dot is treated as a
    decimal point, which makes "1.234" ambiguous -- read here as 1.234 rather
    than 1234. That ambiguity is unavoidable without knowing the locale, and it
    is why the Planner stores a verified value at plan time: a gross mismatch
    against it is detectable.
    """
    match = _NUMBER_RUN.search(raw)
    if not match:
        raise ValueError(f"no number in {raw[:60]!r}")

    text = re.sub(r"[\s  ']", "", match.group(0)).rstrip(".,")
    if not text or text in "-":
        raise ValueError(f"no number in {raw[:60]!r}")

    has_dot, has_comma = "." in text, "," in text
    if has_dot and has_comma:
        if text.rfind(".") > text.rfind(","):
            text = text.replace(",", "")
        else:
            text = text.replace(".", "").replace(",", ".")
    elif has_comma:
        head, _, tail = text.rpartition(",")
        text = f"{head}.{tail}" if len(tail) in (1, 2) and "," not in head \
            else text.replace(",", "")

    return float(text)


_CURRENCY_HINT = re.compile(
    r"[$€£¥₹₽¢]|\b(?:usd|eur|gbp|jpy|inr|rub|cad|aud|chf|sek|nok|pln|dkk|brl|mxn)\b",
    re.IGNORECASE,
)
_MINOR_UNIT = re.compile(r"\d[.,]\d{2}(?!\d)")


def parse_currency(raw: str) -> float:
    """Stricter than parse_number, because money is not just any number.

    Found by a test: asked to read a price from the element containing
    "Steam Deck 512 GB OLED - Valve Certified Refurbished", the permissive
    parser returned 512.0 -- a capacity read as a price. A watch for "under
    $600" would have fired immediately, on a product name.

    So money has to look like money: either a currency symbol or code sits
    nearby, or the number carries a two-digit minor unit. "$629.00" and
    "779,00€" pass; "512 GB" does not. Use `float` when a bare number really is
    the value.
    """
    value = parse_number(raw)
    if _CURRENCY_HINT.search(raw) or _MINOR_UNIT.search(raw):
        return value
    raise ValueError(
        f"no currency symbol and no minor unit in {raw[:60]!r} -- refusing to "
        "read a bare number as money"
    )


_TRUTHY = {"true", "yes", "1", "in stock", "available", "on"}


def coerce(raw: str, parse: str):
    if parse == "text":
        return raw.strip()
    if parse == "currency":
        return parse_currency(raw)
    if parse == "float":
        return parse_number(raw)
    if parse == "int":
        return int(round(parse_number(raw)))
    if parse == "bool":
        return raw.strip().lower() in _TRUTHY
    raise SpecError(f"unknown parse {parse!r}")


# ---------------------------------------------------------------------------
# The three kinds
# ---------------------------------------------------------------------------

_PATH_TOKEN = re.compile(
    r"""\.([A-Za-z_][\w-]*)      # .key
      | \[(\d+)\]                # [0]
      | \['([^']*)'\]            # ['key']
      | \["([^"]*)"\]            # ["key"]
      | ^([A-Za-z_][\w-]*)       # leading bare key
    """,
    re.VERBOSE,
)


def _walk_json(path: str, document):
    """A deliberately small JSONPath: dotted keys and integer indices only.

    No wildcards, filters or recursive descent. Those need a real parser and a
    dependency, and none of them belong in a spec a Planner has to get right
    unattended -- a path it cannot verify is a path that will break quietly.
    """
    cursor = document
    remainder = path.strip()
    if remainder.startswith("$"):
        remainder = remainder[1:]

    consumed = 0
    for match in _PATH_TOKEN.finditer(remainder):
        if match.start() != consumed:
            raise SpecError(f"cannot parse path near {remainder[consumed:][:20]!r}")
        consumed = match.end()

        key, index, quoted, dquoted, bare = match.groups()
        if index is not None:
            if not isinstance(cursor, list):
                return None, f"expected a list at {remainder[:consumed]!r}"
            position = int(index)
            if position >= len(cursor):
                return None, f"index {position} out of range at {remainder[:consumed]!r}"
            cursor = cursor[position]
        else:
            name = key or quoted or dquoted or bare
            if not isinstance(cursor, dict) or name not in cursor:
                return None, f"no key {name!r} at {remainder[:consumed]!r}"
            cursor = cursor[name]

    if consumed != len(remainder):
        raise SpecError(f"cannot parse path near {remainder[consumed:][:20]!r}")
    return cursor, None


def _soup(payload: str):
    """Imported lazily so a JSON-only deployment need not carry beautifulsoup4."""
    from bs4 import BeautifulSoup

    return BeautifulSoup(payload, "html.parser")


def _run_jsonpath(spec: dict, payload: str):
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        return None, f"response is not JSON: {exc}"

    value, error = _walk_json(spec["path"], document)
    if error:
        # A key that is not there, or an index past the end, is the JSON
        # equivalent of a selector matching nothing: possibly a redesign,
        # possibly just a field that is absent today.
        return None, _Missed(error)
    if value is None:
        return None, _Missed("path resolved to null")
    if isinstance(value, (dict, list)):
        return None, "path resolved to a container, not a value"
    return str(value), None


def _run_css(spec: dict, payload: str):
    try:
        soup = _soup(payload)
    except ImportError:  # pragma: no cover
        return None, "beautifulsoup4 is not installed"

    try:
        node = soup.select_one(spec["selector"])
    except Exception as exc:  # noqa: BLE001 -- soupsieve raises its own types
        raise SpecError(f"invalid CSS selector: {exc}") from exc

    if node is None:
        return None, _Missed(f"selector matched nothing: {spec['selector']!r}")

    # An attribute is often the honest source -- <meta content="629.00"> or
    # <span data-price="629.00"> beat the rendered text, which may be decorated.
    # A matched element that lacks the attribute is breakage, not absence: the
    # spec was written against markup that no longer looks like this.
    attribute = spec.get("attribute")
    if attribute:
        if not node.has_attr(attribute):
            return None, f"matched element has no {attribute!r} attribute"
        return str(node[attribute]), None

    return node.get_text(" ", strip=True), None


def _run_regex(spec: dict, payload: str):
    compiled = re.compile(spec["pattern"], re.IGNORECASE | re.DOTALL)
    match = compiled.search(payload)
    if match is None:
        return None, _Missed(f"pattern matched nothing: {spec['pattern']!r}")
    return (match.group(1) if compiled.groups else match.group(0)), None


def _run_count(spec: dict, payload: str):
    """How many elements match. Zero is an answer, not a failure.

    This is the shape of most watches that are not about a price: is there a
    vacancy, has a slot opened, did a new release appear. Those things are
    absent by definition until the moment the watch is supposed to fire, and an
    engine that can only read values that already exist cannot express them.

    Zero being a legitimate value is exactly why `count` needs `scope` more
    than the other kinds do, not less: without an anchor, a selector counting
    nothing because the page was rebuilt is indistinguishable from one counting
    nothing because the job has not been posted yet -- and the second is
    reported forever, silently.
    """
    try:
        soup = _soup(payload)
    except ImportError:  # pragma: no cover
        return None, "beautifulsoup4 is not installed"

    try:
        matches = soup.select(spec["selector"])
    except Exception as exc:  # noqa: BLE001
        raise SpecError(f"invalid CSS selector: {exc}") from exc

    return str(len(matches)), None


# Query parameters that identify the *request*, not the thing. Stripped before
# an item's identity is computed.
#
# Found the hard way. LinkedIn's job links carry a fresh `refId` and
# `trackingId` on every single response, so identity built from the raw href
# changed on every check -- and a repeating watch would have re-reported every
# job, every tick, forever. That is precisely the spam the deduplication exists
# to prevent, and no offline test could have seen it: it appears only when the
# same page is fetched twice.
#
# A denylist rather than "strip the whole query", because plenty of sites put
# the identity *in* the query -- `/jobs?id=123` must stay distinct.
_TRACKING_PARAMS = {
    "refid", "trackingid", "position", "pagenum", "trk", "trkinfo",
    "originaltrackingid", "fbclid", "gclid", "msclkid", "igshid", "ref",
    "referrer", "source", "src",
}

# Attributes carrying a site's own stable id for a listing. Preferred over the
# URL when present: a site that rewrites its links on every request usually
# still keys its markup on the real thing.
#
# `data-asin` is Amazon's product id and was added after watching this fail
# live. Amazon's result links are sponsored-click redirects carrying a base64
# blob that changes every request, and the plain ones embed the result's
# *position* in the path (`/ref=sr_1_3`) -- so the same console at a different
# position was a different item, and a pinned product vanished on the next
# check. Deliberately NOT here: `data-uuid` and `data-index`, which Amazon also
# sets and which are per-render.
_ID_ATTRS = ("data-entity-urn", "data-asin", "data-sku", "data-product-id",
             "data-job-id", "data-jobid", "data-id")


def _stable_key(node, href: str, text: str) -> str:
    """What makes this item *this* item, across fetches.

    Three levels, each more reliable than the next one down:

    1. **The site's own id.** Definitive when it exists.
    2. **The link, with tracking parameters removed.** Stable for most boards.
    3. **The visible text.** Only when there is no link at all.

    Text is deliberately *not* mixed into the first two. A LinkedIn card reads
    "DevOps Engineer Leidos Be'er Sheva 2 days ago" -- and "2 days ago" becomes
    "3 days ago", which would make the same posting a new posting overnight.
    Volatile text is exactly what the levels above exist to avoid.
    """
    for attr in _ID_ATTRS:
        found = node.get(attr)
        if not found:
            inner = node.find(attrs={attr: True})
            found = inner.get(attr) if inner else None
        if found:
            return str(found)

    if href:
        parsed = urlsplit(href)
        kept = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                if k.lower() not in _TRACKING_PARAMS]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                          urlencode(kept), ""))

    return text


def _item_href(node) -> str:
    """The link for a matched item.

    Left **relative** if the page wrote it relative. This module only ever sees
    a payload, never the URL it came from, so joining is done where the URL is
    known -- the Notifier. Guessing a base here would be inventing a fact.
    """
    href = node.get("href") if node.name == "a" else None
    if not href:
        link = node.find("a", href=True)
        href = link["href"] if link else ""
    return str(href or "").strip()


_LD_BLOCK = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I)


def _ld_products(payload: str) -> list:
    """Every schema.org Product with a price, from a page's JSON-LD.

    ## Why a standard beats a selector

    Every other kind here points at markup: a class, an id, a path through a
    site's own JSON. All of them are guesses about a page that will be
    redesigned. `schema.org/Product` is a **published contract** -- shops emit
    it for Google, so they keep it working, and it survives the redesigns that
    break a CSS selector.

    It also carries what a price watch actually needs and a selector usually
    cannot reach: the name, the price, the *currency*, whether it is in stock,
    the link to that specific offer, and an `sku` -- a stable identity, which
    is what deduplication has needed at every step of this project.

    Verified 2026-08-04: ivory.co.il publishes an `ItemList` of 16 priced
    Products on a plain search URL, over plain HTTP, no browser. bug.co.il,
    zap.co.il and amazon.com do not, which is why this is the preferred path
    rather than the only one.
    """
    products = []

    def walk(node):
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        if not isinstance(node, dict):
            return
        if node.get("@type") == "Product":
            products.append(node)
        # ItemList wraps each entry, and @graph is the other common shape.
        for key in ("itemListElement", "@graph", "item"):
            if key in node:
                walk(node[key])

    for block in _LD_BLOCK.findall(payload):
        try:
            walk(json.loads(block))
        except ValueError:
            continue  # one malformed block must not lose the others
    return products


def _offer_of(product: dict) -> dict | None:
    """The first priced offer on a Product, flattened."""
    offers = product.get("offers")
    if isinstance(offers, list):
        offers = next((o for o in offers if isinstance(o, dict)), None)
    if not isinstance(offers, dict) or offers.get("price") in (None, ""):
        return None
    try:
        price = float(str(offers["price"]).replace(",", ""))
    except ValueError:
        return None

    name = " ".join(str(product.get("name") or "").split())[:MAX_ITEM_TEXT]
    url = str(offers.get("url") or product.get("url") or "")
    sku = str(product.get("sku") or product.get("mpn") or "")
    available = str(offers.get("availability") or "")
    return {
        # sku when the shop gives one: a stable identity that survives the
        # link being rewritten, which is exactly what LinkedIn taught us.
        "id": hashlib.sha1((sku or url or name).encode()).hexdigest()[:12],
        "text": name or "(unnamed)",
        "href": url,
        "price": price,
        "currency": str(offers.get("priceCurrency") or ""),
        "in_stock": available.endswith("InStock"),
    }


# A price as a shop writes it, anywhere in a card's text.
_PRICE_IN_TEXT = re.compile(
    r"(?:[$€£₪]|USD|EUR|ILS|GBP)\s*([\d][\d,.\s]*)|([\d][\d,.]*)\s*(?:[$€£₪]|USD|EUR|ILS|GBP)")


def _selector_offers(spec: dict, payload: str) -> list:
    """The fallback: offers read off cards, for shops that publish no standard.

    Worse than JSON-LD in every way that matters -- no currency, no stock, no
    sku, and a price scraped out of prose -- but it is what Amazon and most
    shops leave us. The first money-shaped number in a card is the price the
    card is advertising; where a shop shows a struck-through original it comes
    second, so this takes the first and is right more often than not.
    """
    selector = spec.get("selector")
    if not selector:
        return []
    try:
        soup = _soup(payload)
        nodes = soup.select(selector)
    except Exception:  # noqa: BLE001
        return []

    offers = []
    for node in nodes[:MAX_ITEMS]:
        text = " ".join(node.get_text(" ", strip=True).split())[:MAX_ITEM_TEXT]
        match = _PRICE_IN_TEXT.search(text)
        if not match:
            continue
        try:
            price = parse_number(match.group(1) or match.group(2))
        except ValueError:
            continue
        href = _item_href(node)
        offers.append({
            "id": hashlib.sha1(
                _stable_key(node, href, text).encode()).hexdigest()[:12],
            "text": text,
            "href": href,
            "price": price,
            "currency": "",
            "in_stock": True,
        })
    return offers


def _offers_on(spec: dict, payload: str) -> list:
    """Every priced offer on the page, cheapest first.

    The standard is tried first and a selector is the fallback, never the other
    way round: JSON-LD carries currency, stock and a stable sku, and a shop
    keeps it working because Google reads it.
    """
    offers = [o for o in (_offer_of(p) for p in _ld_products(payload)) if o]
    if not offers:
        offers = _selector_offers(spec, payload)
    return sorted(offers, key=lambda o: o["price"])


def _run_offers(spec: dict, payload: str):
    """Cheapest priced offer on the page. Zero offers is not a failure."""
    offers = _offers_on(spec, payload)
    if not offers:
        return None, _Missed("no priced offer found on this page")
    return str(offers[0]["price"]), None


def _count_items(spec: dict, payload: str) -> list:
    """What the count matched, as text and link.

    `count` returned an integer and nothing else, so a triggered vacancy watch
    emailed the user the word "1" and a link to the *search page* -- leaving
    them to go and find the posting themselves, which is most of the work they
    asked to be spared.

    Identity is computed here rather than by each caller so that "the same
    posting" means one thing everywhere: the Checker deduplicates on it, and
    the email is written from it.
    """
    try:
        soup = _soup(payload)
        matches = soup.select(spec["selector"])
    except Exception:  # noqa: BLE001 -- the count already succeeded; this is extra
        return []

    items = []
    for node in matches[:MAX_ITEMS]:
        text = " ".join(node.get_text(" ", strip=True).split())[:MAX_ITEM_TEXT]
        href = _item_href(node)
        if not text and not href:
            continue
        items.append({
            "id": hashlib.sha1(
                _stable_key(node, href, text).encode()).hexdigest()[:12],
            "text": text,
            "href": href,
        })
    return items


_RUNNERS = {
    "offers": _run_offers,
    "jsonpath": _run_jsonpath,
    "css": _run_css,
    "regex": _run_regex,
    "count": _run_count,
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _apply_scope(selector: str, payload: str):
    """Narrow the document to one element. Returns (node, error).

    This is the anchor test for the whole spec. A scope that matches nothing
    means the page no longer has the shape the plan was written against -- the
    one honest signal available that an extractor is broken rather than early.
    """
    try:
        soup = _soup(payload)
    except ImportError:  # pragma: no cover
        return None, "beautifulsoup4 is not installed"

    try:
        node = soup.select_one(selector)
    except Exception as exc:  # noqa: BLE001
        raise SpecError(f"invalid CSS selector in scope: {exc}") from exc

    if node is None:
        return None, f"scope matched nothing: {selector!r}"
    return node, None


def _scoped_payload(node, kind: str) -> str:
    """Markup for the selector-shaped kinds; text for jsonpath.

    The jsonpath case is deliberate rather than incidental. Scoping to
    `script[type="application/ld+json"]` and then reading a path out of it is
    frequently the cheapest and most stable source of a fact on an HTML page,
    and preferring exactly that kind of source is the Planner's new job.
    """
    return node.get_text() if kind == "jsonpath" else str(node)


def _matches(spec: dict, payload: str) -> bool:
    """Did this predicate find anything? Used for unavailable_if."""
    raw, error = _RUNNERS[spec["kind"]](spec, payload)
    if spec["kind"] == "count":
        return error is None and int(raw) > 0
    return error is None and raw is not None


def extract(spec: dict, payload: str) -> Extraction:
    """Run a spec against a fetched body. Never raises for page-shaped problems.

    A malformed *spec* raises SpecError, because that is a planning bug that
    should surface loudly. A page that does not match is a normal runtime
    outcome and comes back as a FAILED or UNAVAILABLE result instead.
    """
    validate_spec(spec)

    if payload is None or payload == "":
        return Extraction(FAILED, error="empty response body")

    kind = spec["kind"]
    parse = spec.get("parse", default_parse(kind))

    # --- the anchor ------------------------------------------------------
    # Everything below runs against the scoped fragment when there is one.
    # Without a scope, `body` is the whole document and nothing is anchored,
    # so every miss stays FAILED -- the pre-scope behaviour, unchanged.
    scope = spec.get("scope")
    node, body = None, payload
    if scope is not None:
        node, error = _apply_scope(scope, payload)
        if error is not None:
            return Extraction(FAILED, error=error)
        body = _scoped_payload(node, kind)

    # --- availability ----------------------------------------------------
    # Checked first: an out-of-stock page usually still shows a price, so
    # reading the value and then asking "but is it real" would report a number
    # for something nobody can buy. Evaluated inside the scope, because on a
    # real page "Out of stock" is nearly always somewhere -- another product,
    # a hidden template, a localised string table.
    unavailable = spec.get("unavailable_if")
    if unavailable is not None:
        predicate_body = (
            _scoped_payload(node, unavailable["kind"]) if node is not None else payload
        )
        if _matches(unavailable, predicate_body):
            return Extraction(UNAVAILABLE, notes=["unavailable_if matched"])

    raw, error = _RUNNERS[kind](spec, body)
    if error is not None:
        # Inside a proven anchor, "not there" means not there *yet*. Outside
        # one it is indistinguishable from a redesign and must stay FAILED,
        # because 8d escalates FAILED and deliberately ignores UNAVAILABLE.
        if node is not None and isinstance(error, _Missed):
            return Extraction(
                UNAVAILABLE, notes=[f"nothing to read inside {scope!r}: {error}"]
            )
        return Extraction(FAILED, error=error)

    if kind == "count":
        count = int(raw)
        return Extraction(OK, value=(count > 0) if parse == "bool" else count,
                          raw=raw, items=_count_items(spec, body))

    if kind == "offers":
        found = _offers_on(spec, body)
        return Extraction(OK, value=found[0]["price"], raw=raw, items=found)

    try:
        value = coerce(raw, parse)
    except ValueError as exc:
        return Extraction(FAILED, raw=raw, error=f"could not parse {raw[:60]!r}: {exc}")

    return Extraction(OK, value=value, raw=raw.strip() if isinstance(raw, str) else raw)


def plausible(value, baseline, tolerance: float = 10.0) -> bool:
    """Is a reading believable next to the one verified at plan time?

    A selector that survives a redesign but now points at a different number --
    a shipping cost, a review count, next month's price -- fails silently in a
    way a missing selector does not. Comparing against the plan-time value is
    the cheap defence. Ten-fold is deliberately loose: this catches a spec
    reading the wrong element, not a genuine price move.
    """
    if baseline in (None, 0) or not isinstance(value, (int, float)):
        return True
    if isinstance(value, bool):
        return True
    ratio = abs(float(value)) / abs(float(baseline))
    return 1 / tolerance <= ratio <= tolerance
