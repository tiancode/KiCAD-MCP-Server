"""S-expression helpers for editing KiCAD ``.kicad_sch`` text directly.

These are pure string-manipulation utilities with no KiCAD/pcbnew dependency.
They were previously private methods on ``KiCADInterface`` (and a duplicated
``_escape_sexpr_string`` in ``commands/dynamic_symbol_loader.py``); collecting
them here removes the duplication and lets them be unit-tested in isolation.

Note: these intentionally do not understand string literals when matching
parentheses — that is fine for ``.kicad_sch`` files because property values
cannot contain a bare ``(`` or ``)`` (they would be backslash-escaped).
"""

import re
from typing import Any, Dict, Tuple


def escape_sexpr_string(value: str) -> str:
    """Escape a string for safe insertion into an S-expression double-quoted token.

    Backslash first (so the quote-escape's backslash isn't doubled), then the
    double-quote. A user-supplied value like ``2.9" EPD FPC (24P)`` carries a
    literal ``"`` that, written raw into ``(property "Value" "...")``, would open
    a second string and corrupt the whole ``.kicad_sch``.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def find_matching_paren(s: str, start: int) -> int:
    """Return the index of the closing paren matching the opening paren at ``start``.

    Returns -1 if no match is found. Does not understand string literals — that's
    fine for KiCAD .kicad_sch files because property values cannot contain a
    bare ``(`` or ``)`` character (they would be backslash-escaped).
    """
    depth = 0
    i = start
    while i < len(s):
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def set_property_in_block(
    block: str,
    name: str,
    spec: Dict[str, Any],
    default_position: Tuple[float, float],
) -> Tuple[str, str]:
    """Add or update a property within a placed-symbol block.

    Args:
        block: The full text of the (symbol ...) block.
        name: Property name (e.g. "MPN", "Manufacturer").
        spec: Dict that may contain keys: value, x, y, angle, hide, fontSize.
        default_position: (x, y) of the parent symbol — used as the default
            location for newly-created properties so the field is anchored
            near the component, not at (0, 0).

    Returns:
        Tuple of (new_block_text, action_taken) where action is "added" or "updated".
    """
    new_value = spec.get("value")
    new_x = spec.get("x")
    new_y = spec.get("y")
    new_angle = spec.get("angle")
    new_hide = spec.get("hide")
    font_size = spec.get("fontSize", 1.27)

    existing_match = re.search(
        r'\(property\s+"' + re.escape(name) + r'"\s+"',
        block,
    )

    if existing_match:
        # Property exists — patch value / position / hide in place
        if new_value is not None:
            escaped = escape_sexpr_string(str(new_value))
            block = re.sub(
                r'(\(property\s+"' + re.escape(name) + r'"\s+)"[^"]*"',
                rf'\1"{escaped}"',
                block,
                count=1,
            )

        if new_x is not None or new_y is not None or new_angle is not None:
            pos_match = re.search(
                r'(\(property\s+"'
                + re.escape(name)
                + r'"\s+"[^"]*"\s+\(at\s+)([\d\.\-]+)\s+([\d\.\-]+)\s+([\d\.\-]+)(\s*\))',
                block,
            )
            if pos_match:
                cx = new_x if new_x is not None else float(pos_match.group(2))
                cy = new_y if new_y is not None else float(pos_match.group(3))
                ca = new_angle if new_angle is not None else float(pos_match.group(4))
                block = (
                    block[: pos_match.start()]
                    + pos_match.group(1)
                    + f"{cx} {cy} {ca}"
                    + pos_match.group(5)
                    + block[pos_match.end() :]
                )

        if new_hide is not None:
            block = set_hide_on_property(block, name, bool(new_hide))

        return block, "updated"

    # Property does not exist — append a new one after the last existing property
    if new_value is None:
        # Adding a brand-new property requires at least a value
        raise ValueError(
            f"Property '{name}' does not exist on this component yet — supply a value to create it"
        )

    cx = new_x if new_x is not None else default_position[0]
    cy = new_y if new_y is not None else default_position[1]
    ca = new_angle if new_angle is not None else 0
    # New properties default to hidden (BOM/sourcing data normally has no
    # visible footprint on the schematic canvas).
    hide_str = "(hide yes)" if (new_hide is None or new_hide) else "(hide no)"
    escaped = escape_sexpr_string(str(new_value))
    escaped_name = escape_sexpr_string(str(name))

    new_prop = (
        f'    (property "{escaped_name}" "{escaped}" (at {cx} {cy} {ca})\n'
        f"      (effects (font (size {font_size} {font_size})) {hide_str})\n"
        f"    )"
    )

    # Find the last existing property block and insert immediately after it.
    last_prop_end = -1
    for m in re.finditer(r'\(property\s+"', block):
        end = find_matching_paren(block, m.start())
        if end > last_prop_end:
            last_prop_end = end

    if last_prop_end < 0:
        # No properties at all — insert just before the closing paren of the symbol
        block_close = block.rfind(")")
        if block_close < 0:
            raise ValueError("Malformed symbol block: no closing paren")
        block = block[:block_close] + "\n" + new_prop + "\n  " + block[block_close:]
    else:
        block = block[: last_prop_end + 1] + "\n" + new_prop + block[last_prop_end + 1 :]

    return block, "added"


def set_hide_on_property(block: str, name: str, hide: bool) -> str:
    """Set the (hide yes|no) flag on a named property's effects clause.

    Handles three pre-existing forms:
        (effects (font (size 1.27 1.27)))                   — no hide flag
        (effects (font (size 1.27 1.27)) hide)              — legacy bare token
        (effects (font (size 1.27 1.27)) (hide yes|no))     — KiCad 9 form
    """
    prop_match = re.search(
        r'\(property\s+"' + re.escape(name) + r'"',
        block,
    )
    if not prop_match:
        return block
    prop_start = prop_match.start()
    prop_end = find_matching_paren(block, prop_start)
    if prop_end < 0:
        return block

    # Locate the (effects ...) clause inside the property
    prop_segment = block[prop_start : prop_end + 1]
    eff_match = re.search(r"\(effects\b", prop_segment)
    if not eff_match:
        return block
    eff_start = prop_start + eff_match.start()
    eff_end = find_matching_paren(block, eff_start)
    if eff_end < 0:
        return block

    eff_inner = block[eff_start + 1 : eff_end]  # 'effects (font ...) ...'
    eff_inner = re.sub(r"\s*\(hide\s+(yes|no)\)", "", eff_inner)
    eff_inner = re.sub(r"\s+hide\b(?!\s+(yes|no))", "", eff_inner)
    eff_inner = eff_inner.rstrip() + f' (hide {"yes" if hide else "no"})'

    new_effects = "(" + eff_inner + ")"
    return block[:eff_start] + new_effects + block[eff_end + 1 :]


def remove_property_from_block(block: str, name: str) -> Tuple[str, bool]:
    """Remove a property from the symbol block. Returns (new_block, removed_bool)."""
    m = re.search(r'\(property\s+"' + re.escape(name) + r'"\s+"', block)
    if not m:
        return block, False
    start = m.start()
    end = find_matching_paren(block, start)
    if end < 0:
        return block, False

    # Trim surrounding whitespace (leading newline + indent) so the resulting
    # file does not develop blank lines after every removal.
    trim_start = start
    while trim_start > 0 and block[trim_start - 1] in (" ", "\t"):
        trim_start -= 1
    if trim_start > 0 and block[trim_start - 1] == "\n":
        trim_start -= 1
    return block[:trim_start] + block[end + 1 :], True
