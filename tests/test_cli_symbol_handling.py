"""CLI symbol validation/classification must agree with the data path.

The CLI accepts the ways users write a Vietnamese code (``fpt``, ``FPT.VN``,
``HOSE:FPT``, ``VN-Index``) and passes on exactly the code the data path prices.
"""
import pytest

import cli.run as cli_run
from cli.models import AssetType
from cli.prompts import (
    detect_asset_type,
    is_valid_ticker_input,
    normalize_ticker_symbol,
    parse_ticker,
)
from tradingagents.dataflows.symbols import normalize_symbol


@pytest.mark.parametrize("value,ok", [
    ("FPT", True),
    ("fpt.vn", True),
    ("HOSE:SSI", True),
    ("VN-Index", True),
    ("^VNINDEX", True),
    ("", True),                 # empty -> defaults to FPT downstream
    ("bad symbol!", False),     # space + '!' rejected
    ("A" * 40, False),          # too long
])
def test_ticker_input_validation(value, ok):
    assert is_valid_ticker_input(value) is ok


@pytest.mark.parametrize("raw", ["FPT", "HPG.VN", "HNX:SHS"])
def test_every_ticker_is_a_vietnamese_stock(raw):
    assert detect_asset_type(raw) == AssetType.STOCK


def test_cli_normalize_delegates_to_data_layer():
    # CLI must produce the same canonical symbol the data path will price.
    for raw in ("fpt", "FPT.VN", "HOSE:FPT", "VN-Index"):
        assert normalize_ticker_symbol(raw) == normalize_symbol(raw)


def test_a_flag_ticker_is_canonical():
    assert parse_ticker("hose:fpt") == "FPT"
    with pytest.raises(ValueError):
        parse_ticker("")


@pytest.mark.unit
def test_the_run_directory_cannot_escape_the_results_directory(tmp_path, monkeypatch):
    """Every other path that interpolates a ticker validates it first; the CLI's
    own results tree did not, so a ticker of '..' wrote a level up."""

    with pytest.raises(ValueError):
        cli_run._run_directory({"results_dir": str(tmp_path)}, "..", "2026-09-01")

    ok = cli_run._run_directory({"results_dir": str(tmp_path)}, "FPT", "2026-09-01")
    assert str(ok).startswith(str(tmp_path))
