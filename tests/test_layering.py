"""Only the data layer imports vendor libraries and vendor modules.

Vendor calls belong in dataflows, where failures are raised as VendorError
subclasses; a call made elsewhere can report an outage as a fact about the market.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VENDOR_LIBRARIES = {"vnstock", "yfinance"}
VENDOR_MODULES = "tradingagents.dataflows.vendors"


def _imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
    return names


def _vendor_module_imports(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(VENDOR_MODULES):
            return True
        if isinstance(node, ast.Import) and any(a.name.startswith(VENDOR_MODULES) for a in node.names):
            return True
    return False


@pytest.mark.unit
def test_vendor_libraries_are_imported_only_by_the_data_layer():
    data_layer = ROOT / "tradingagents" / "dataflows"
    offenders = sorted(
        str(path.relative_to(ROOT))
        for package in ("tradingagents", "cli")
        for path in (ROOT / package).rglob("*.py")
        if data_layer not in path.parents and _imports(path) & VENDOR_LIBRARIES
    )
    assert offenders == []


@pytest.mark.unit
def test_vendor_modules_are_called_only_through_the_data_layer():
    """DNSE, VCI, CafeF and Google News are reached through dataflows functions,
    which turn their failures into VendorError and their answers into text."""
    data_layer = ROOT / "tradingagents" / "dataflows"
    offenders = sorted(
        str(path.relative_to(ROOT))
        for package in ("tradingagents", "cli")
        for path in (ROOT / package).rglob("*.py")
        if data_layer not in path.parents and _vendor_module_imports(path)
    )
    assert offenders == []
