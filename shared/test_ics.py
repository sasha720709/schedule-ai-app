"""Tests for the calendar entry.

The format is the whole difficulty. Every assertion here is about a file that
some calendars would open and others would silently reject -- which is the
worst failure mode available, because it looks like it worked.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import ics

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


def lines(text):
    return text.split("\r\n")


def field(text, name):
    return next(line for line in lines(text) if line.startswith(name + ":"))


def entry(**kwargs):
    kwargs.setdefault("uid", "w_1")
    kwargs.setdefault("when", datetime(2026, 8, 6, 9, 0,
                                       tzinfo=ZoneInfo("Asia/Jerusalem")))
    kwargs.setdefault("title", "Call the dentist")
    kwargs.setdefault("now", NOW)
    return ics.event(**kwargs)


# --- shape -------------------------------------------------------------------

def test_it_is_a_calendar_containing_one_event():
    text = entry()
    assert lines(text)[0] == "BEGIN:VCALENDAR"
    assert "END:VCALENDAR" in lines(text)
    assert text.count("BEGIN:VEVENT") == 1


def test_every_line_ends_crlf():
    """The spec requires it and some parsers mean it."""
    text = entry()
    assert "\r\n" in text
    assert text.endswith("\r\n")
    assert "\n" not in text.replace("\r\n", "")


def test_it_publishes_rather_than_invites():
    """REQUEST makes calendars show accept/decline buttons for a meeting with
    no other attendees."""
    assert "METHOD:PUBLISH" in lines(entry())


def test_the_entry_carries_an_alarm():
    """An entry that arrives with no alert at all is the one useless outcome."""
    text = entry()
    assert "BEGIN:VALARM" in lines(text)
    assert "ACTION:DISPLAY" in lines(text)


# --- time --------------------------------------------------------------------

def test_the_moment_is_written_in_utc():
    """`DTSTART;TZID=...` is legal only alongside a full VTIMEZONE block
    defining that zone's rules -- dozens of lines that go stale when a country
    changes its DST law."""
    assert field(entry(), "DTSTART") == "DTSTART:20260806T060000Z"


def test_nine_in_jerusalem_is_not_nine_in_utc():
    """The wall-clock intent survives because it was resolved in the right
    zone first. Getting this backwards puts the reminder three hours out."""
    text = entry(when=datetime(2026, 8, 6, 9, 0,
                               tzinfo=ZoneInfo("Asia/Jerusalem")))
    assert "T060000Z" in field(text, "DTSTART")


def test_the_string_form_the_row_stores_is_accepted():
    text = entry(when="2026-08-06T09:00:00+03:00")
    assert field(text, "DTSTART") == "DTSTART:20260806T060000Z"


def test_a_naive_time_is_treated_as_utc_rather_than_rejected():
    text = entry(when=datetime(2026, 8, 6, 9, 0))
    assert field(text, "DTSTART") == "DTSTART:20260806T090000Z"


def test_the_entry_occupies_a_short_block_not_zero_time():
    """A zero-length event renders as an all-day banner in several calendars."""
    text = entry()
    assert field(text, "DTEND") == "DTEND:20260806T061500Z"


def test_the_length_can_be_chosen():
    assert field(entry(minutes=60), "DTEND") == "DTEND:20260806T070000Z"


# --- escaping ----------------------------------------------------------------

def test_a_comma_in_the_title_does_not_split_it():
    """"Call Dr. Levi, ext. 4" is a plausible thing to write, and an unescaped
    comma turns one value into two."""
    text = entry(title="Call Dr. Levi, ext. 4")
    assert field(text, "SUMMARY") == r"SUMMARY:Call Dr. Levi\, ext. 4"


def test_semicolons_and_backslashes_are_escaped():
    text = entry(title=r"a;b\c")
    assert field(text, "SUMMARY") == r"SUMMARY:a\;b\\c"


def test_backslashes_are_escaped_before_anything_else():
    """Order matters: escaping commas first would then double their new
    backslashes."""
    assert r"SUMMARY:x\\\,y" == field(entry(title="x\\,y"), "SUMMARY")


def test_a_newline_in_a_note_becomes_the_escape_not_a_new_field():
    """A raw newline ends the property, so the rest of the note would be read
    as a malformed line and the whole file rejected."""
    text = entry(note="line one\nline two")
    assert r"DESCRIPTION:line one\nline two" in lines(text)


def test_an_absent_note_writes_no_description():
    assert not any(line.startswith("DESCRIPTION:line")
                   for line in lines(entry()))


# --- folding -----------------------------------------------------------------

def test_a_long_line_is_folded():
    text = entry(title="x" * 200)
    assert all(len(line.encode()) <= 75 for line in lines(text))


def test_a_folded_line_continues_with_a_space():
    text = entry(title="x" * 200)
    folded = [line for line in lines(text) if line.startswith(" ")]
    assert folded


def test_folding_counts_octets_not_characters():
    """A Hebrew title is three bytes per character, so a limit counted in
    characters produces lines three times too long -- which works everywhere
    in testing and fails on one person's calendar."""
    text = entry(title="לזכור להתקשר לרופא השיניים בבוקר " * 4)
    assert all(len(line.encode()) <= 75 for line in lines(text))


def test_folding_never_splits_a_character():
    text = entry(title="לזכור להתקשר לרופא השיניים בבוקר " * 4)
    for line in lines(text):
        line.encode("utf-8").decode("utf-8")  # raises if a char was cut


# --- identity ----------------------------------------------------------------

def test_the_uid_is_the_watch_so_a_resend_updates_rather_than_duplicates():
    """A calendar treats a repeated UID as the same entry updated. That is the
    behaviour you want from a retry."""
    assert field(entry(uid="w_abc"), "UID") == "UID:w_abc@schedule-ai-app"


def test_two_entries_for_one_watch_are_the_same_entry():
    first = entry(uid="w_abc", title="one")
    second = entry(uid="w_abc", title="two")
    assert field(first, "UID") == field(second, "UID")


# --- filename ----------------------------------------------------------------

def test_the_filename_is_recognisable_in_an_attachment_list():
    assert ics.filename("Call the dentist") == "Call-the-dentist.ics"


def test_a_filename_never_carries_path_characters():
    assert "/" not in ics.filename("a/b/../c")
    assert ".." not in ics.filename("a/b/../c")


def test_an_empty_title_still_produces_a_filename():
    assert ics.filename("") == "reminder.ics"
    assert ics.filename(None) == "reminder.ics"


# --- a repeating entry --------------------------------------------------------

def test_a_daily_reminder_repeats_in_the_calendar():
    """One entry that repeats, rather than an entry a day: with a stable UID
    the daily email keeps correcting the same series instead of littering the
    calendar with ninety copies."""
    assert "RRULE:FREQ=DAILY" in lines(entry(repeat="daily"))


def test_a_weekly_reminder_says_weekly():
    assert "RRULE:FREQ=WEEKLY" in lines(entry(repeat="weekly"))


def test_a_one_shot_carries_no_rule():
    assert not any(line.startswith("RRULE") for line in lines(entry()))
    assert not any(line.startswith("RRULE")
                   for line in lines(entry(repeat="once")))
