""".kicad_sym file parsing and property extraction.

Split out of the former monolithic commands/library_symbol.py.
"""

import logging
import re
from typing import Dict, List

from ._models import SymbolInfo

logger = logging.getLogger("kicad_interface")

# A double-quoted S-expression string, allowing backslash escapes so an
# escaped quote (\") inside the value doesn't prematurely end the match.
# KiCad escapes " as \" and \ as \\ in property values — e.g. power-symbol
# descriptions read `"Power symbol creates a global label with name \"+5V\""`.
# The old `"([^"]*)"` capture truncated those at the first inner quote,
# yielding `Power symbol ... with name \` (F7).
_QUOTED_STRING = r'"((?:\\.|[^"\\])*)"'
_PROP_RE = re.compile(r"\(property\s+" + _QUOTED_STRING + r"\s+" + _QUOTED_STRING)

# LCSC part number property names, most-authoritative first.  easyeda2kicad
# (the LCSC/JLCPCB importer, see commands/easyeda_import.py) writes the id as
# ``"LCSC Part"``; some hand-authored/JLCPCB libraries use plain ``"LCSC"``.
# Reading only ``"LCSC"`` left every easyeda-imported symbol with an empty
# lcsc_id, so search_library_parts / search_symbols never matched by LCSC id.
_LCSC_PROPERTY_NAMES = ("LCSC Part", "LCSC")


def _lcsc_from_properties(properties: Dict[str, str]) -> str:
    """Return the LCSC part number from a symbol's properties, or ``""``.

    Accepts both ``"LCSC Part"`` (easyeda2kicad) and ``"LCSC"`` (hand-authored),
    preferring the former, with a cheap case-insensitive fallback for odd
    casings — the properties dict is only a handful of entries per symbol.
    """
    for key in _LCSC_PROPERTY_NAMES:
        if properties.get(key):
            return properties[key]
    wanted = {name.lower() for name in _LCSC_PROPERTY_NAMES}
    for key, value in properties.items():
        if key.lower() in wanted and value:
            return value
    return ""


def _find_block_end(content: str, start_pos: int) -> int:
    r"""Return the index one past the ``)`` that closes the paren at ``start_pos``.

    Tracks S-expression paren depth **string-aware**: parentheses that appear
    inside double-quoted strings must not move the depth counter.  KiCad
    ``.kicad_sym`` files routinely embed unbalanced parens inside string
    values — pin names such as ``"PA13(JTMS"`` / ``"PA14(JTCK"`` and
    descriptions like ``"... MCU (Cortex-M33)"``.  A naive ``(``/``)`` counter
    mis-tracks depth on those, walks past the true block end (often to EOF),
    and the symbol is logged "Malformed symbol block" and silently dropped —
    which made whole libraries (e.g. MCU_ST_STM32H5) unsearchable.

    Backslash escapes inside strings are honoured the same way as
    ``_QUOTED_STRING`` / ``_unescape_sexpr_string``: a backslash escapes the
    following character verbatim, so an escaped quote ``\"`` does not end the
    string and an escaped backslash ``\\`` doesn't escape the next character.

    ``start_pos`` is expected to sit on the opening ``(`` of the block (both
    callers pass ``(symbol "..."`` match starts).  Returns ``start_pos``
    unchanged when the parens never balance (a truly malformed / truncated
    block), which callers treat as a skip signal.
    """
    depth = 0
    i = start_pos
    n = len(content)
    in_string = False
    while i < n:
        ch = content[i]
        if in_string:
            if ch == "\\":
                # Skip the escaped char (\" or \\ or any other) so it can
                # neither close the string nor be seen as its own escape.
                i += 2
                continue
            if ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return start_pos


def _unescape_sexpr_string(s: str) -> str:
    r"""Decode S-expression string escapes so callers get the literal text.

    KiCad writes property values as double-quoted S-expression strings with
    backslash escapes; a backslash escapes the following character verbatim,
    which correctly undoes both ``\"`` -> ``"`` and ``\\`` -> ``\``.
    """
    if "\\" not in s:
        return s
    out: List[str] = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n:
            out.append(s[i + 1])
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


class ParsingMixin:
    def _parse_kicad_sym_file(self, library_path: str, library_name: str) -> List[SymbolInfo]:
        """
        Parse a .kicad_sym file to extract symbol metadata

        Args:
            library_path: Path to the .kicad_sym file
            library_name: Nickname of the library

        Returns:
            List of SymbolInfo objects
        """
        symbols = []

        try:
            with open(library_path, "r", encoding="utf-8") as f:
                content = f.read()

            # Find all top-level symbol definitions
            # Pattern: (symbol "SYMBOL_NAME" ... ) at the top level
            # We need to find symbols that are direct children of kicad_symbol_lib
            # and not sub-symbols (which have names like "PARENT_0_1")

            # Simple approach: find all (symbol "NAME" and filter out sub-symbols
            symbol_pattern = r'\(symbol\s+"([^"]+)"'

            for match in re.finditer(symbol_pattern, content):
                symbol_name = match.group(1)

                # Skip sub-symbols (they contain _0_, _1_, etc. suffixes)
                if re.search(r"_\d+_\d+$", symbol_name):
                    continue

                # Find the start position of this symbol
                start_pos = match.start()

                # Walk forward tracking parenthesis depth to find the true end
                # of the block.  String-aware so unbalanced parens inside
                # quoted values (pin names like "PA13(JTMS", descriptions) do
                # not corrupt the depth count and drop the symbol.
                end_pos = _find_block_end(content, start_pos)

                if end_pos == start_pos:
                    logger.warning(
                        f"Malformed symbol block for '{symbol_name}' in {library_path}; skipping"
                    )
                    continue

                symbol_block = content[start_pos:end_pos]

                # Extract properties
                properties = self._extract_properties(symbol_block)

                symbol_info = SymbolInfo(
                    name=symbol_name,
                    library=library_name,
                    full_ref=f"{library_name}:{symbol_name}",
                    value=properties.get("Value", ""),
                    description=properties.get("Description", ""),
                    footprint=properties.get("Footprint", ""),
                    lcsc_id=_lcsc_from_properties(properties),
                    manufacturer=properties.get("Manufacturer", ""),
                    mpn=properties.get("Part", properties.get("MPN", "")),
                    category=properties.get("Category", ""),
                    datasheet=properties.get("Datasheet", ""),
                    stock=properties.get("Stock", ""),
                    price=properties.get("Price", ""),
                    lib_class=properties.get("Class", ""),
                    sim_pins=properties.get("Sim.Pins", ""),
                )

                symbols.append(symbol_info)

            logger.debug(f"Parsed {len(symbols)} symbols from {library_name}")

        except (OSError, ValueError) as e:
            logger.exception(f"Error parsing symbol library {library_path}: {e}")

        return symbols

    def _extract_properties(self, symbol_block: str) -> Dict[str, str]:
        """Extract properties from a symbol block.

        Property values are double-quoted S-expression strings that may
        contain escaped quotes (``\\"``) — common in power-symbol
        descriptions like ``"... with name \\"+5V\\""``.  ``_PROP_RE``
        tolerates the escapes so the value isn't truncated at the first
        inner quote, and both key and value are unescaped so callers get
        the literal text rather than raw ``\\"`` sequences.
        """
        properties: Dict[str, str] = {}

        for match in _PROP_RE.finditer(symbol_block):
            key = _unescape_sexpr_string(match.group(1))
            value = _unescape_sexpr_string(match.group(2))
            properties[key] = value

        return properties
