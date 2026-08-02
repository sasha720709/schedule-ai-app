"""The `value` kind: something is on the page now, and the user waits for it
to change. A price, a rating, a countdown.

The default, and the one every other kind is a deviation from. Its anchor is
the reading itself, which makes verification unusually strong: the spec must
reproduce a value that was demonstrably there a second ago.
"""

from kinds.base import Kind

COMPILE_PROMPT = """You compile a deterministic extractor for a web page.

You are given fragments of real HTML surrounding a value, and the literal
value itself. Write a spec that will read that value again on every future
check, with no language model involved. It must keep working for months.

Respond with ONLY a JSON object:
{
  "scope": string | null,     // CSS selector narrowing to the region. STRONGLY PREFERRED.
  "kind": "css" | "regex" | "jsonpath" | "count",
  "selector": string,         // for css and count
  "pattern": string,          // for regex, at most ONE capture group
  "path": string,             // for jsonpath, e.g. "$.offers.price"
  "attribute": string | null, // read an attribute instead of the text
  "parse": "currency" | "float" | "int" | "text" | "bool",
  "unavailable_if": {...} | null   // same shape, no scope; matches when there is legitimately no value
}

RULES THAT MATTER:

- `scope` is not decoration. It narrows the search AND acts as a liveness
  test: if the scope stops matching, the page was redesigned and the watch
  reports a fault instead of silently reporting nothing. Without it, "the
  value is missing" and "the page was rebuilt" are indistinguishable.
  A real page contains many products, hidden templates and localised string
  tables -- an unscoped "Out of stock" pattern matches some other item's
  status and reports a healthy watch as unavailable forever.
- PREFER STABLE SELECTORS. Semantic class names, ids, data- attributes,
  itemprop. AVOID build-generated hashed classes like `_2mg-ayeqtfvSlVBeUNudsd`
  or `pk-LoKoNmmPK4GBiC9DR8` -- they change on every deploy of the site.
- Use `currency` for money. It requires a symbol or a two-digit minor unit,
  which is what stops "Steam Deck 512 GB" being read as a price of 512.
  Use `float` only when a bare number really is the value.
- Use `count` when the watch is about something that does not exist yet --
  a vacancy, a restock, a new listing. Zero is a legitimate answer for it,
  where every other kind treats "nothing found" as a problem.
- `jsonpath` supports dotted keys and integer indices only. No wildcards,
  filters or recursive descent.
- A `script[type="application/ld+json"]` scope plus a jsonpath is often the
  most stable option on an HTML page. Look for it."""


class ValueKind(Kind):
    name = "value"
    compile_prompt = COMPILE_PROMPT

    def anchor(self, reading: dict, url: str) -> str:
        literal = (reading.get("literal") or "").strip()
        if not literal:
            raise ValueError(
                f"nothing to watch on {url}: {reading.get('note', '')}. If this "
                f"is something you are waiting to APPEAR rather than to change, "
                f"it needs to be planned as a presence watch."
            )
        return literal

    def describe_anchor(self, anchor: str) -> str:
        return f"The literal value on the page right now: {anchor!r}"

    def feedback(self, result, anchor: str) -> str:
        return (
            f"running it gave {result.status}"
            f"{': ' + result.error if result.error else ''}, "
            f"but the value {anchor!r} is definitely on the page"
        )
