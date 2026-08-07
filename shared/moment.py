"""One definition of "a moment a schedule can actually be set for".

This lived inside `planner/kinds/reminder.py` until a reminder became
editable. Two callers now need the identical guard -- the Planner, deciding
whether a moment the model produced is usable, and the api, deciding whether a
moment the *user typed* is usable -- and a guard duplicated across two Lambdas
is a guard that will disagree with itself. It is the same argument that put
`shared/condition.py` and `shared/cost.py` here.

The refusals are sentences rather than codes on purpose. A schedule in the
past is rejected by EventBridge at create time, which surfaces as a 500 from
whichever endpoint asked for it, long after there was anything the user could
do about it -- and **a guardrail that returns 500 is an outage**, which is the
lesson from 2026-08-04 that this file exists to stop repeating.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# A reminder two years out is a misread year far more often than it is a real
# request. Refusing says so while there is still something to correct.
MAX_YEARS_AHEAD = 2


def parse(raw, zone_name: str) -> datetime:
    """The local wall-clock time to fire at, validated.

    Returned **aware but unconverted**: the reading stays 09:00 and the zone
    travels beside it, which is the shape `at(...)` needs. Converting to UTC
    here would make "9am" mean 9am only until the clocks changed.

    Raises `ValueError` with a sentence a person can act on.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("could not work out when to remind you")

    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ValueError(f"could not read {raw!r} as a date and time") from exc

    zone = ZoneInfo(zone_name)
    local = parsed.replace(tzinfo=zone) if parsed.tzinfo is None \
        else parsed.astimezone(zone)

    now = datetime.now(zone)
    if local <= now:
        raise ValueError(
            f"{local:%Y-%m-%d %H:%M} ({zone_name}) has already passed — "
            f"say a time in the future"
        )
    if local > now + timedelta(days=365 * MAX_YEARS_AHEAD):
        raise ValueError(
            f"{local:%Y-%m-%d %H:%M} is more than {MAX_YEARS_AHEAD} years "
            f"away — check the year"
        )
    return local


def zone_or(name, fallback: str) -> str:
    """A timezone name the runtime actually has, or the fallback.

    A zone the model invented ("Israel/Tel_Aviv") should cost an hour's
    imprecision on a card the user is about to read, not a rejected request.
    """
    if isinstance(name, str) and name.strip():
        try:
            ZoneInfo(name.strip())
        except (ZoneInfoNotFoundError, ValueError):
            return fallback
        return name.strip()
    return fallback
