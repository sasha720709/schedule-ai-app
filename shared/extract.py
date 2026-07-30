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

import json
import re
from dataclasses import dataclass, field

OK = "ok"
UNAVAILABLE = "unavailable"
FAILED = "failed"

KINDS = ("jsonpath", "css", "regex")
PARSERS = ("float", "currency", "int", "text", "bool")


class SpecError(ValueError):
    """The extractor spec itself is malformed -- a planning bug, not a page bug."""


@dataclass
class Extraction:
    status: str
    value: float | int | str | bool | None = None
    raw: str | None = None
    error: str | None = None
    notes: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == OK

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "value": self.value,
            "raw": self.raw,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Spec validation
# ---------------------------------------------------------------------------

_REQUIRED_FIELD = {"jsonpath": "path", "css": "selector", "regex": "pattern"}


def validate_spec(spec, *, _nested: bool = False) -> None:
    """Reject a malformed spec loudly, at plan time, not silently at tick time."""
    if not isinstance(spec, dict):
        raise SpecError("extractor must be an object")

    kind = spec.get("kind")
    if kind not in KINDS:
        raise SpecError(f"kind must be one of {', '.join(KINDS)}, got {kind!r}")

    field_name = _REQUIRED_FIELD[kind]
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

    parse = spec.get("parse", "text")
    if parse not in PARSERS:
        raise SpecError(f"parse must be one of {', '.join(PARSERS)}, got {parse!r}")

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


def _run_jsonpath(spec: dict, payload: str):
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        return None, f"response is not JSON: {exc}"

    value, error = _walk_json(spec["path"], document)
    if error:
        return None, error
    if value is None:
        return None, "path resolved to null"
    if isinstance(value, (dict, list)):
        return None, "path resolved to a container, not a value"
    return str(value), None


def _run_css(spec: dict, payload: str):
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover
        return None, "beautifulsoup4 is not installed"

    soup = BeautifulSoup(payload, "html.parser")
    try:
        node = soup.select_one(spec["selector"])
    except Exception as exc:  # noqa: BLE001 -- soupsieve raises its own types
        raise SpecError(f"invalid CSS selector: {exc}") from exc

    if node is None:
        return None, f"selector matched nothing: {spec['selector']!r}"

    # An attribute is often the honest source -- <meta content="629.00"> or
    # <span data-price="629.00"> beat the rendered text, which may be decorated.
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
        return None, f"pattern matched nothing: {spec['pattern']!r}"
    return (match.group(1) if compiled.groups else match.group(0)), None


_RUNNERS = {"jsonpath": _run_jsonpath, "css": _run_css, "regex": _run_regex}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _matches(spec: dict, payload: str) -> bool:
    """Did this predicate find anything? Used for unavailable_if."""
    raw, error = _RUNNERS[spec["kind"]](spec, payload)
    return error is None and raw is not None


def extract(spec: dict, payload: str) -> Extraction:
    """Run a spec against a fetched body. Never raises for page-shaped problems.

    A malformed *spec* raises SpecError, because that is a planning bug that
    should surface loudly. A page that does not match is a normal runtime
    outcome and comes back as a FAILED result instead.
    """
    validate_spec(spec)

    if payload is None or payload == "":
        return Extraction(FAILED, error="empty response body")

    # Availability is checked first. An out-of-stock page often still shows a
    # price, so reading the value and then asking "but is it real" would report
    # a number for something that cannot be bought.
    unavailable = spec.get("unavailable_if")
    if unavailable is not None and _matches(unavailable, payload):
        return Extraction(UNAVAILABLE, notes=["unavailable_if matched"])

    raw, error = _RUNNERS[spec["kind"]](spec, payload)
    if error is not None:
        return Extraction(FAILED, error=error)

    try:
        value = coerce(raw, spec.get("parse", "text"))
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
