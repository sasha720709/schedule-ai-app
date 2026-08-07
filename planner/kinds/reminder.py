"""The `reminder` kind: a watch whose trigger is the clock.

## Why this is the kind that proves the design

Every other kind answers *is it true yet*. This one has no page, no extractor,
no condition and no target -- the schedule going off **is** the event. Phase 9
was organised around three axes, and this is the only thing in the product that
exercises the first one: what makes a watch fire, condition or time.

The doc predicted `Kind` would need a `trigger` distinction here, the way
`quote` forced `CompiledKind` out of `Kind` in step 2, and said to be
suspicious if it did not. It did: `plan()` returns no targets at all, which
every caller downstream had assumed was impossible.

## What it does not do, and why that is the point

It does not remind you. **It sends you a calendar entry, and your calendar
reminds you** -- see `docs/phase-9-watch-kinds.md` §6. That is not a lesser
version of a real integration: for a one-way reminder an `.ics` attachment
*is* the real thing, it works with every calendar at once, and it needs no
OAuth, no per-provider client and no stored credential.

## The clock this reads is not the user's

There is no user profile -- `user_id` is still `"default"` and `NOTIFY_EMAIL`
is one address -- so "9am" has to mean 9am *somewhere* chosen in advance.
The zone comes from the request when the request names one, and otherwise from
`DEFAULT_TIMEZONE`, which is a deployment setting exactly like the notify
address. **The resolved local time and its zone are shown on the plan card**,
so a wrong assumption is visible in the one second before it matters rather
than at 6am. This is the auth-shaped gap the roadmap already names; it is not
a new one, and it will close when a user record exists.
"""

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import llm
import moment
from kinds.base import Kind

# Where "9am" is, absent anything better. A deployment setting, like
# NOTIFY_EMAIL -- and like NOTIFY_EMAIL it is a placeholder for a user record
# that does not exist yet.
DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "Asia/Jerusalem")

# A reminder further out than this is almost certainly a misread year. The
# limit is a sanity check rather than a policy: EventBridge will happily accept
# `at(2126-...)` and then hold a schedule for a century. Defined beside the
# guard that enforces it, and re-exported here because this module's readers
# expect to find it here.
MAX_YEARS_AHEAD = moment.MAX_YEARS_AHEAD

ONCE, DAILY, WEEKLY = "once", "daily", "weekly"
REPEATS = (ONCE, DAILY, WEEKLY)

# How long a repeating reminder runs before stopping itself, matching the term
# a repeating vacancy watch gets. A daily reminder is the second thing in this
# system that does not stop by itself, and a forgotten one would arrive every
# evening for years. The expiry email says plainly that nothing broke.
REPEATING_TERM_DAYS = 90

REMINDER_PROMPT = """You turn a request into a single reminder at a single
moment. There is nothing to watch and no page to read -- the time arriving is
the whole event.

WHEN. The local date and time, as `YYYY-MM-DDTHH:MM:SS`. No timezone offset
and no trailing Z: the zone is a separate field.

Resolve everything relative against the current time you are given.

  "remind me at 9am tomorrow"        -> tomorrow's date at 09:00:00
  "in two hours"                     -> now + 2 hours
  "next Monday morning"              -> next Monday at 09:00:00
  "on the 3rd at half past four"     -> that date at 16:30:00

If they give a date and no time, use 09:00:00. If they give a time and no
date, use the next occurrence of that time -- today if it has not passed yet,
otherwise tomorrow.

The time you are given is already local. Do not convert it, and do not apply
an offset to your answer -- write the wall-clock time a person would read off
a clock beside them.

TIMEZONE. An IANA name, and ONLY when the request actually names a place or a
zone: "9am New York time" -> "America/New_York". Null otherwise, which means
the local zone you were given. Do not guess from the language of the request
or from what the reminder is about.

TITLE. What goes on the calendar, in a few words, as an instruction to the
person: "Call the dentist", "Passport expires", "Pay the electricity bill".
No date in the title -- the calendar entry already has one.

NOTE. Any detail worth keeping that is not in the title: a phone number, an
address, a reference. Empty string if there is none.

REPEAT. Does this come back?

  "daily"   they said so: "every day", "each morning", "daily", "every
            evening at 9"
  "weekly"  "every Monday", "each Friday", "weekly"
  "once"    they said so: "tomorrow", "on the 3rd", "just once", or the
            request names a single dated event -- a flight, an expiry, an
            appointment
  null      **they did not say, and it could sensibly be either.** "Remind me
            at 9pm to learn English" is null: learning English is a habit and
            a single evening is also plausible. Do NOT guess. Null is the
            answer that gets the user asked.

Prefer null over a guess. Answering "once" for something they meant to repeat
is a reminder that silently never comes again.

Respond with ONLY a JSON object:
{"when": string, "timezone": string | null, "title": string, "note": string,
 "repeat": "once" | "daily" | "weekly" | null}"""


class ReminderKind(Kind):
    name = "reminder"

    # The axis this kind exists to prove. Everything else in the product is
    # "condition": something is read and judged. Here the schedule firing is
    # the event, so there is nothing to read, nothing to judge, and nothing
    # for 8d to repair.
    trigger = "time"

    # Nothing is compiled, so nothing can break.
    self_heals = False

    # A moment happens once. "Every morning at 9" is a different shape and is
    # deliberately not this one -- it never stops by itself, which is the
    # thing `expires_at` exists to bound.
    repeating = False

    def plan(self, request: str, symbol=None, *, hints=None, client=None) -> dict:
        # The clock the model is given must be the same clock its answer is
        # read in. Handing it UTC and then interpreting "in four minutes" as
        # local time puts the reminder hours in the past -- refused, for a
        # request that was perfectly reasonable. Found by trying to run one.
        here = ZoneInfo(DEFAULT_TIMEZONE)
        now = datetime.now(here)
        result = llm.ask(
            client,
            model=llm.READ_MODEL,
            max_tokens=llm.READ_MAX_TOKENS,
            system=REMINDER_PROMPT,
            # The model has no clock. "Tomorrow" is unanswerable without this,
            # and a model that guesses the date guesses the year too.
            content=(f"The current local time is {now:%Y-%m-%d %H:%M:%S} "
                     f"({DEFAULT_TIMEZONE}).\n\n{request}"),
        )

        zone_name = _zone(result.get("timezone"))
        when = _moment(result.get("when"), zone_name)
        title = " ".join(str(result.get("title") or "").split())[:200]
        repeat = _repeat(result.get("repeat"))

        return {
            # The two things every caller downstream assumed could not be
            # empty. That assumption is what step 3b went and fixed.
            "targets": [],
            "condition": {},
            "relative_change_pct": None,
            # Not an interval -- there is no polling. Kept only because the
            # cost model and the plan card both read it, and one check that
            # never repeats is the honest description.
            "check_interval_min": 1440,
            "fire_at": when.isoformat(),
            "fire_timezone": zone_name,
            "reminder_title": title or _fallback_title(request),
            "reminder_note": " ".join(str(result.get("note") or "").split())[:500],
            # "once" until told otherwise, and the question below is what does
            # the telling. Defaulting to a repeat would be the worse mistake:
            # a reminder nobody asked to repeat, arriving every evening
            # forever, is harder to undo than one that came only once.
            "repeat": repeat or ONCE,
            # Asked only when the request genuinely did not say. The owner's
            # own framing: "there should be, depending on the case, a question
            # -- daily, or one time?" This is that question, on the plan card,
            # answered before anything is scheduled.
            "questions": [] if repeat else [REPEAT_QUESTION],
        }


# The one fork a request can leave open, asked rather than guessed.
#
# It uses the same shape `questions.py` produces, because the plan card and
# `confirm` already speak it -- but note the difference: those questions narrow
# a list of things that were found, and this one chooses a setting. `items` is
# empty for exactly that reason, and `_confirm_reminder` reads the answer
# directly instead of intersecting ids.
REPEAT_QUESTION = {
    "id": "repeat",
    "question": "Once, or every day?",
    "options": [
        {"value": ONCE, "label": "Just this once", "items": []},
        {"value": DAILY, "label": "Every day at this time", "items": []},
        {"value": WEEKLY, "label": "Every week on this day", "items": []},
    ],
}


def _repeat(value):
    """What the model said about repeating, or None if it did not say.

    Anything unrecognised is treated as "did not say" and therefore asked
    about, rather than coerced to `once`. A model inventing "sometimes" should
    produce a question, not a silently one-shot reminder.
    """
    if isinstance(value, str) and value.strip().lower() in REPEATS:
        return value.strip().lower()
    return None


def _zone(name) -> str:
    """The zone named in the request, or the deployment's own.

    An unknown name falls back rather than raising: the model producing
    "Israel/Tel_Aviv" instead of "Asia/Jerusalem" should cost an hour's
    imprecision on a card the user is about to read, not a rejected request.
    """
    if isinstance(name, str) and name.strip():
        try:
            ZoneInfo(name.strip())
        except (ZoneInfoNotFoundError, ValueError):
            print(f"[reminder] unknown timezone {name!r}, "
                  f"using {DEFAULT_TIMEZONE}")
        else:
            return name.strip()
    return DEFAULT_TIMEZONE


# The validated moment moved to `shared/moment.py` when a reminder became
# editable: the api now applies the identical guard to a time the *user* typed,
# and one guard in two Lambdas is a guard that will eventually disagree with
# itself. Re-exported under the old private name so nothing here changed shape.
_moment = moment.parse


def _fallback_title(request: str) -> str:
    """A title is what the calendar entry is called, so it can never be empty."""
    return " ".join(request.split())[:80] or "Reminder"
