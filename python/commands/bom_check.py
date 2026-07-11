"""BOM availability checking against the local JLCPCB parts database.

Pure grouping/matching logic lives here so it is testable without a board
or the sqlite catalog; the handler in handlers/jlcpcb.py extracts
components from the loaded board and injects the catalog lookups.
"""

import logging
import re
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("kicad_interface")

# Common chip package tokens found in KiCad footprint names
# (e.g. "Resistor_SMD:R_0603_1608Metric" -> "0603").
_PACKAGE_RE = re.compile(
    r"(?:^|[_\-])"
    r"(0201|0402|0603|0805|1206|1210|1812|2010|2512|"
    r"SOT-\d+(?:-\d+)?|SOIC-\d+|TSSOP-\d+|MSOP-\d+|QFN-\d+|LQFP-\d+|"
    r"TQFP-\d+|BGA-\d+|DIP-\d+|TO-\d+(?:-\d+)?|DO-\d+|SOD-\d+)"
    r"(?:[_\-]|$)",
    re.IGNORECASE,
)


def extract_package(footprint_id: str) -> Optional[str]:
    """Best-effort package token from a KiCad footprint id (lib:name)."""
    name = footprint_id.split(":", 1)[-1]
    m = _PACKAGE_RE.search(name)
    return m.group(1).upper() if m else None


def group_bom(components: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group components into BOM line items by (value, footprint).

    components: [{"reference", "value", "footprint", "lcsc": optional}]
    A line's lcsc is taken from the first component that carries one; a
    conflict within one line is surfaced as a warning on the line.
    """
    lines: Dict[Any, Dict[str, Any]] = {}
    for comp in components:
        key = (comp.get("value", ""), comp.get("footprint", ""))
        line = lines.setdefault(
            key,
            {
                "value": comp.get("value", ""),
                "footprint": comp.get("footprint", ""),
                "package": extract_package(comp.get("footprint", "") or ""),
                "references": [],
                "quantity": 0,
                "lcsc": None,
                "warnings": [],
            },
        )
        line["references"].append(comp.get("reference", "?"))
        line["quantity"] += 1
        lcsc = (comp.get("lcsc") or "").strip() or None
        if lcsc:
            if line["lcsc"] is None:
                line["lcsc"] = lcsc
            elif line["lcsc"] != lcsc:
                line["warnings"].append(
                    f"conflicting LCSC numbers in one line: {line['lcsc']} vs {lcsc}"
                )
    result = list(lines.values())
    for line in result:
        line["references"].sort()
    result.sort(key=lambda item: item["references"][0])
    return result


def _unit_price_at_qty(price_breaks: List[Dict[str, Any]], qty: int) -> Optional[float]:
    """Pick the unit price applicable to the given order quantity."""
    best: Optional[float] = None
    for brk in price_breaks or []:
        try:
            min_qty = int(brk.get("qty") or brk.get("qFrom") or brk.get("min_qty") or 1)
            price = float(brk.get("price"))
        except (TypeError, ValueError):
            continue
        if min_qty <= qty:
            best = price if best is None else min(best, price)
    if best is None and price_breaks:
        try:
            best = float(price_breaks[0].get("price"))
        except (TypeError, ValueError):
            best = None
    return best


def evaluate_bom_lines(
    lines: List[Dict[str, Any]],
    *,
    lookup_lcsc: Callable[[str], Optional[Dict[str, Any]]],
    search: Callable[..., Dict[str, Any]],
    board_qty: int = 1,
) -> Dict[str, Any]:
    """Match each BOM line against the catalog and compute availability.

    lookup_lcsc: lcsc number -> part dict (with stock/price_breaks) or None.
    search: kwargs(query, package, in_stock, limit) -> {"parts": [...], ...}.
    """
    matched = 0
    not_found: List[str] = []
    out_of_stock: List[str] = []
    total_cost = 0.0
    cost_complete = True

    for line in lines:
        needed = line["quantity"] * board_qty
        part: Optional[Dict[str, Any]] = None
        line["matchMode"] = None
        line["candidates"] = 0

        if line.get("lcsc"):
            part = lookup_lcsc(line["lcsc"])
            if part:
                line["matchMode"] = "lcsc"
                line["candidates"] = 1
            else:
                line["warnings"].append(f"LCSC {line['lcsc']} not found in local catalog")

        if part is None:
            query = line["value"]
            try:
                result = search(query=query, package=line.get("package"), in_stock=False, limit=5)
                parts = result.get("parts", [])
            except Exception as search_err:  # noqa: BLE001 — degrade to not-found
                logger.warning(f"BOM search failed for {query!r}: {search_err}")
                parts = []
            line["candidates"] = len(parts)
            if parts:
                part = parts[0]
                line["matchMode"] = "search" if len(parts) == 1 else "search_ambiguous"

        if part is None:
            line["status"] = "not_found"
            not_found.append(line["references"][0])
            cost_complete = False
            continue

        stock = part.get("stock")
        stock = int(stock) if stock is not None else None
        price_breaks = part.get("price_breaks") or []
        unit_price = _unit_price_at_qty(price_breaks, needed)

        line["match"] = {
            "lcsc": part.get("lcsc"),
            "mpn": part.get("mfr") or part.get("mpn"),
            "description": part.get("description"),
            "package": part.get("package"),
            "stock": stock,
            "unitPrice": unit_price,
            "libraryType": part.get("library_type") or part.get("basic"),
        }
        if stock is not None and stock < needed:
            line["status"] = "out_of_stock" if stock == 0 else "low_stock"
            out_of_stock.append(line["references"][0])
        else:
            line["status"] = "ok"
        matched += 1
        if unit_price is not None:
            total_cost += unit_price * line["quantity"]
        else:
            cost_complete = False

    return {
        "lines": lines,
        "summary": {
            "totalLines": len(lines),
            "matched": matched,
            "notFound": not_found,
            "stockIssues": out_of_stock,
            "boardQty": board_qty,
            "estimatedCostPerBoard": round(total_cost, 4),
            "costComplete": cost_complete,
        },
    }
