"""Tests for the known-source registry.

The property that matters: a market-quote request must produce the same
target every time, with nothing searched and nothing compiled. Watching the
registry's shape is watching that promise.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sources
from extract import extract, validate_spec

CNBC_FIXTURE = json.dumps({
    "FormattedQuoteResult": {"FormattedQuote": [{
        "symbol": "AAPL", "last": "333.43", "currencyCode": "USD",
        "curmktstatus": "PRE_MKT",
        "ExtendedMktQuote": {"last": "307.29"},
    }]}
})


def test_a_symbol_expands_to_a_complete_http_target():
    t = sources.expand("stock_quote", "AAPL")
    assert t["fetch_method"] == "http"
    assert "AAPL" in t["url"]
    validate_spec(t["extractor"])  # canned specs get no exemption from the gate


def test_the_canned_extractor_reads_the_real_payload_shape():
    t = sources.expand("stock_quote", "AAPL")
    r = extract(t["extractor"], CNBC_FIXTURE)
    assert r.status == "ok"
    assert r.value == 333.43


def test_the_same_request_always_lands_on_the_same_source():
    """The whole point, stated as a test."""
    assert sources.expand("stock_quote", "AAPL") == sources.expand("stock_quote", "AAPL")


def test_symbols_are_normalised_and_odd_instruments_survive():
    assert "AAPL" in sources.expand("stock_quote", " aapl ")["url"]
    for odd in ("@CL.1", ".DJI", "BRK.A", "EUR="):
        sources.expand("stock_quote", odd)  # must not raise


def test_a_symbol_is_a_hard_gate_because_it_enters_a_url():
    for bad in ("", "AAPL&x=1", "a b", "х0", "A" * 13, "../etc"):
        with pytest.raises(ValueError):
            sources.expand("stock_quote", bad)


def test_an_unknown_kind_is_rejected_loudly():
    with pytest.raises(ValueError, match="unknown known_source"):
        sources.expand("weather", "AAPL")
