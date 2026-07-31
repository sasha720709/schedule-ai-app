"""Decide whether a condition is met, with no model.

Until now nothing in this codebase ever *evaluated* a condition. The Planner
wrote `{"metric": "price", "op": "<", "value": 450}`, the frontend printed it,
and Haiku did the actual comparing inside its own head on every tick. Tier 0
has to do it in Python, which means the loose shape that was fine as a display
string now has to become something executable.

## The rule that matters

**An op this module does not understand must never evaluate to "not met".**

That is the single dangerous failure here, and it is the same silent rot the
three-way extraction outcome exists to prevent. If Sonnet writes `"op": "under"`
on a Tuesday and this module quietly answers False, the watch never fires, no
error is ever recorded, and the owner concludes the price never dropped. So an
unrecognised op raises, the Checker records it as a failure, and 8d escalates
it like any other broken plan. A watch that cannot be evaluated has to say so.

Aliases are therefore generous -- models genuinely do write "below", "lt" and
"less_than" for the same idea -- but the fallback is a loud error, not a guess.
"""

import operator

OPS = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}

# Observed and plausible spellings. The Planner's prompt pins the vocabulary to
# the symbols, but the prompt is a request, not a guarantee, and the cost of
# tolerating a synonym is one dictionary entry.
ALIASES = {
    "lt": "<", "below": "<", "under": "<", "less_than": "<", "less": "<",
    "lte": "<=", "le": "<=", "at_most": "<=", "no_more_than": "<=",
    "gt": ">", "above": ">", "over": ">", "greater_than": ">", "greater": ">",
    "gte": ">=", "ge": ">=", "at_least": ">=", "no_less_than": ">=",
    "eq": "==", "equals": "==", "is": "==", "=": "==",
    "ne": "!=", "not_equals": "!=", "isnt": "!=", "<>": "!=",
}


class ConditionError(ValueError):
    """The condition cannot be evaluated -- a planning bug, not a page result."""


def normalise_op(op) -> str:
    if not isinstance(op, str):
        raise ConditionError(f"op must be a string, got {op!r}")
    cleaned = op.strip().lower().replace(" ", "_").replace("-", "_")
    resolved = ALIASES.get(cleaned, cleaned)
    if resolved not in OPS:
        raise ConditionError(
            f"unknown op {op!r} -- refusing to guess, because guessing 'not met' "
            f"is how a watch dies without anyone noticing"
        )
    return resolved


def _comparable(value, target):
    """Line the two sides up, or refuse.

    Numbers compare to numbers and text to text. The mixed case is the one that
    matters: a `text` extractor reading "629.00" against a numeric threshold is
    a spec that should have used `currency`, and coercing it here would hide
    that. But a bool against 0/1 is a real and intended pairing -- that is how
    `count` with `parse: bool` expresses "is there one yet".
    """
    if isinstance(value, bool) or isinstance(target, bool):
        return bool(value), bool(target)

    value_is_number = isinstance(value, (int, float))
    target_is_number = isinstance(target, (int, float))

    if value_is_number and target_is_number:
        return float(value), float(target)

    if not value_is_number and not target_is_number:
        return str(value).strip().lower(), str(target).strip().lower()

    raise ConditionError(
        f"cannot compare {type(value).__name__} {value!r} with "
        f"{type(target).__name__} {target!r} -- check the extractor's `parse`"
    )


def evaluate(condition, value) -> bool:
    """Is this condition true of this value right now?

    Only ever called with a real reading. An `unavailable` extraction means
    there is no value to judge, and the caller must treat that as "not yet"
    without coming here -- a watch for "price under $450" is not satisfied by
    an item having no price at all.
    """
    if not isinstance(condition, dict):
        raise ConditionError(f"condition must be an object, got {condition!r}")
    if value is None:
        raise ConditionError("no value to evaluate")

    if "op" not in condition or "value" not in condition:
        raise ConditionError(f"condition needs `op` and `value`: {condition!r}")

    op = normalise_op(condition["op"])
    left, right = _comparable(value, condition["value"])
    return bool(OPS[op](left, right))


def describe(condition) -> str:
    """A short human phrase for logs and notification emails."""
    if not isinstance(condition, dict):
        return str(condition)
    metric = condition.get("metric", "value")
    currency = condition.get("currency") or ""
    try:
        op = normalise_op(condition.get("op"))
    except ConditionError:
        op = str(condition.get("op"))
    return f"{metric} {op} {condition.get('value')}{(' ' + currency) if currency else ''}"
