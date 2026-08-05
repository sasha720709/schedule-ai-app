"""A calendar entry, as a file.

Named `ics.py`, not `calendar.py`. The Lambda zip is flat -- every shared
module sits beside `handler.py` -- so a module called `calendar` would shadow
the standard library's, which `email.utils` imports to build a Date header.
The same trap `docs/phase-9-watch-kinds.md` records for `kinds.py`, and it
would have surfaced as a broken email rather than an import error.

## Why a file and not an integration

The request was "put it in my calendar app". The obvious reading is Google
Calendar's API: OAuth, a consent screen, a refresh token to store and rotate,
a per-provider client, and the whole thing works for exactly one calendar.

An `.ics` attachment is the other reading, and for a **one-way reminder it is
not the lesser one** -- it is the actual answer. RFC 5545 is what every
calendar reads: Google, Apple, Outlook, Thunderbird, a phone. No OAuth, no
stored credential, no provider to be down. `docs/phase-9-watch-kinds.md` §6.

And it moves the reminding somewhere better. Once the entry is in a calendar,
**the calendar does the reminding** -- with the user's own alert settings, on
their own devices, offline. This app's job ends at delivering the entry.

## What is fiddly about the format, and it is all of it

Three things, each of which produces a file that some calendars open and
others silently reject:

- **UTC, not a named zone.** `DTSTART;TZID=Asia/Jerusalem:...` is legal only
  alongside a full `VTIMEZONE` block defining that zone's rules, which is
  dozens of lines and goes stale when a country changes its DST law. The
  moment is converted and written as `...Z` instead. The wall-clock intent is
  preserved because it was resolved in the right zone first -- see
  `planner/kinds/reminder.py`.
- **Escaping.** In an iCalendar TEXT value, `\\`, `;`, `,` and newlines are
  special. A reminder titled "Call Dr. Levi, ext. 4" is a plausible thing to
  write and an unescaped comma splits it into two values.
- **Folding at 75 octets.** Long lines must be wrapped with a CRLF and a
  leading space. Parsers that enforce it reject the whole file, not the line.

Lines end `\\r\\n`. The spec requires it and some parsers mean it.
"""

from datetime import datetime, timedelta, timezone

PRODID = "-//schedule-ai-app//reminder//EN"

# How long the entry occupies. A reminder is a moment rather than a meeting,
# but a zero-length event renders oddly in several calendars -- Google shows
# it as an all-day banner -- so it gets a short, unobtrusive block.
DEFAULT_MINUTES = 15

# RFC 5545 section 3.1: no line may exceed 75 octets, excluding the line break.
FOLD_AT = 75


def _escape(value) -> str:
    """Escape a TEXT value. Order matters: backslashes first, or they double."""
    text = "" if value is None else str(value)
    text = text.replace("\\", "\\\\")
    text = text.replace("\n", "\\n").replace("\r", "")
    text = text.replace(";", "\\;").replace(",", "\\,")
    return text


def _fold(line: str) -> str:
    """Wrap to 75 octets, continuing with a leading space.

    Counted in **octets, not characters**, because a title in Hebrew is three
    bytes per character and a limit counted in characters would produce lines
    three times too long -- which is precisely the kind of thing that works
    everywhere in testing and fails on one user's calendar.
    """
    raw = line.encode("utf-8")
    if len(raw) <= FOLD_AT:
        return line

    parts, start = [], 0
    while start < len(raw):
        end = min(start + (FOLD_AT if not parts else FOLD_AT - 1), len(raw))
        # Never split a multi-byte character: back off until the next byte is
        # not a continuation byte.
        while end < len(raw) and (raw[end] & 0xC0) == 0x80:
            end -= 1
        chunk = raw[start:end].decode("utf-8")
        parts.append(chunk if not parts else " " + chunk)
        start = end
    return "\r\n".join(parts)


def _stamp(moment: datetime) -> str:
    """A UTC timestamp in the form the format wants."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def event(*, uid: str, when, title: str, note: str = "",
          minutes: int = DEFAULT_MINUTES, now=None) -> str:
    """One `VEVENT`, wrapped in a `VCALENDAR`, ready to attach.

    `uid` is the watch id: stable, unique, and already meaningful. A calendar
    treats a repeated UID as *the same entry updated*, so re-sending a reminder
    corrects the existing one instead of creating a duplicate -- which is the
    behaviour you want from a retry and the reason not to generate a fresh one.
    """
    if isinstance(when, str):
        when = datetime.fromisoformat(when)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    ends = when + timedelta(minutes=max(1, int(minutes)))
    summary = _escape(title or "Reminder")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        # PUBLISH rather than REQUEST: this is an entry being handed over, not
        # an invitation expecting an RSVP. REQUEST makes calendars show
        # accept/decline buttons for a meeting with no other attendees.
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{_escape(uid)}@schedule-ai-app",
        f"DTSTAMP:{_stamp(now or datetime.now(timezone.utc))}",
        f"DTSTART:{_stamp(when)}",
        f"DTEND:{_stamp(ends)}",
        f"SUMMARY:{summary}",
    ]
    if note:
        lines.append(f"DESCRIPTION:{_escape(note)}")
    lines += [
        # An alarm at the moment itself. The user's calendar may well add its
        # own default on top; that is theirs to change, and the entry arriving
        # with no alert at all would be the one useless outcome.
        "BEGIN:VALARM",
        "TRIGGER:PT0S",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{summary}",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def filename(title: str) -> str:
    """A filename a person can recognise in an attachment list."""
    safe = "".join(c if c.isalnum() or c in " -_" else "" for c in title or "")
    safe = "-".join(safe.split())[:40].strip("-")
    return f"{safe or 'reminder'}.ics"
