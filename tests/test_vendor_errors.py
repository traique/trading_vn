"""The vendor data-error hierarchy: every "vendor couldn't return usable data"
condition derives from VendorError, so the router catches base types and any
vendor slots in without new handling.
"""
import copy
import unittest
from unittest import mock

import pytest

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.dataflows import router
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)


class DnseRateLimitError(VendorUnavailableError):
    """A vendor-named throttle, as a vendor module would define one."""


class DnseNotConfiguredError(VendorNotConfiguredError):
    """A vendor-named missing-configuration error."""


@pytest.mark.unit
class HierarchyTests(unittest.TestCase):
    def test_all_conditions_derive_from_vendor_error(self):
        for cls in (NoMarketDataError, VendorUnavailableError, VendorNotConfiguredError):
            self.assertTrue(issubclass(cls, VendorError))

    def test_not_configured_is_still_a_value_error(self):
        # Back-compat: existing `except ValueError` callers keep working.
        self.assertTrue(issubclass(VendorNotConfiguredError, ValueError))

    def test_vendor_named_errors_subclass_the_generic_bases(self):
        self.assertTrue(issubclass(DnseRateLimitError, VendorUnavailableError))
        self.assertTrue(issubclass(DnseNotConfiguredError, VendorNotConfiguredError))
        # ... and therefore still ValueErrors
        self.assertTrue(issubclass(DnseNotConfiguredError, ValueError))


@pytest.mark.unit
class RouterHandlesBaseTypesTests(unittest.TestCase):
    def setUp(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def tearDown(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def test_rate_limit_subclass_caught_by_base(self):
        # A vendor-named rate-limit error skips to the next vendor in the chain.
        set_config({"data_vendors": {"core_stock_apis": "dnse,vci"}})

        def _throttled(*a, **k):
            raise DnseRateLimitError("slow down")

        with mock.patch.dict(
            router.VENDOR_METHODS,
            {"get_stock_data": {"dnse": _throttled, "vci": lambda *a, **k: "VCI"}},
            clear=False,
        ):
            out = router.route_to_vendor("get_stock_data", "FPT", "2026-01-01", "2026-01-10")
        self.assertEqual(out, "VCI")

    def test_not_configured_falls_through_to_next_vendor(self):
        set_config({"data_vendors": {"core_stock_apis": "dnse,vci"}})

        def _unconfigured(*a, **k):
            raise DnseNotConfiguredError("no key")

        with mock.patch.dict(
            router.VENDOR_METHODS,
            {"get_stock_data": {"dnse": _unconfigured, "vci": lambda *a, **k: "VCI"}},
            clear=False,
        ):
            out = router.route_to_vendor("get_stock_data", "FPT", "2026-01-01", "2026-01-10")
        self.assertEqual(out, "VCI")

    def test_sole_unconfigured_vendor_surfaces_the_error(self):
        # With no fallback, the not-configured condition must surface (not vanish).
        set_config({"data_vendors": {"core_stock_apis": "dnse"}})

        def _unconfigured(*a, **k):
            raise DnseNotConfiguredError("no key")

        with mock.patch.dict(
            router.VENDOR_METHODS,
            {"get_stock_data": {"dnse": _unconfigured}},
            clear=False,
        ), self.assertRaises(DnseNotConfiguredError):
            router.route_to_vendor("get_stock_data", "FPT", "2026-01-01", "2026-01-10")

    def test_an_unavailable_vendor_keeps_absence_from_being_claimed(self):
        # The throttled vendor's coverage was never learned, so "no usable data
        # from any configured vendor" would be a claim nobody checked.
        def throttled(*a, **k):
            raise VendorUnavailableError("DNSE HTTP 429")

        def timed_out(*a, **k):
            raise TimeoutError("read timed out")    # a vendor that does not type its failures

        def not_covered(*a, **k):
            raise NoMarketDataError("FPT", "FPT", "not covered")

        for chain in ({"vci": throttled, "dnse": not_covered},
                      {"vci": not_covered, "dnse": throttled},
                      {"dnse": timed_out, "vci": not_covered}):
            set_config({"data_vendors": {"core_stock_apis": ",".join(chain)}})
            with mock.patch.dict(router.VENDOR_METHODS, {"get_stock_data": chain}, clear=False):
                out = router.route_to_vendor("get_stock_data", "FPT", "2026-01-01", "2026-01-10")
            self.assertTrue(out.startswith("DATA_UNAVAILABLE"), out)


if __name__ == "__main__":
    unittest.main()
