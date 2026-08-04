"""Tier 1 for a stream: judge what appeared, once, against what was asked for.

## The problem

A `jobs` watch fires when the board returns something new. The board matched on
one or two keywords, so "new" means *new and roughly relevant* -- and roughly
is where it stops. A request like "a student job for a cloud engineer in Beer
Sheva" carries three criteria; the search box takes one. The other two are
dropped, and the email reports every DevOps role in the district.

## Why this does not put the model back in the hot path

Phase 8b removed a Haiku call from every tick and that must stay removed. This
is not that. **Judging is paid per notification, not per check**, and the
difference is the whole design:

    one deterministic check                       $0.0000041
    one batched judgement of ten new items         $0.0031
    a jobs watch at 15 min, firing twice a day     $0.19/month
    the same watch judging on every tick          $16.42/month

A cheap counter decides *whether* anything happened; the model only ever sees
the handful of things that did. That is the same Tier 0 / Tier 1 split the
Checker already uses for repairs.

**One call for all the new items, not one per item.** Ten job cards are about
five hundred tokens. Judging them separately would cost ten times as much,
take ten times as long inside a Lambda that also has to finish its tick, and
produce scores that cannot be compared with each other because nothing saw
them together.

## Ranking, not filtering

Only items the model calls **irrelevant** are held back -- a barista job in a
cloud engineering search. Everything else is reported with a score and a
one-line reason, best first.

The asymmetry is deliberate and is the same argument `COUNT_PROMPT` makes about
loose selectors: a near-miss the user can dismiss costs them two seconds, and a
missed job is the thing the watch exists to prevent. A confident filter is
worse than a vague one.

## It must never block a notification

If the model is slow, malformed, over budget or simply down, the items go out
unjudged in their original order. Nothing about ranking is allowed to stand
between a person and a job that was found for them -- which is also why the
spend gate is checked before the call rather than after it.
"""

import cost
import llm

MAX_ITEMS = 20

# One line per item is all that is wanted, and a cap stops a model that decides
# to explain itself at length from costing a multiple of the estimate.
MAX_TOKENS = 900

SYSTEM_PROMPT = """You rank things that just appeared against what someone
asked to be told about.

You are given a request in plain English and a numbered list of items -- job
postings, usually, as they appear on a search results page: a title, a company,
a location, sometimes a date.

For each item give:

  "score"  0-10, how well it matches EVERYTHING the request asked for
  "why"    one short clause, under 12 words, naming the thing that decided it
  "keep"   false ONLY if the item is about something else entirely

SCORING. The board already matched on a keyword or two, so most items will be
plausible. Use the range:

  9-10  matches every stated criterion
  6-8   matches the role, misses or does not mention a secondary criterion
  3-5   related role, or clearly wrong on seniority or place
  0-2   a different job entirely

Judge only on what the item text actually says. **An unstated criterion is not
a failed criterion**: if the request asks for part-time and the posting does
not mention hours, that is a 7 with "hours not stated", not a 2. You are
reading a summary card, not the job description.

KEEP. `false` means "this is not the kind of thing they asked about at all" --
a barista role in a cloud engineering search. A junior role when they wanted
senior is still `true` with a low score; they can dismiss it in two seconds,
and a job wrongly withheld is the one mistake that matters here.

Respond with ONLY a JSON object:
{"items": [{"n": integer, "score": integer, "why": string, "keep": boolean}]}"""


def _prompt_for(request: str, items: list) -> str:
    lines = [f"{n}. {item.get('text', '')[:300]}"
             for n, item in enumerate(items, start=1)]
    return f"They asked:\n{request}\n\nWhat just appeared:\n" + "\n".join(lines)


def rank(request: str, items: list, *, client=None) -> tuple:
    """Score and order `items`. Returns `(items, spend_usd)`.

    Never raises. On any failure the items come back untouched, in their
    original order, with no scores -- an unranked email is a small
    disappointment and a missing one is a missed job.
    """
    if not items or not (request or "").strip():
        return items, 0.0

    batch = items[:MAX_ITEMS]
    try:
        reply = llm.ask(
            client,
            model=llm.READ_MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            content=_prompt_for(request, batch),
        )
        verdicts = {int(v["n"]): v for v in reply.get("items", [])
                    if isinstance(v, dict) and "n" in v}
    except Exception as exc:  # noqa: BLE001 -- ranking is never load-bearing
        print(f"[rank] failed, sending unranked: {type(exc).__name__}: {exc}")
        return items, 0.0

    spend = cost.rank_cost(len(batch))
    judged, unjudged = [], list(items[MAX_ITEMS:])

    for n, item in enumerate(batch, start=1):
        verdict = verdicts.get(n)
        if verdict is None:
            # The model skipped it. Report it rather than lose it.
            unjudged.append(item)
            continue
        if verdict.get("keep") is False:
            continue
        judged.append({
            **item,
            "score": _clamp(verdict.get("score")),
            "why": str(verdict.get("why") or "")[:120],
        })

    # Best first, and anything the model did not speak about goes last in the
    # order the board gave it -- never dropped.
    judged.sort(key=lambda i: i["score"], reverse=True)
    return judged + unjudged, spend


def _clamp(score) -> int:
    try:
        return max(0, min(10, int(score)))
    except (TypeError, ValueError):
        return 0
