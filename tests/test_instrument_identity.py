"""Tests for deterministic instrument-identity resolution (#814) and the
context-anchored message placeholder (#888)."""

import unittest
from unittest.mock import patch

import pytest

from tradingagents.agents.context import (
    _identity,
    build_instrument_context,
    get_instrument_context_from_state,
    resolve_instrument_identity,
)

LISTING = "tradingagents.dataflows.symbols.listing_entry"

FPT_ROW = {
    "symbol": "FPT",
    "board": "HSX",
    "organName": "Công ty Cổ phần FPT",
    "organShortName": "FPT Corp",
}


@pytest.mark.unit
class ResolveInstrumentIdentityTests(unittest.TestCase):
    def setUp(self):
        _identity.cache_clear()

    def tearDown(self):
        _identity.cache_clear()

    def test_resolves_company_metadata_from_the_listing(self):
        with patch(LISTING, return_value=FPT_ROW) as mock:
            identity = resolve_instrument_identity("fpt")
        mock.assert_called_once_with("FPT")
        self.assertEqual(identity["company_name"], "Công ty Cổ phần FPT")
        self.assertEqual(identity["exchange"], "HOSE")  # HSX board -> HOSE
        self.assertEqual(identity["sector"], "Công nghệ")

    def test_board_is_normalised_to_the_exchange_name(self):
        for board, exchange in (("HSX", "HOSE"), ("HNX", "HNX"), ("UPCOM", "UPCOM")):
            _identity.cache_clear()
            with patch(LISTING, return_value={**FPT_ROW, "board": board}):
                self.assertEqual(resolve_instrument_identity("FPT")["exchange"], exchange)

    def test_falls_back_to_short_name(self):
        with patch(LISTING, return_value={"symbol": "FPT", "board": "HSX", "organShortName": "FPT Corp"}):
            identity = resolve_instrument_identity("FPT")
        self.assertEqual(identity["company_name"], "FPT Corp")

    def test_skips_placeholder_values(self):
        # An unmapped code has sector "Khác", which is left out like a placeholder.
        with patch(LISTING, return_value={"symbol": "ZZZ", "board": "n/a", "organName": "  ",
                                          "organShortName": "None"}):
            identity = resolve_instrument_identity("ZZZ")
        self.assertEqual(identity, {})

    def test_an_index_is_named_without_a_listing_lookup(self):
        with patch(LISTING) as mock:
            identity = resolve_instrument_identity("VNINDEX")
        mock.assert_not_called()
        self.assertEqual(identity, {"company_name": "chỉ số VNINDEX", "quote_type": "INDEX"})

    def test_an_unknown_code_resolves_to_no_identity(self):
        with patch(LISTING, return_value=None):
            self.assertEqual(resolve_instrument_identity("ZZZ"), {})

    def test_fails_open_on_exception(self):
        with patch(LISTING, side_effect=RuntimeError("rate limited")):
            self.assertEqual(resolve_instrument_identity("FPT"), {})

    def test_a_failed_lookup_is_asked_again(self):
        # A long process (a backtest) must not run every later cell without an
        # identity because one request failed.
        with patch(LISTING, side_effect=[TimeoutError(), FPT_ROW]) as mock:
            self.assertEqual(resolve_instrument_identity("FPT"), {})
            self.assertEqual(resolve_instrument_identity("FPT")["company_name"], "Công ty Cổ phần FPT")
        self.assertEqual(mock.call_count, 2)

    def test_an_unknown_code_is_asked_again(self):
        with patch(LISTING, side_effect=[None, FPT_ROW]):
            self.assertEqual(resolve_instrument_identity("FPT"), {})
            self.assertEqual(resolve_instrument_identity("FPT")["exchange"], "HOSE")

    def test_result_is_cached(self):
        with patch(LISTING, return_value=FPT_ROW) as mock:
            first = resolve_instrument_identity("FPT")
            second = resolve_instrument_identity("FPT")
        mock.assert_called_once()  # second call served from cache
        self.assertEqual(first, second)


@pytest.mark.unit
class BuildInstrumentContextTests(unittest.TestCase):
    def test_mentions_exact_symbol_without_identity(self):
        context = build_instrument_context("FPT")
        self.assertIn("`FPT`", context)
        self.assertIn("Vietnamese stock", context)
        self.assertIn("VND", context)
        self.assertNotIn("Resolved identity", context)

    def test_injects_resolved_identity(self):
        context = build_instrument_context(
            "FPT", "stock",
            {"company_name": "Công ty Cổ phần FPT", "sector": "Công nghệ", "exchange": "HOSE"},
        )
        self.assertIn("Company: Công ty Cổ phần FPT", context)
        self.assertIn("Sector: Công nghệ", context)
        self.assertIn("Exchange: HOSE", context)
        self.assertIn("Do not substitute a different company", context)

    def test_a_historical_run_gets_only_the_current_name_with_a_caveat(self):
        context = build_instrument_context(
            "FPT", "stock",
            {"company_name": "Công ty Cổ phần FPT", "sector": "Công nghệ", "exchange": "HOSE"},
            trade_date="2020-01-02",
        )
        self.assertIn("Company: Công ty Cổ phần FPT (its current name", context)
        self.assertIn("on 2020-01-02 it may have been named differently", context)
        self.assertNotIn("Sector:", context)
        self.assertIn("Exchange: HOSE", context)


@pytest.mark.unit
class GetInstrumentContextFromStateTests(unittest.TestCase):
    def test_prefers_precomputed_context(self):
        state = {"company_of_interest": "FPT", "instrument_context": "PRECOMPUTED"}
        self.assertEqual(get_instrument_context_from_state(state), "PRECOMPUTED")

    def test_fallback_is_network_free_ticker_only(self):
        # No instrument_context and no listing call — must not hit the network.
        with patch(LISTING) as mock:
            context = get_instrument_context_from_state(
                {"company_of_interest": "HPG", "asset_type": "stock"}
            )
        mock.assert_not_called()
        self.assertIn("HPG", context)
        self.assertNotIn("Resolved identity", context)

    def test_fallback_describes_a_vietnamese_stock(self):
        context = get_instrument_context_from_state(
            {"company_of_interest": "VCB", "asset_type": "stock"}
        )
        self.assertIn("Vietnamese stock `VCB`", context)


if __name__ == "__main__":
    unittest.main()
