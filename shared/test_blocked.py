"""Tests for recognising a refusal.

The two failure directions are not symmetric, and the tests are weighted the
same way the module is.

**A false negative costs money and noise**: an unrecognised block is N watches
paying Haiku to repair an extractor that was never broken, then degrading
separately with emails that say the wrong thing.

**A false positive costs a working watch.** So the bar for declaring a refusal
is deliberately high, and most of what is below is about not seeing one where
there is none -- the same lesson as `unavailable_if` matching a string table
and "FREE delivery" appearing in fourteen cards that had none.
"""

import pytest

import blocked

# Roughly the size of a real Amazon search result page.
BIG_PAGE = "<html><body>" + ("<div>a product card</div>" * 60000) + "</body></html>"


def test_a_real_page_is_not_a_refusal():
    assert blocked.reason(status=200, body=BIG_PAGE,
                          url="https://www.amazon.com/s?k=x") is None


def test_nothing_at_all_is_not_a_refusal():
    assert blocked.reason() is None
    assert blocked.reason(status=200, body="") is None
    assert blocked.reason(status=200, body=None) is None


# --- status codes ------------------------------------------------------------

@pytest.mark.parametrize("status", [403, 429, 503])
def test_the_statuses_that_mean_not_you(status):
    why = blocked.reason(status=status, url="https://www.amazon.com/s")
    assert why is not None
    assert str(status) in why and "amazon.com" in why


def test_a_404_is_a_wrong_url_not_a_refusal():
    """It is a planning bug, and it will never clear by waiting. Dressing it up
    as a refusal would leave a watch retrying a page that does not exist."""
    assert blocked.reason(status=404, url="https://shop.example/x") is None


def test_a_401_is_not_a_refusal_either():
    """It means the resource needs credentials -- also permanent, also not
    something patience fixes."""
    assert blocked.reason(status=401, url="https://shop.example/x") is None


def test_a_200_can_still_be_a_wall():
    """Cloudflare and Amazon both serve their bot checks with a normal status,
    which is why the status alone is never enough."""
    page = "<html><title>Attention Required! | Cloudflare</title></html>"
    assert blocked.reason(status=200, body=page, url="https://x.example") is not None


# --- strong markers stand alone ----------------------------------------------

@pytest.mark.parametrize("marker", [
    "Enter the characters you see below",
    "Type the characters you see in this image",
    "/errors/validateCaptcha",
    "Checking your browser before accessing",
    "Request unsuccessful. Incapsula incident ID: 123",
    "Pardon Our Interruption",
    "captcha-delivery.com",
])
def test_an_unambiguous_bot_check_is_a_refusal_at_any_size(marker):
    """Each of these belongs to one bot-defence product and appears nowhere
    else, so no corroboration is needed."""
    page = f"<html><body><h1>{marker}</h1>" + ("<p>x</p>" * 40000) + "</body></html>"
    assert blocked.reason(status=200, body=page, url="https://a.example") is not None


def test_the_reason_names_the_host_and_the_marker():
    why = blocked.reason(status=200, url="https://www.amazon.com/s?k=xbox",
                         body="<h1>Enter the characters you see below</h1>")
    assert "amazon.com" in why
    assert "enter the characters you see below" in why


# --- weak markers need corroboration -----------------------------------------

def test_a_short_page_saying_access_denied_is_a_refusal():
    page = "<html><body><h1>Access Denied</h1><p>You were blocked.</p></body></html>"
    assert blocked.reason(status=200, body=page, url="https://x.example") is not None


def test_the_same_words_deep_in_a_real_page_are_not():
    """"Access denied" is a plausible substring of a shop's help centre. A real
    Amazon search is about a million characters; a block page is a few
    thousand, which is the cheapest discriminator available."""
    page = BIG_PAGE.replace("a product card", "access denied help article", 1)
    assert blocked.reason(status=200, body=page, url="https://x.example") is None


def test_a_weak_marker_far_from_the_top_of_a_short_page_is_not_a_refusal():
    """A refusal page says so immediately. Anything thousands of characters in
    is a page that merely mentions the words."""
    page = "<html><body>" + ("<p>real content</p>" * 400) + "are you a robot</body>"
    assert len(page) < blocked.BLOCK_PAGE_MAX_CHARS
    assert blocked.reason(status=200, body=page, url="https://x.example") is None


def test_a_weak_marker_at_the_top_of_a_short_page_is():
    page = "<html><title>Are you a robot?</title><body>please verify</body></html>"
    assert blocked.reason(status=200, body=page, url="https://x.example") is not None


# --- the exception -----------------------------------------------------------

def test_check_says_nothing_about_a_real_page():
    blocked.check(status=200, body=BIG_PAGE, url="https://x.example")


def test_check_raises_with_the_reason_the_row_and_the_email_will_show():
    with pytest.raises(blocked.Blocked) as caught:
        blocked.check(status=429, url="https://www.bug.co.il/search")

    assert caught.value.status == 429
    assert "bug.co.il" in caught.value.reason
    assert caught.value.reason == str(caught.value)


# --- host, for grouping the metric -------------------------------------------

def test_the_host_is_what_a_person_would_call_the_shop():
    assert blocked.host_of("https://www.amazon.com/s?k=xbox") == "amazon.com"
    assert blocked.host_of("https://bug.co.il/search?q=x") == "bug.co.il"
    assert blocked.host_of("https://shop.example:8443/x") == "shop.example"


def test_a_host_can_always_be_produced():
    assert blocked.host_of(None) == "unknown"
    assert blocked.host_of("not a url") == "unknown"
