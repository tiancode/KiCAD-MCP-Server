"""
Datasheet Manager for KiCAD MCP Server

Enriches KiCAD schematic symbols with datasheet URLs derived from LCSC part
numbers (or the library symbol's own Datasheet). Uses direct text manipulation
(like dynamic_symbol_loader.py) to avoid skip-library-induced schematic
corruption.

URL schema: https://www.lcsc.com/datasheet/{LCSC#}.pdf
No API key required.
"""

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from utils.sexpr import escape_sexpr_string, find_block_end

logger = logging.getLogger("kicad_interface")

LCSC_DATASHEET_URL = "https://www.lcsc.com/datasheet/{lcsc}.pdf"
LCSC_PRODUCT_URL = "https://www.lcsc.com/product-detail/{lcsc}.html"

# Values treated as "empty" datasheet
EMPTY_DATASHEET_VALUES = {"~", "", "~{DATASHEET}"}

# LCSC part number is stored under "LCSC Part" by easyeda2kicad imports and under
# a plain "LCSC" by some hand-built libraries — accept both.
_LCSC_PROPERTY_NAMES = ("LCSC Part", "LCSC")

# References we never treat as BOM parts: offscreen placement templates and
# power/flag symbols (#PWR0x, #FLG0x, PWR_FLAG).
_NON_BOM_REF_PREFIXES = ("_TEMPLATE", "#")


def _property_value(block: str, name: str) -> Optional[str]:
    """Raw (still-escaped) value of the first ``(property "name" "value" …)`` in
    ``block``, or None. ``\\s+`` spans newlines so the easyeda2kicad layout —
    where name/value sit on their own lines below ``(property`` — matches too.
    """
    m = re.search(
        r'\(property\s+"' + re.escape(name) + r'"\s+"((?:[^"\\]|\\.)*)"',
        block,
    )
    return m.group(1) if m else None


class DatasheetManager:
    """
    Enriches KiCAD schematics with datasheet URLs.

    Reads .kicad_sch files, finds placed symbol instances whose Datasheet is
    empty, and fills it from the symbol's LCSC part number (constructing the
    LCSC datasheet URL) or, failing that, from the library symbol's own
    Datasheet property.
    """

    @staticmethod
    def _normalize_lcsc(lcsc: str) -> Optional[str]:
        """
        Normalize LCSC number to standard format 'C123456'.

        Accepts: 'C123456', '123456', 'c123456'
        Returns: 'C123456' or None if invalid
        """
        lcsc = lcsc.strip()
        if not lcsc:
            return None
        without_prefix = lcsc.lstrip("Cc")
        if without_prefix.isdigit():
            return f"C{without_prefix}"
        return None

    @staticmethod
    def _find_lib_symbols_span(content: str) -> Tuple[int, int]:
        """Char span ``[start, end)`` of the ``(lib_symbols …)`` block.

        Returns ``(-1, -1)`` when absent. Quote/escape aware so it survives
        library symbol property values that contain literal parens.
        """
        start = content.find("(lib_symbols")
        if start == -1:
            return -1, -1
        return start, find_block_end(content, start)

    @classmethod
    def _lib_symbol_datasheets(cls, content: str, lib_start: int, lib_end: int) -> Dict[str, str]:
        """Map ``lib_id`` (``"Lib:Name"``) → raw Datasheet value for every embedded
        library symbol that carries a non-empty Datasheet.

        Used as a secondary fill source: a placed instance with an empty
        Datasheet but a resolvable ``lib_id`` inherits the library symbol's
        Datasheet (this is what KiCad copies on placement).
        """
        out: Dict[str, str] = {}
        if lib_start < 0:
            return out
        body = content[lib_start:lib_end]
        for m in re.finditer(r'\(symbol\s+"((?:[^"\\]|\\.)*)"', body):
            name = m.group(1)
            # Top-level library entries are "Lib:Name"; the nested unit/style
            # sub-symbols ("Name_0_1") have no colon and no Datasheet.
            if ":" not in name:
                continue
            sym_block = body[m.start() : find_block_end(body, m.start())]
            ds = _property_value(sym_block, "Datasheet")
            if ds is not None and ds not in EMPTY_DATASHEET_VALUES:
                out[name] = ds
        return out

    @classmethod
    def _iter_placed_symbol_blocks(cls, content: str) -> List[Tuple[int, int]]:
        """Char spans ``[start, end)`` of every placed ``(symbol (…) …)`` instance.

        Placed instances open with ``(symbol`` followed by a nested s-expression
        — ``(symbol (lib_id …)`` on one line OR ``(symbol\\n  (lib_id …)`` across
        lines (how KiCad itself saves). Library-definition symbols use the
        quoted ``(symbol "Lib:Name" …)`` form, so ``\\(symbol\\s+\\(`` matches only
        placed instances; the lib_symbols span is skipped for defence in depth.
        """
        lib_start, lib_end = cls._find_lib_symbols_span(content)
        blocks: List[Tuple[int, int]] = []
        for m in re.finditer(r"\(symbol\s+\(", content):
            pos = m.start()
            if lib_start >= 0 and lib_start <= pos < lib_end:
                continue
            end = find_block_end(content, pos)
            blocks.append((pos, end))
        return blocks

    def enrich_schematic(self, schematic_path: Path, dry_run: bool = False) -> Dict:
        """
        Scan a .kicad_sch file and fill in missing datasheet URLs.

        For each placed symbol whose Datasheet is empty (``~`` / empty), fill it
        from, in order of preference:
          1. its LCSC part number ("LCSC Part" or "LCSC") → LCSC datasheet URL, or
          2. its library symbol's Datasheet property (via lib_id).

        Args:
            schematic_path: Path to .kicad_sch file
            dry_run: If True, report what would change without writing

        Returns:
            {
                "success": True,
                "updated": <count>,
                "already_set": <count>,
                "no_lcsc": <count>,            # empty Datasheet, no fill source
                "no_datasheet_field": <count>, # placed symbol with no Datasheet field
                "skipped": <count>,            # template / power / flag symbols
                "total_symbols": <count>,
                "details": [{"reference", "source", "lcsc"?, "url"}],
            }
        """
        schematic_path = Path(schematic_path)
        if not schematic_path.exists():
            return {
                "success": False,
                "message": f"Schematic not found: {schematic_path}",
            }

        with open(schematic_path, "r", encoding="utf-8") as f:
            content = f.read()

        lib_start, lib_end = self._find_lib_symbols_span(content)
        lib_datasheets = self._lib_symbol_datasheets(content, lib_start, lib_end)

        updated = 0
        already_set = 0
        no_source = 0
        no_datasheet_field = 0
        skipped = 0
        total = 0
        details: List[Dict] = []

        # Process blocks back-to-front so in-place splices keep earlier offsets
        # valid (spans were computed against the original content).
        for block_start, block_end in sorted(
            self._iter_placed_symbol_blocks(content), reverse=True
        ):
            block = content[block_start:block_end]

            reference = _property_value(block, "Reference") or "?"
            if reference.startswith(_NON_BOM_REF_PREFIXES):
                skipped += 1
                continue
            total += 1

            ds_value = _property_value(block, "Datasheet")
            if ds_value is None:
                # No Datasheet field at all — nothing to update in place.
                no_datasheet_field += 1
                logger.warning(f"Symbol {reference} has no Datasheet property field")
                continue
            if ds_value not in EMPTY_DATASHEET_VALUES:
                already_set += 1
                logger.debug(f"Symbol {reference}: Datasheet already set to {ds_value!r}")
                continue

            # Empty Datasheet — find a fill source.
            raw_lcsc = None
            for prop in _LCSC_PROPERTY_NAMES:
                raw_lcsc = _property_value(block, prop)
                if raw_lcsc:
                    break
            lcsc_norm = self._normalize_lcsc(raw_lcsc) if raw_lcsc else None
            lib_id_match = re.search(r'\(lib_id\s+"((?:[^"\\]|\\.)*)"', block)
            lib_id = lib_id_match.group(1) if lib_id_match else None

            fill_value: Optional[str] = None  # escaped, ready to emit
            detail: Dict = {"reference": reference}
            if lcsc_norm:
                url = LCSC_DATASHEET_URL.format(lcsc=lcsc_norm)
                fill_value = escape_sexpr_string(url)
                detail.update({"source": "lcsc", "lcsc": lcsc_norm, "url": url})
            elif lib_id and lib_id in lib_datasheets:
                # lib_datasheets values are already escaped (copied verbatim).
                fill_value = lib_datasheets[lib_id]
                detail.update({"source": "lib_symbol", "url": lib_datasheets[lib_id]})
            else:
                no_source += 1
                continue

            new_block = re.sub(
                r'(\(property\s+"Datasheet"\s+)"(?:[^"\\]|\\.)*"',
                lambda mm: mm.group(1) + '"' + fill_value + '"',
                block,
                count=1,
            )
            if not dry_run:
                content = content[:block_start] + new_block + content[block_end:]
            updated += 1
            detail["dry_run"] = dry_run
            details.append(detail)
            logger.info(
                f"{'[DRY RUN] ' if dry_run else ''}Set Datasheet for "
                f"{reference} from {detail['source']}: {detail['url']}"
            )

        if not dry_run and updated > 0:
            with open(schematic_path, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info(f"Saved {schematic_path.name}: {updated} datasheet URL(s) written")

        # Details were gathered back-to-front; present them in schematic order.
        details.reverse()
        return {
            "success": True,
            "updated": updated,
            "already_set": already_set,
            "no_lcsc": no_source,
            "no_datasheet_field": no_datasheet_field,
            "skipped": skipped,
            "total_symbols": total,
            "dry_run": dry_run,
            "details": details,
            "schematic": str(schematic_path),
        }

    def get_datasheet_url(self, lcsc: str) -> Optional[str]:
        """
        Return the LCSC datasheet URL for a given LCSC number.
        No network request – pure URL construction.
        """
        norm = self._normalize_lcsc(lcsc)
        if norm:
            return LCSC_DATASHEET_URL.format(lcsc=norm)
        return None

    def get_product_url(self, lcsc: str) -> Optional[str]:
        """Return the LCSC product page URL."""
        norm = self._normalize_lcsc(lcsc)
        if norm:
            return LCSC_PRODUCT_URL.format(lcsc=norm)
        return None
