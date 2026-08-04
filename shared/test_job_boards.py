"""Tests for the job-board registry.

This file exists because the searching Planner could not plan the request the
`presence` kind was built for. Asked "tell me when a student cloud engineer
vacancy appears in Beer Sheva", it chose a cookie-walled careers page and
LinkedIn's ordinary search page, and failed on both.

The fix is the same one `sources.py` applies to stock quotes: stop asking a
question whose answer never changes. What is protected here is that the
registry keeps producing the same, verified, plain-HTTP targets -- and that an
unknown country narrows the search rather than refusing it.
"""

import pytest

import job_boards


def test_a_search_always_produces_at_least_one_board():
    targets = job_boards.targets_for("cloud engineer", "Beer Sheva, Israel", "IL")
    assert targets
    assert all(t["url"].startswith("https://") for t in targets)


def test_linkedin_answers_for_every_country():
    """The whole reason it is worth the trouble: one board covers Israel and
    the US, which is what the owner asked to be certain of."""
    for country in ("IL", "US", "DE", None, "", "ZZ"):
        names = {t["board"] for t in job_boards.targets_for("developer", "", country)}
        assert "linkedin" in names


def test_israel_gets_a_hebrew_board_as_well():
    names = {t["board"] for t in job_boards.targets_for("cloud", "Tel Aviv", "IL")}
    assert names == {"linkedin", "drushim"}


def test_an_unknown_country_narrows_rather_than_refuses():
    """Refusing to plan would be far worse than planning with one board."""
    names = {t["board"] for t in job_boards.targets_for("cloud", "Mars", "ZZ")}
    assert names == {"linkedin"}


def test_the_country_is_case_and_space_insensitive():
    assert len(job_boards.targets_for("cloud", "", " il ")) == 2


def test_a_search_with_nothing_to_search_for_is_refused():
    """A blank keyword produces a board's unfiltered front page, which would
    fire on every job in the country."""
    for blank in ("", "   ", None):
        with pytest.raises(ValueError, match="needs something"):
            job_boards.targets_for(blank, "Israel", "IL")


# --------------------------------------------------------------------------
# The canned targets themselves
# --------------------------------------------------------------------------

def linkedin_for(keywords, location, country="IL"):
    return next(t for t in job_boards.targets_for(keywords, location, country)
                if t["board"] == "linkedin")


def test_the_linkedin_target_is_the_keyless_guest_endpoint():
    """No account, no key, no OAuth, no Chromium -- which is what keeps a jobs
    check at the price of any other HTTP check."""
    target = linkedin_for("cloud engineer", "Beer Sheva, Israel")

    assert "jobs-guest" in target["url"]
    assert target["fetch_method"] == "http"


def test_keywords_and_location_are_url_encoded():
    target = linkedin_for("cloud engineer", "Beer Sheva, Israel")

    assert "cloud+engineer" in target["url"] or "cloud%20engineer" in target["url"]
    assert "Beer" in target["url"]
    assert " " not in target["url"]


def test_the_extractor_is_canned_and_counts():
    """Nothing is compiled, so nothing can be compiled wrongly. This is the
    step that removed the presence kind's unverifiable text filter."""
    target = linkedin_for("cloud engineer", "Israel")

    assert target["extractor"]["kind"] == "count"
    assert target["extractor"]["parse"] == "int"
    # data-entity-urn lives on this element, which is what makes deduplication
    # survive LinkedIn rewriting every link on every request.
    assert "job-search-card" in target["extractor"]["selector"]


def test_the_hint_says_where_the_listings_come_from():
    """It is the repair instruction and the plan card's explanation, not a
    reading instruction -- nothing reads it on a tick."""
    target = linkedin_for("cloud engineer", "Beer Sheva, Israel")

    assert "LinkedIn" in target["extract_hint"]
    assert "cloud engineer" in target["extract_hint"]


def test_a_quote_in_the_keywords_cannot_break_out_of_the_url():
    """The keywords come from a model and are spliced into a URL."""
    target = linkedin_for('cloud" onerror=x', "Israel")

    assert '"' not in target["url"]
    assert " " not in target["url"]


def test_every_board_is_plain_http():
    """A browser check is 45x. If a board ever needs rendering it does not
    belong in this registry -- it belongs on the searching path."""
    for target in job_boards.targets_for("developer", "Tel Aviv", "IL"):
        assert target["fetch_method"] == "http"


def test_every_board_declares_a_country_it_serves():
    for board in job_boards.BOARDS.values():
        assert board.countries
