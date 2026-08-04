"""The `jobs` kind: a vacancy search, from boards that are known to answer.

## Why this is a kind and not a better prompt

`presence` could not plan the request it was built for. Asked "tell me when a
student cloud engineer vacancy appears in Beer Sheva", the searching Planner
chose a cookie-walled company careers page and LinkedIn's ordinary search
page, and failed on both. The machinery was fine -- the *choice of where to
look* was the failure.

That is the same failure `quote` was split out to fix, and it gets the same
answer: a registry. `shared/job_boards.py` owns the URL and the extractor;
the model's entire contribution is turning "student cloud engineer in Beer
Sheva" into keywords, a location and a country.

So this kind, like `quote`, extends `Kind` directly rather than `CompiledKind`.
There is nothing to anchor and nothing to compile.

## What that buys, beyond working at all

**The text filter disappears.** A `presence` watch had to encode the user's
request as a CSS `:-soup-contains(...)`, which is the one thing about that
kind that can never be verified -- nobody can check a filter against a posting
that does not exist yet. Here the *board* does the filtering, server-side, and
the extractor simply counts what came back. The unverifiable step is gone
rather than mitigated.

**It is cheaper.** No web search, no Sonnet compile, no Chromium: one small
Haiku call for the search terms, then a plain GET per board. About
$0.012/month at fifteen-minute intervals.

**It repeats.** A job search is a stream, so `repeating = True` -- each posting
is reported once and the watch keeps looking. That matters more here than
anywhere: LinkedIn's guest endpoint alternates between two result sets, so
without deduplication a jobs watch would email every other tick forever.
"""

import job_boards
from extract import extract
from fetch import fetch_raw

import llm
from kinds.base import Kind

JOBS_PROMPT = """You plan a watch on job vacancies. The job boards are already
decided -- you are NOT choosing where to look and you have no web search.

Turn the request into a search the boards can run, and say how often to check.

KEYWORDS. What goes in a job-search box: the role, in the fewest words that
still identify it. "cloud engineer", "python developer", "barista".

  "a student job for a cloud engineer in Beer Sheva" -> "cloud engineer"
  "any part-time work near me in Haifa"              -> "part time"
  "junior React roles"                               -> "react"

Do NOT put the location in the keywords -- it has its own field. Do NOT stack
qualifiers a job title would not contain: "student cloud engineer Beer Sheva
part time" matches nothing, and a search that can never match is worse than
one that is slightly loose. Prefer TWO words to five.

LOCATION. As a board would recognise it, with the country: "Beer Sheva,
Israel", "New York, United States", "Berlin, Germany". If the request names
no place, use the country alone, or "" if there is no clue at all.

COUNTRY. The ISO two-letter code for that location: IL, US, DE, GB. Null if
you cannot tell.

INTERVAL. Minutes. Job postings appear during business hours and are not
urgent to the minute; 15-60 is sensible. Use 15 if the request sounds urgent.

Respond with ONLY a JSON object:
{
  "keywords": string,
  "location": string,
  "country": string | null,
  "check_interval_min": integer
}"""


class JobsKind(Kind):
    name = "jobs"

    # The extractor comes from the registry, so there is nothing for 8d to
    # repair: if a board reshapes its markup, the fix is one line in
    # `job_boards.py` for every watch at once. Same argument as `quote`.
    self_heals = False

    # A job search is a stream you follow for weeks, not an event. See
    # `Kind.repeating` -- and note that LinkedIn's alternating result sets make
    # deduplication load-bearing here rather than merely polite.
    repeating = True

    def plan(self, request: str, symbol=None, *, hints=None, client=None) -> dict:
        """Search terms and cadence. No web search: the boards are decided."""
        result = llm.ask(
            client,
            model=llm.READ_MODEL,
            max_tokens=llm.READ_MAX_TOKENS,
            system=JOBS_PROMPT,
            content=request,
        )
        keywords = (result.get("keywords") or "").strip()
        if not keywords:
            raise ValueError(
                f"could not work out what job to search for in {request!r}"
            )

        interval = int(result.get("check_interval_min") or 30)
        return {
            # Any new posting is the whole point; there is no threshold to
            # cross. The counter is what the Checker compares, and the
            # per-item deduplication is what stops it repeating itself.
            "condition": {"metric": "count", "op": ">", "value": 0,
                          "currency": None},
            "relative_change_pct": None,
            "check_interval_min": max(1, min(interval, 1440)),
            "targets": job_boards.targets_for(
                keywords,
                result.get("location") or "",
                result.get("country"),
            ),
        }

    def resolve(self, target: dict, condition: dict, *,
                fetch_http=None, fetch_browser=None, client=None) -> dict:
        """Prove the board answers, before the plan is offered.

        `fetch_http` and `fetch_browser` are accepted and ignored: the URL is
        not known until `plan` has expanded it, so the caller cannot have bound
        a fetcher to it, and there is no page to render.

        A board returning **nothing** is refused rather than stored. Zero is a
        legitimate reading for a `presence` watch -- the job has not been
        posted yet -- but here it means something else: the board has been
        reshaped, or the query is malformed, and a watch built on it would
        report "not yet" until the end of time. This is the liveness test that
        a canned counter would otherwise lack.
        """
        url = target["url"]
        outcome = extract(target["extractor"], fetch_raw(url))
        if not outcome.ok:
            raise ValueError(
                f"{target.get('board', 'board')} could not be read: "
                f"{outcome.error or outcome.status}"
            )
        if not outcome.value:
            raise ValueError(
                f"{target.get('board', 'board')} returned no listings at all "
                f"for this search, so a watch on it could never fire. Try "
                f"broader search terms."
            )

        return {
            "url": url,
            "extract_hint": target["extract_hint"],
            "fetch_method": target["fetch_method"],
            "window": self.window,
            "extractor": target["extractor"],
            "verified_value": outcome.value,
            "verified_raw": outcome.raw,
            "verified_items": outcome.items,
            # The board already filtered, so every result counts as a match.
            # Saying so makes the plan card read the same as a presence watch's.
            "unfiltered_count": outcome.value,
            "literal": outcome.raw,
            "why": (f"{target.get('board', 'known board')}; "
                    f"{outcome.value} listings right now, nothing compiled"),
        }
