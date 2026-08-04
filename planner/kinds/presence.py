"""The `presence` kind: the thing being watched does not exist yet.

A job posting, a restock, an appointment slot. This is the largest class of
watch there is, and the first version of the Planner could not create one at
all -- it demanded a literal value on the page before compiling anything, so
`count` was unreachable in exactly the case it was added for.

Two inversions make it work, and both are easy to undo by accident:

- The anchor is a **neighbour**, not the thing wanted. Another listing of the
  same kind reveals the list's markup.
- **Zero is a passing verification.** Every other kind treats "found nothing"
  as failure; here it is the normal state of a watch that has not fired yet.
"""

import re

from extract import extract

from kinds.base import CompiledKind

COUNT_PROMPT = """You compile a deterministic counter for a list on a web page.

The user is waiting for something to APPEAR that is not there yet -- a job
posting, a restock, an open slot. So you are not shown the thing itself. You
are shown the markup around a DIFFERENT item of the same kind that happens to
be on the page today, because that is what reveals the list's structure.

Your job: write a spec that counts how many items in that list match what the
user is waiting for. Counting zero today is the correct and expected answer.

Respond with ONLY a JSON object:
{
  "scope": string,        // CSS selector for the LIST CONTAINER. Required.
  "kind": "count",
  "selector": string,     // CSS selector matching ONE item, filtered to what the user wants
  "parse": "int"
}

RULES THAT MATTER:

- `scope` must select the container that holds the list of items -- the results
  table, the `<ul>`, the grid. It is REQUIRED here, and it is doing real work:
  it is the liveness test. If the site is redesigned and the container stops
  matching, the watch reports a fault. Without it, a rebuilt page counts zero
  items forever and the user is told "not yet" until the end of time.
- `selector` matches an individual item AND filters it. Use soupsieve's
  `:-soup-contains("...")` to filter by visible text -- for example
  `a.job-title:-soup-contains("Cloud Engineer")`. Prefer `:-soup-contains(...)`
  over `:contains(...)`, which is a deprecated alias.
- Match the way the page is actually written. If the listings are in Hebrew,
  Japanese, or German, filter on the term as it appears on the page, not on
  the user's English phrasing.
- Filter on ONE distinguishing term rather than a whole phrase. A listing
  titled "Junior Cloud Engineer - Student Position" will not match a selector
  looking for the exact string "student job cloud engineer". Pick the term most
  likely to appear verbatim in a matching item's title.
- Do NOT try to encode every criterion the user mentioned. Being slightly loose
  is right: a watch that fires on a near-match wastes one email, while a watch
  too strict to ever fire wastes the whole point of the watch.
- PREFER STABLE SELECTORS. Semantic class names, ids, data- attributes. AVOID
  build-generated hashed classes -- they change on every deploy of the site."""

# `:-soup-contains("...")` and its deprecated alias `:contains("...")`.
_TEXT_FILTER = re.compile(r':(?:-soup-)?contains\((["\'])(?:(?!\1).)*\1\)')


def unfiltered(selector: str) -> str:
    """The same selector with its text filters removed."""
    return _TEXT_FILTER.sub("", selector).strip()


def unfiltered_count(spec: dict, raw: str):
    """How many items the list holds in total, ignoring the text filter.

    Two uses, one number. It is the proof that the item selector is real, and
    it is the only honest thing a plan card can say about a count that
    verified at zero: "47 jobs listed here today, 3 of which mention Cloud"
    lets a person judge whether the filter is sane. Without it they are shown
    a 0 and asked to trust it.
    """
    bare = unfiltered(spec.get("selector", ""))
    if not bare:
        return None
    probe = {**spec, "selector": bare}
    probe.pop("unavailable_if", None)
    result = extract(probe, raw)
    return result.value if result.ok else None


def prove_the_item_selector(spec: dict, raw: str) -> str | None:
    """Check that a count spec could ever match anything. Returns a complaint.

    A count verified at zero is much weaker evidence than a price verified at
    $789: it shows the container exists and the selector parses, but not that
    the selector would match the thing being waited for when it finally
    appears. A wrong item class counts zero today, counts zero forever, and
    never reports a fault -- the watch is alive, billed, and incapable of
    firing.

    So the filter is stripped and the bare item selector re-run. It must match
    something now: the page lists *other* jobs today, and if the selector
    cannot see those it will not see the one the user is waiting for either.
    Only the text filter is left unproven, which is the irreducible part --
    nobody can verify a match against a posting that does not exist yet.
    """
    selector = spec.get("selector", "")
    bare = unfiltered(selector)
    if not bare or bare == selector:
        return None  # nothing was filtered; the count itself is the proof

    probe = {**spec, "selector": bare}
    probe.pop("unavailable_if", None)
    result = extract(probe, raw)
    if result.ok and result.value:
        return None

    return (
        f"the item selector {bare!r} (your selector with its text filter "
        f"removed) matches nothing on this page, so the filtered version can "
        f"never match either. The page does list items today -- find the "
        f"selector that matches one of those, then filter it."
    )


class PresenceKind(CompiledKind):
    name = "presence"

    # A job search is a stream, not an event. Reporting the first posting and
    # then going silent would be the wrong shape for every request this kind
    # exists to serve. See `Kind.repeating`.
    repeating = True
    compile_prompt = COUNT_PROMPT

    def anchor(self, reading: dict, url: str) -> str:
        # Either works: if a matching item happens to be listed today, its
        # markup is just as good a guide to the list's shape as a neighbour's.
        literal = (reading.get("literal") or "").strip()
        sample = (reading.get("sample") or "").strip()
        anchor = literal or sample
        if not anchor:
            raise ValueError(
                f"{url} lists nothing that could be counted: "
                f"{reading.get('note', '')}"
            )
        return anchor

    def describe_anchor(self, anchor: str) -> str:
        return (
            f"An example item currently in the list: {anchor!r}\n"
            f"(This is NOT what the user wants -- it is a neighbour, shown to "
            f"reveal the list's structure.)"
        )

    def prove(self, spec: dict, raw: str) -> str | None:
        return prove_the_item_selector(spec, raw)

    def extras(self, spec: dict, raw: str, result) -> dict:
        total = unfiltered_count(spec, raw)
        return {"unfiltered_count": total} if total is not None else {}

    def feedback(self, result, anchor: str) -> str:
        # A count that ran and returned nothing is a working extractor
        # describing an empty list -- the normal state of a watch for something
        # that has not happened yet. So the complaint is about the scope.
        return (
            f"running it gave {result.status}"
            f"{': ' + result.error if result.error else ''}. The scope must "
            f"select the list container that holds items like {anchor!r}, "
            f"and it must match the page as it is written today."
        )
