"""Tests for the plain HTTP fetch.

This file exists because of one bug, and the bug is the kind that costs money
quietly rather than failing loudly.

`urllib` asks for `identity` encoding and does not decompress anything itself.
Some servers compress regardless -- python.org does, behind its CDN. Decoding
gzip bytes as UTF-8 **does not raise**; it produces line noise. So an entirely
ordinary HTML page arrived as garbage, the Planner reported "page content is
corrupted/unreadable binary data", and the escalation did the sensible thing
and rendered it in Chromium.

A browser check is 45x an HTTP one. Found on 2026-08-04, when a python.org job
watch was priced at $16.11/month and refused by the budget gate -- for two
targets that are both plain HTML and cost $0.07 between them once this worked.
"""

import gzip
import os
import sys
import zlib
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import blocked  # noqa: E402
import fetch  # noqa: E402

PAGE = "<html><body><h1>Junior Cloud Engineer</h1></body></html>"


@pytest.fixture
def wire(monkeypatch):
    """Stand in for the network, returning bytes and headers verbatim."""

    def serve(body: bytes, headers: dict | None = None):
        response = MagicMock()
        response.read.return_value = body
        response.headers = headers or {}
        response.__enter__ = lambda self: self
        response.__exit__ = lambda self, *a: False
        monkeypatch.setattr(fetch.urllib.request, "urlopen",
                            lambda *a, **k: response)

    return serve


def test_a_plain_body_is_unchanged(wire):
    wire(PAGE.encode())
    assert fetch.fetch_raw("https://example.com") == PAGE


def test_a_gzipped_body_is_decompressed(wire):
    """The bug. Without this the caller receives line noise that no selector
    can match and no error describes."""
    wire(gzip.compress(PAGE.encode()), {"Content-Encoding": "gzip"})
    assert fetch.fetch_raw("https://example.com") == PAGE


def test_gzip_is_detected_even_when_it_is_not_declared(wire):
    """A CDN that compresses without saying so is not hypothetical, and the
    magic number is unambiguous."""
    wire(gzip.compress(PAGE.encode()))
    assert fetch.fetch_raw("https://example.com") == PAGE


def test_a_deflated_body_is_decompressed(wire):
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    body = compressor.compress(PAGE.encode()) + compressor.flush()
    wire(body, {"Content-Encoding": "deflate"})
    assert fetch.fetch_raw("https://example.com") == PAGE


def test_a_body_that_lies_about_its_encoding_is_not_fatal(wire):
    """Better to hand back something a selector might match than to take
    planning down over a wrong header."""
    wire(PAGE.encode(), {"Content-Encoding": "gzip"})
    assert fetch.fetch_raw("https://example.com") == PAGE


def test_a_declared_charset_is_honoured(wire):
    """Hebrew job boards are the reason this is not academic."""
    hebrew = "<h1>מהנדס ענן</h1>"
    wire(hebrew.encode("utf-8"),
         {"Content-Type": "text/html; charset=utf-8"})
    assert fetch.fetch_raw("https://example.com") == hebrew


def test_a_windows_1255_page_is_decoded_with_its_own_charset(wire):
    hebrew = "<h1>עברית</h1>"
    wire(hebrew.encode("cp1255"),
         {"Content-Type": 'text/html; charset="windows-1255"'})
    assert fetch.fetch_raw("https://example.com") == hebrew


def test_a_missing_content_type_falls_back_to_utf8(wire):
    wire("<h1>café</h1>".encode())
    assert fetch.fetch_raw("https://example.com") == "<h1>café</h1>"


def test_a_malformed_content_type_does_not_raise(wire):
    wire(PAGE.encode(), {"Content-Type": "text/html; charset="})
    assert fetch.fetch_raw("https://example.com") == PAGE


def test_undecodable_bytes_are_replaced_rather_than_raising(wire):
    """A page that is genuinely not text must still come back as a string --
    the extractor will fail to match it, which is the right outcome, and a
    crash here would be recorded as an outage instead."""
    wire(b"\xff\xfe\x00garbage")
    assert isinstance(fetch.fetch_raw("https://example.com"), str)


def test_markup_is_capped(wire):
    wire(b"<p>x</p>" * 400000)
    assert len(fetch.fetch_raw("https://example.com")) == fetch.MAX_RAW_CHARS


# --- being refused, at the boundary where the reading happens -----------------

def test_a_403_is_reported_as_a_refusal_not_a_crash(monkeypatch):
    """403 and 429 arrive as exceptions rather than responses, and they are the
    commonest way a site says "not you"."""
    import urllib.error

    def deny(*a, **k):
        raise urllib.error.HTTPError("https://www.amazon.com/s", 403,
                                     "Forbidden", {}, None)

    monkeypatch.setattr(fetch.urllib.request, "urlopen", deny)

    with pytest.raises(blocked.Blocked) as caught:
        fetch.fetch_raw("https://www.amazon.com/s?k=xbox")

    assert caught.value.status == 403
    assert "amazon.com" in caught.value.reason


def test_a_404_still_raises_the_original_error(monkeypatch):
    """A wrong URL is a planning bug that will never clear by waiting, and
    dressing it up as a refusal would leave a watch retrying forever."""
    import urllib.error

    def missing(*a, **k):
        raise urllib.error.HTTPError("https://shop.example/gone", 404,
                                     "Not Found", {}, None)

    monkeypatch.setattr(fetch.urllib.request, "urlopen", missing)

    with pytest.raises(urllib.error.HTTPError):
        fetch.fetch_raw("https://shop.example/gone")


def test_a_captcha_served_with_a_200_is_still_a_refusal(wire):
    """Cloudflare and Amazon both do this, which is why the status alone is
    never enough."""
    wire(b"<html><h1>Enter the characters you see below</h1></html>")

    with pytest.raises(blocked.Blocked):
        fetch.fetch_raw("https://www.amazon.com/s?k=xbox")


def test_an_ordinary_page_is_still_returned_untouched(wire):
    wire(b"<html><body>a real page</body></html>")
    assert "a real page" in fetch.fetch_raw("https://shop.example/x")
