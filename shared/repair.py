"""Tier 1: put the model back, once, only when the extractor is broken.

Phase 8b took the language model out of the hot path. This puts it back in the
*repair* path, which is the whole bargain that makes a compiled extractor safe
to leave running for months: a selector will eventually stop matching, because
sites get redesigned, and something has to notice and fix it.

## What earns a repair

Only `failed`. Never `unavailable`.

That distinction is the reason `shared/extract.py` has three outcomes instead
of two, and this module is where the cost of getting it wrong lands. An
`unavailable` result means the page is fine and there is legitimately no value
today -- an out-of-stock item, a vacancy nobody has posted. Repairing that
would pay a model to "fix" an extractor that works, on every tick, forever.

## Why Haiku and not Sonnet

The Planner uses Sonnet because it is choosing a strategy from a blank page.
A repair is a much smaller question: here is the markup, here is the spec that
used to work, here is how it broke. Haiku costs a fifth as much and this runs
unattended, against a budget shared with the checks themselves.

## The repair is verified before it is trusted

A repaired spec is run against the same page it was derived from, exactly as
at plan time. A spec that does not reproduce a value is not stored -- otherwise
a broken extractor would be replaced by a differently broken one and the watch
would keep reporting a fault it cannot explain.
"""

import json

from anthropic import Anthropic

from extract import SpecError, extract, validate_spec

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1024

# Enough page for the value to appear without paying to send a whole megabyte
# of markup. Repairs are rare; this is still the largest single cost in one.
MAX_REPAIR_CHARS = 24000

SYSTEM_PROMPT = """You repair a broken extraction spec for a web page.

A spec that used to read a value has stopped working -- most often because the
site was redesigned and a class name or an id changed. You are given the old
spec, the error it produced, what the value means in plain English, and the
page as it looks now. Write a spec that works again.

Respond with ONLY a JSON object:
{
  "scope": string | null,     // CSS selector narrowing to the region
  "kind": "css" | "regex" | "jsonpath" | "count",
  "selector": string,         // for css and count
  "pattern": string,          // for regex, at most ONE capture group
  "path": string,             // for jsonpath, e.g. "$.offers.price"
  "attribute": string | null, // read an attribute instead of the text
  "parse": "currency" | "float" | "int" | "text" | "bool",
  "unavailable_if": {...} | null
}

RULES THAT MATTER:

- Keep the SAME `kind` and `parse` as the old spec unless they are the reason
  it broke. You are fixing a selector, not redesigning the watch. Changing
  `parse` from `currency` to `float` to make something match is how a product
  name gets read as a price.
- `scope` is the liveness anchor. Keep it if it still matches; replace it if
  it does not. Never drop it -- without one, a rebuilt page reads as "no value"
  forever instead of reporting a fault.
- PREFER STABLE SELECTORS. Semantic class names, ids, data- attributes,
  itemprop. AVOID build-generated hashed classes -- the site was redesigned
  once and will be again.
- If the value genuinely is not on the page any more, do NOT invent a selector
  that matches something else. Return the old spec unchanged; a watch that
  reports a fault is better than one that silently reads the wrong number."""


def _parse_json(raw: str) -> dict:
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"no JSON object in repair response: {raw[:200]!r}")
    return json.loads(raw[start:end + 1])


def _tidy(spec: dict) -> dict:
    keep = ("scope", "kind", "selector", "pattern", "path", "attribute",
            "parse", "unavailable_if")
    return {k: v for k, v in spec.items() if k in keep and v not in (None, "")}


def repair(old_spec: dict, error: str, extract_hint: str, payload: str,
           *, baseline=None, client=None) -> dict:
    """Re-derive a spec from the page. Returns the verified new spec, or raises.

    `baseline` is the value verified at plan time. A repair that produces a
    wildly different number has usually latched onto the wrong element -- a
    shipping cost, a review count -- which is the failure a missing selector
    does *not* look like, and the reason `plausible()` exists.
    """
    client = client or Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": (
            f"What the value means: {extract_hint}\n"
            f"The spec that stopped working: {json.dumps(old_spec)}\n"
            f"How it failed: {error}\n"
            f"The value read when the watch was created: {baseline!r}\n\n"
            f"The page as it is now:\n{payload[:MAX_REPAIR_CHARS]}"
        )}],
    )

    blocks = [block.text for block in response.content if block.type == "text"]
    if not blocks:
        raise ValueError(
            f"no text in repair response (blocks="
            f"{[b.type for b in response.content]}, stop_reason="
            f"{response.stop_reason!r})"
        )

    spec = _tidy(_parse_json(blocks[-1]))
    try:
        validate_spec(spec)
    except SpecError as exc:
        raise ValueError(f"repaired spec is malformed: {exc}") from exc

    result = extract(spec, payload)
    if not result.ok:
        raise ValueError(
            f"repaired spec still does not work: {result.status}"
            f"{': ' + result.error if result.error else ''}"
        )

    return {"extractor": spec, "value": result.value, "raw": result.raw}
