"""Tests for check_bom_availability: grouping, package extraction, matching."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.bom_check import (
    evaluate_bom_lines,
    extract_package,
    group_bom,
)


@pytest.mark.unit
class TestExtractPackage:
    def test_chip_packages(self):
        assert extract_package("Resistor_SMD:R_0603_1608Metric") == "0603"
        assert extract_package("Capacitor_SMD:C_0402_1005Metric") == "0402"

    def test_ic_packages(self):
        assert extract_package("Package_TO_SOT_SMD:SOT-23-5") == "SOT-23-5"
        assert extract_package("Package_SO:SOIC-8_3.9x4.9mm_P1.27mm") == "SOIC-8"

    def test_no_package_token(self):
        assert extract_package("Connector_PinHeader:PinHeader_1x04_P2.54mm") is None


@pytest.mark.unit
class TestGroupBom:
    def test_groups_by_value_and_footprint(self):
        comps = [
            {"reference": "R1", "value": "10k", "footprint": "R_0603"},
            {"reference": "R2", "value": "10k", "footprint": "R_0603"},
            {"reference": "R3", "value": "10k", "footprint": "R_0805"},
        ]
        lines = group_bom(comps)
        assert len(lines) == 2
        first = next(ln for ln in lines if ln["footprint"] == "R_0603")
        assert first["quantity"] == 2
        assert first["references"] == ["R1", "R2"]

    def test_lcsc_taken_from_first_and_conflict_warned(self):
        comps = [
            {"reference": "C1", "value": "100n", "footprint": "C_0402", "lcsc": "C1525"},
            {"reference": "C2", "value": "100n", "footprint": "C_0402", "lcsc": "C9999"},
        ]
        (line,) = group_bom(comps)
        assert line["lcsc"] == "C1525"
        assert any("conflicting LCSC" in w for w in line["warnings"])


@pytest.mark.unit
class TestEvaluateBomLines:
    CATALOG = {
        "C1525": {
            "lcsc": "C1525",
            "mfr": "CL05B104KO5NNNC",
            "stock": 100000,
            "package": "0402",
            "price_breaks": [{"qty": 1, "price": 0.004}, {"qty": 100, "price": 0.002}],
        }
    }

    def _search(self, **kw):
        if kw.get("query") == "10k":
            return {
                "parts": [
                    {
                        "lcsc": "C25804",
                        "mfr": "0603WAF1002T5E",
                        "stock": 50,
                        "package": "0603",
                        "price_breaks": [{"qty": 1, "price": 0.001}],
                    }
                ]
            }
        return {"parts": []}

    def test_lcsc_exact_match_with_price_break(self):
        lines = group_bom(
            [{"reference": "C1", "value": "100n", "footprint": "C_0402", "lcsc": "C1525"}]
        )
        report = evaluate_bom_lines(
            lines,
            lookup_lcsc=self.CATALOG.get,
            search=self._search,
            board_qty=100,
        )
        (line,) = report["lines"]
        assert line["status"] == "ok"
        assert line["matchMode"] == "lcsc"
        assert line["match"]["unitPrice"] == pytest.approx(0.002)  # 100-qty break
        assert report["summary"]["matched"] == 1

    def test_search_fallback_and_stock_issue(self):
        lines = group_bom(
            [{"reference": f"R{i}", "value": "10k", "footprint": "R_0603"} for i in range(3)]
        )
        report = evaluate_bom_lines(
            lines,
            lookup_lcsc=self.CATALOG.get,
            search=self._search,
            board_qty=100,  # need 300, stock 50
        )
        (line,) = report["lines"]
        assert line["matchMode"] == "search"
        assert line["status"] == "low_stock"
        assert report["summary"]["stockIssues"] == ["R0"]

    def test_not_found_line(self):
        lines = group_bom([{"reference": "U1", "value": "OBSCURE-IC", "footprint": "QFN-32"}])
        report = evaluate_bom_lines(
            lines, lookup_lcsc=self.CATALOG.get, search=self._search, board_qty=1
        )
        (line,) = report["lines"]
        assert line["status"] == "not_found"
        assert report["summary"]["notFound"] == ["U1"]
        assert report["summary"]["costComplete"] is False

    def test_cost_per_board_sums_matched_lines(self):
        comps = [
            {"reference": "C1", "value": "100n", "footprint": "C_0402", "lcsc": "C1525"},
            {"reference": "C2", "value": "100n", "footprint": "C_0402", "lcsc": "C1525"},
        ]
        report = evaluate_bom_lines(
            group_bom(comps), lookup_lcsc=self.CATALOG.get, search=self._search, board_qty=1
        )
        # qty 2 at unit price 0.004 (qty-1 break applies: needed=2)
        assert report["summary"]["estimatedCostPerBoard"] == pytest.approx(0.008)
