"""
Pin Locator for KiCad Schematics

Discovers pin locations on symbol instances, accounting for position, rotation, and mirroring.
Uses S-expression parsing to extract pin data from symbol definitions.
"""

import logging
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata
from sexpdata import Symbol
from skip import Schematic

logger = logging.getLogger("kicad_interface")

# Multi-unit symbol sub-definitions are named "<base>_<unit>_<bodystyle>"
# (e.g. "LM2904_2_1" = unit 2, body style 1). The trailing two integer groups
# are always unit/style; the base name itself may contain "_<digits>".
_UNIT_SUFFIX_RE = re.compile(r"_(\d+)_(\d+)$")


class PinLocator:
    """Locate pins on symbol instances in KiCad schematics"""

    # Process-wide caches shared across every PinLocator instance, keyed by a
    # (abspath, size, mtime_ns) file signature. The net/label listing paths
    # create a fresh PinLocator() per net (see wire_connectivity), so without
    # sharing, the whole .kicad_sch was re-read and re-parsed — and every
    # lib_symbol re-extracted — once per net (O(nets x parse); a single
    # list_schematic_nets re-extracted Device:R 100+ times). Keying on the
    # signature keeps reuse correct: any write bumps size/mtime, which is a
    # cache miss, so a stale parse is never served. _cache_put keeps only the
    # newest signature per path, bounding memory to ~one parse per file.
    _SCHEMATIC_CACHE: Dict[Tuple[str, int, int], Any] = {}  # sig -> skip.Schematic
    _SEXP_CACHE: Dict[Tuple[str, int, int], Any] = {}  # sig -> parsed sexpdata
    _PINDEF_CACHE: Dict[Tuple[str, int, int, str], Dict[str, Dict]] = {}  # sig+lib_id -> pins

    def __init__(self) -> None:
        """Bind to the shared, mtime-keyed class caches (see class docstring)."""
        self.pin_definition_cache = PinLocator._PINDEF_CACHE

    @staticmethod
    def _file_sig(path: Any) -> Tuple[str, int, int]:
        """Cache signature for a schematic file: (abspath, size, mtime_ns).
        Any edit changes size or mtime, so a stale parse is never reused.
        Missing/unreadable file collapses to a sentinel so callers still hit
        their own not-found handling rather than crashing here."""
        p = os.path.abspath(str(path))
        try:
            st = os.stat(p)
            return (p, st.st_size, st.st_mtime_ns)
        except OSError:
            return (p, -1, -1)

    @staticmethod
    def _cache_put(cache: Dict[Any, Any], key: Tuple[Any, ...], value: Any) -> None:
        """Insert, evicting any entries for the same path with an older
        signature so a cache holds at most the newest parse per file. Works for
        both the file caches (key == sig) and the pin-def cache (key ==
        sig + (lib_id,)): identity is key[0] (path), freshness is key[:3] (sig),
        so sibling lib_ids of the current signature are preserved."""
        path, sig = key[0], key[:3]
        for stale in [k for k in cache if k[0] == path and k[:3] != sig]:
            del cache[stale]
        cache[key] = value

    def _load_skip_schematic(self, schematic_path: Any) -> Any:
        """Return a kicad-skip Schematic for the file, reusing the shared cache
        when the on-disk signature is unchanged."""
        sig = self._file_sig(schematic_path)
        sch = PinLocator._SCHEMATIC_CACHE.get(sig)
        if sch is None:
            sch = Schematic(str(schematic_path))
            self._cache_put(PinLocator._SCHEMATIC_CACHE, sig, sch)
        return sch

    def _load_sexp(self, schematic_path: Any) -> Any:
        """Return the parsed sexpdata tree for the file, reusing the shared
        cache when the on-disk signature is unchanged."""
        sig = self._file_sig(schematic_path)
        data = PinLocator._SEXP_CACHE.get(sig)
        if data is None:
            with open(schematic_path, "r", encoding="utf-8") as f:
                data = sexpdata.loads(f.read())
            self._cache_put(PinLocator._SEXP_CACHE, sig, data)
        return data

    @staticmethod
    def parse_symbol_definition(symbol_def: list) -> Dict[str, Dict]:
        """
        Parse a symbol definition from lib_symbols to extract pin information

        Args:
            symbol_def: S-expression list representing symbol definition

        Returns:
            Dictionary mapping pin number -> pin data:
            {
                "1": {"x": 0, "y": 3.81, "angle": 270, "length": 1.27, "name": "~", "type": "passive", "unit": 1},
                "2": {"x": 0, "y": -3.81, "angle": 90, "length": 1.27, "name": "~", "type": "passive", "unit": 1}
            }

        Each pin carries ``unit`` — the symbol unit that owns it, parsed from
        the ``<base>_<unit>_<bodystyle>`` sub-symbol naming convention. For a
        multi-unit part (op-amp, gate array) different pins belong to different
        units, and each unit is placed as a separate instance at its own
        location; callers must transform a pin by *its* unit's instance, not
        unit 1's, or pins collapse onto the first unit's coordinates. Pins not
        nested under a unit sub-symbol get unit 0 (common/graphic-only).
        """
        pins: Dict[str, Dict[str, Any]] = {}

        def extract_pins_recursive(sexp: Any, current_unit: int) -> None:
            """Recursively search for pin definitions, tracking the unit context"""
            if not isinstance(sexp, list) or len(sexp) == 0:
                return

            # Descending into a unit sub-symbol ("<base>_<unit>_<style>")
            # switches the unit context for every pin nested inside it.
            if sexp[0] == Symbol("symbol") and len(sexp) > 1:
                match = _UNIT_SUFFIX_RE.search(str(sexp[1]).strip('"'))
                if match:
                    current_unit = int(match.group(1))

            if sexp[0] == Symbol("pin"):
                # Pin format: (pin type shape (at x y angle) (length len) (name "name") (number "num"))
                pin_data = {
                    "x": 0,
                    "y": 0,
                    "angle": 0,
                    "length": 0,
                    "name": "",
                    "number": "",
                    "type": str(sexp[1]) if len(sexp) > 1 else "passive",
                    "unit": current_unit,
                }

                for item in sexp:
                    if isinstance(item, list) and len(item) > 0:
                        if item[0] == Symbol("at") and len(item) >= 3:
                            pin_data["x"] = float(item[1])
                            pin_data["y"] = float(item[2])
                            if len(item) >= 4:
                                pin_data["angle"] = float(item[3])

                        elif item[0] == Symbol("length") and len(item) >= 2:
                            pin_data["length"] = float(item[1])

                        elif item[0] == Symbol("name") and len(item) >= 2:
                            pin_data["name"] = str(item[1]).strip('"')

                        elif item[0] == Symbol("number") and len(item) >= 2:
                            pin_data["number"] = str(item[1]).strip('"')

                # Store by pin number. When the same pin number is defined
                # more than once in a single symbol — which happens in some
                # community-generated symbols (e.g.,
                # ``PCM_Diode_Schottky_AKL:MBRS130``) where an inner
                # zero-length "ghost" pin overlaps the real outer pin — keep
                # the definition with the greater ``length``. That is the pin
                # with a visible stub; its ``at`` coordinate is the wire-
                # connection endpoint that matches where labels and wires
                # are actually placed. Ties resolve to first-encountered, so
                # legitimate same-length duplicates (e.g., per-unit
                # repetitions in multi-unit symbols) retain stable ordering.
                if pin_data["number"]:
                    existing = pins.get(str(pin_data["number"]))
                    if existing is None or pin_data["length"] > existing["length"]:
                        pins[pin_data["number"]] = pin_data

            # Recurse into sublists, carrying the current unit context.
            for item in sexp:
                if isinstance(item, list):
                    extract_pins_recursive(item, current_unit)

        extract_pins_recursive(symbol_def, 0)
        return pins

    def get_symbol_pins(self, schematic_path: Path, lib_id: str) -> Dict[str, Dict]:
        """
        Get pin definitions for a symbol from the schematic's lib_symbols section

        Args:
            schematic_path: Path to .kicad_sch file
            lib_id: Library identifier (e.g., "Device:R", "MCU_ST_STM32F1:STM32F103C8Tx")

        Returns:
            Dictionary mapping pin number -> pin data.

            READ-ONLY: this is the shared, process-wide cached object (see the
            PinLocator class docstring), not a copy. Mutating the returned dict
            — or any inner pin_data dict — corrupts the cache for every other
            caller. Copy it first if you need to modify it. (get_all_symbol_pins
            builds a fresh dict per call and is safe to mutate.)
        """
        # Check cache (keyed by file signature so an edit invalidates it)
        cache_key = self._file_sig(schematic_path) + (lib_id,)
        if cache_key in self.pin_definition_cache:
            logger.debug(f"Using cached pin data for {lib_id}")
            return self.pin_definition_cache[cache_key]

        try:
            # Read schematic (shared, signature-keyed parse)
            sch_data = self._load_sexp(schematic_path)

            lib_symbols = None
            for item in sch_data:
                if isinstance(item, list) and len(item) > 0 and item[0] == Symbol("lib_symbols"):
                    lib_symbols = item
                    break

            if not lib_symbols:
                logger.error("No lib_symbols section found in schematic")
                return {}

            # Find the specific symbol definition.
            # KiCad lib_symbols may use a different name than the instance lib_id:
            #   instance lib_id:  "stat-tis-custom:BAT_18650"
            #   lib_symbols name: "BAT_18650_3"  (prefix stripped, unit suffix added)
            # Strategy: exact match first, then bare-name prefix match.
            bare_name = lib_id.split(":")[-1] if ":" in lib_id else lib_id

            best_match = None
            for item in lib_symbols[1:]:
                if not (isinstance(item, list) and len(item) > 1 and item[0] == Symbol("symbol")):
                    continue
                symbol_name = str(item[1]).strip('"')
                if symbol_name == lib_id:
                    best_match = item
                    break
                if best_match is None:
                    sn_bare = symbol_name.split(":")[-1] if ":" in symbol_name else symbol_name
                    if sn_bare == bare_name or (
                        sn_bare.startswith(bare_name)
                        and len(sn_bare) > len(bare_name)
                        and sn_bare[len(bare_name)] == "_"
                        and sn_bare[len(bare_name) + 1 :].isdigit()
                    ):
                        best_match = item

            if best_match is not None:
                matched_name = str(best_match[1]).strip('"')
                pins = self.parse_symbol_definition(best_match)
                self._cache_put(self.pin_definition_cache, cache_key, pins)
                if matched_name != lib_id:
                    logger.debug(
                        f"Matched {lib_id} → lib_symbols '{matched_name}' ({len(pins)} pins)"
                    )
                else:
                    logger.debug(f"Extracted {len(pins)} pins from {lib_id}")
                return pins

            logger.warning(f"Symbol {lib_id} not found in lib_symbols")
            return {}

        except (OSError, AttributeError, KeyError, ValueError, TypeError) as e:
            # API boundary — surface the failure as an empty pin dict with a
            # full traceback in the log.  Tightened from `except Exception`;
            # the wrapped code only does file IO + sexpdata attribute access.
            logger.exception(f"Error getting symbol pins: {e}")
            return {}

    @staticmethod
    def rotate_point(x: float, y: float, angle_degrees: float) -> Tuple[float, float]:
        """
        Rotate a point around the origin

        Args:
            x: X coordinate
            y: Y coordinate
            angle_degrees: Rotation angle in degrees (counterclockwise)

        Returns:
            (rotated_x, rotated_y)
        """
        if angle_degrees == 0:
            return (x, y)

        angle_rad = math.radians(angle_degrees)
        cos_a = math.cos(angle_rad)
        sin_a = math.sin(angle_rad)

        # Standard counter-clockwise rotation (math convention, Y-up).
        # Callers are responsible for any y-axis negation required to convert
        # library coordinates (y-up) to schematic coordinates (y-down) before
        # passing values here — see get_pin_location and _transform_local_point.
        rotated_x = x * cos_a - y * sin_a
        rotated_y = x * sin_a + y * cos_a

        return (rotated_x, rotated_y)

    def _get_lib_id(self, schematic_path: Path, symbol_reference: str) -> Optional[str]:
        """Helper: return the lib_id string for a placed symbol"""
        try:
            sch = self._load_skip_schematic(schematic_path)
            for symbol in sch.symbol:
                if symbol.property.Reference.value.rstrip("_") == symbol_reference:
                    return symbol.lib_id.value if hasattr(symbol, "lib_id") else None
        except (OSError, AttributeError) as e:
            # best-effort: missing schematic file (OSError) or unexpected
            # symbol shape (AttributeError on .property/.Reference/.value).
            # Caller handles the None return and falls back to other lookup
            # paths; logging the cause at debug level is enough.
            logger.debug(f"_get_lib_id({symbol_reference}): {type(e).__name__}: {e}")
        return None

    @staticmethod
    def _is_multi_unit(defined_units: Optional[Any]) -> bool:
        """True when the symbol genuinely spans more than one non-common unit.

        ``defined_units`` is the set of ``unit`` values parsed off the pins.
        Unit 0 is the common / graphic-only layer, so it is excluded: a part
        is multi-unit only when two or more *numbered* units carry pins.
        """
        if not defined_units:
            return False
        return len({u for u in defined_units if u not in (0, None)}) > 1

    def _get_symbol_transform(
        self,
        schematic_path: Path,
        symbol_reference: str,
        pin_unit: Optional[int] = None,
        defined_units: Optional[Any] = None,
    ) -> Optional[Tuple[float, float, float, bool, bool, str]]:
        """
        Read symbol position, rotation, mirror flags, and lib_id directly from the
        .kicad_sch file via sexpdata (authoritative — not kicad-skip cache, which
        does not reflect mirror/rotation changes made by rotate_schematic_component).

        Returns (x, y, rotation, mirror_x, mirror_y, lib_id) or None.

        ``pin_unit`` selects which unit's instance to read for a multi-unit part.
        Each unit is placed separately (own position/rotation/mirror), so a pin
        must be transformed by *its* unit's instance. When ``pin_unit`` is None,
        the first instance is used (back-compatible). When a specific numbered
        unit is requested but has **no placed instance**, this returns None —
        never a fabricated coordinate on another unit's instance — UNLESS the
        part is effectively single-unit (only one numbered unit defined) and a
        single instance is present, in which case that lone instance owns the
        pin even if its recorded ``(unit N)`` index differs from the pin's
        parsed unit (some community symbols mis-number). Unit 0 (common /
        graphic-only pins) always falls back to the first instance.

        ``defined_units`` (the set of unit values across the symbol's pins) lets
        this distinguish a genuinely multi-unit part with an unplaced unit — the
        F1 bug, where only unit A of a 2-unit MCU is on the sheet yet unit-B pins
        were being located on unit A's origin — from a single-unit part whose one
        instance simply records a surprising unit index.
        """
        from commands.wire_dragger import WireDragger

        try:
            sexp = self._load_sexp(schematic_path)
        except (OSError, ValueError) as e:
            # OSError covers missing/unreadable file; ValueError covers
            # sexpdata parse failures (it raises ValueError on bad syntax).
            logger.error(f"_get_symbol_transform: failed to parse {schematic_path}: {e}")
            return None

        instances = WireDragger.find_symbol_instances(sexp, symbol_reference)
        if not instances:
            return None

        if pin_unit is None:
            chosen = instances[0]
        else:
            chosen = next((inst for inst in instances if inst[7] == pin_unit), None)
            if chosen is None:
                if pin_unit == 0:
                    # Common / graphic-only pins are drawn on every unit; there
                    # is no dedicated instance, so fall back to the first one.
                    chosen = instances[0]
                elif not self._is_multi_unit(defined_units) and len(instances) == 1:
                    # Effectively single-unit: the sole placed instance owns the
                    # pin even if its recorded (unit N) differs from the pin's
                    # parsed unit (regex mis-numbering on some community symbols).
                    chosen = instances[0]
                else:
                    # Genuinely multi-unit AND this unit is not on the sheet —
                    # refuse rather than mislocate the pin onto another unit.
                    logger.debug(
                        f"{symbol_reference}: unit {pin_unit} not placed "
                        f"(present units: {sorted(inst[7] for inst in instances)})"
                    )
                    return None

        _, sym_x, sym_y, rotation, lib_id, mirror_x, mirror_y, _unit = chosen
        return sym_x, sym_y, rotation, mirror_x, mirror_y, lib_id

    def get_pin_angle(
        self, schematic_path: Path, symbol_reference: str, pin_number: str
    ) -> Optional[float]:
        """
        Get the OUTWARD angle of a pin endpoint in degrees
        (0=right, 90=up, 180=left, 270=down): the direction a wire stub must
        extend AWAY from the symbol body to stay connected to the pin.

        The angle is returned in the convention the stub/label math in
        ConnectionManager.connect_to_net consumes: a caller reaches the stub end
        with ``(pin_x + d*cos(angle), pin_y - d*sin(angle))`` in screen (Y-down)
        coordinates, so 0=+X (right) and 90=visual up.

        KiCad pin-angle semantics: in a ``.kicad_sym`` a pin's ``(at x y angle)``
        angle points from the electrical endpoint TOWARD the symbol body — a
        left-side pin is stored with angle 0 (pointing right/inward), a top pin
        with 270 (pointing down/inward). The OUTWARD direction is therefore the
        opposite of the library angle (``+180``), then transformed by the
        symbol's placement (rotation + mirror).

        Rather than re-derive the reflection algebra (which was subtly wrong: the
        old code negated the library angle for the Y-flip, which coincides with
        the outward ``+180`` only for pins that end up vertical in world space —
        so horizontal pins got an inward stub), the direction is measured the
        same way pin positions are: extend the pin one unit outward in library
        space and push both the endpoint and the extended point through
        ``WireDragger.pin_world_xy``. Because the translation cancels in the
        difference, the result is exactly the placement-transformed outward unit
        vector — guaranteed consistent with where the pin is actually drawn under
        every rotation (0/90/180/270) and mirror combination.

        Accounts for mirror flags read directly from the .kicad_sch file.

        Returns angle in degrees, or None if pin not found.
        """
        from commands.wire_dragger import WireDragger

        try:
            transform = self._get_symbol_transform(schematic_path, symbol_reference)
            if transform is None:
                return None

            sym_x, sym_y, symbol_rotation, mirror_x, mirror_y, lib_id = transform
            if not lib_id:
                return None

            pins = self.get_symbol_pins(schematic_path, lib_id)
            if pin_number not in pins:
                matched_num = next(
                    (num for num, data in pins.items() if data.get("name") == pin_number),
                    None,
                )
                if matched_num:
                    pin_number = matched_num
                else:
                    return None

            # Re-resolve the transform for the unit that actually owns this pin
            # — units can be placed with different rotations/mirrors.
            pin_unit = pins[pin_number].get("unit")
            if pin_unit is not None:
                defined_units = {p.get("unit") for p in pins.values()}
                unit_transform = self._get_symbol_transform(
                    schematic_path,
                    symbol_reference,
                    pin_unit=pin_unit,
                    defined_units=defined_units,
                )
                if unit_transform is None:
                    return None
                sym_x, sym_y, symbol_rotation, mirror_x, mirror_y, _ = unit_transform

            pin = pins[pin_number]
            px, py = pin["x"], pin["y"]

            # Extend the pin one unit OUTWARD in library space (opposite of the
            # library angle, which points inward toward the body). Length only
            # scales the vector, so a zero-length pin still yields a direction.
            out_rad = math.radians(pin.get("angle", 0) + 180.0)
            unit = pin.get("length") or 1.0
            ox = px + unit * math.cos(out_rad)
            oy = py + unit * math.sin(out_rad)

            wx_pin, wy_pin = WireDragger.pin_world_xy(
                px, py, sym_x, sym_y, symbol_rotation, mirror_x, mirror_y
            )
            wx_out, wy_out = WireDragger.pin_world_xy(
                ox, oy, sym_x, sym_y, symbol_rotation, mirror_x, mirror_y
            )

            dx = wx_out - wx_pin
            dy = wy_out - wy_pin
            # pin_world_xy returns screen coords (Y-down); the stub math reaches
            # its target with (cos θ, -sin θ), so a screen displacement (dx, dy)
            # maps back to θ = atan2(-dy, dx).
            return math.degrees(math.atan2(-dy, dx)) % 360.0

        except (AttributeError, KeyError, TypeError, ValueError) as e:
            # Pin missing from the symbol definition (KeyError), unexpected
            # transform shape (AttributeError/TypeError), or numeric coercion
            # failure (ValueError).  Log so this isn't silent — the original
            # `except Exception: return None` masked real bugs in PRs.
            logger.debug(
                f"get_pin_angle({symbol_reference}/{pin_number}): " f"{type(e).__name__}: {e}"
            )
            return None

    def get_pin_location(
        self, schematic_path: Path, symbol_reference: str, pin_number: str
    ) -> Optional[List[float]]:
        """
        Get the absolute location of a pin on a symbol instance

        Args:
            schematic_path: Path to .kicad_sch file
            symbol_reference: Symbol reference designator (e.g., "R1", "U1")
            pin_number: Pin number/identifier (e.g., "1", "2", "GND", "VCC")

        Returns:
            [x, y] absolute coordinates of the pin, or None if not found
        """
        try:
            # Load schematic with kicad-skip to get symbol instance
            # (shared, signature-keyed cache avoids reparsing per pin lookup)
            sch = self._load_skip_schematic(schematic_path)

            # Find the symbol instance.
            # skip may write references with a trailing "_" (e.g. "R1_") — strip it when comparing.
            target_symbol = None
            for symbol in sch.symbol:
                ref = symbol.property.Reference.value.rstrip("_")
                if ref == symbol_reference:
                    target_symbol = symbol
                    break

            if not target_symbol:
                logger.error(f"Symbol {symbol_reference} not found in schematic")
                return None

            # Get symbol transform from sexpdata (authoritative: reflects mirror state
            # after rotate_schematic_component, which kicad-skip cache does not).
            transform = self._get_symbol_transform(schematic_path, symbol_reference)
            if transform is None:
                logger.error(f"Could not read transform for {symbol_reference}")
                return None
            symbol_x, symbol_y, symbol_rotation, mirror_x, mirror_y, lib_id = transform

            if not lib_id:
                logger.error(f"Symbol {symbol_reference} has no lib_id")
                return None

            logger.debug(
                f"Symbol {symbol_reference}: pos=({symbol_x}, {symbol_y}), rot={symbol_rotation}, "
                f"mirror_x={mirror_x}, mirror_y={mirror_y}, lib_id={lib_id}"
            )

            pins = self.get_symbol_pins(schematic_path, lib_id)
            if not pins:
                logger.error(f"No pin definitions found for {lib_id}")
                return None

            # Find the requested pin — match by number first, then by name
            if pin_number not in pins:
                # Try matching by pin name (e.g. "VCC1", "SDA", "GND")
                matched_num = next(
                    (num for num, data in pins.items() if data.get("name") == pin_number),
                    None,
                )
                if matched_num:
                    logger.debug(
                        f"Resolved pin name '{pin_number}' to pin number '{matched_num}' on {symbol_reference}"
                    )
                    pin_number = matched_num
                else:
                    logger.error(
                        f"Pin {pin_number} not found on {symbol_reference}. Available pins: {list(pins.keys())} "
                        f"(names: {[d.get('name','') for d in pins.values()]})"
                    )
                    return None

            pin_data = pins[pin_number]
            from commands.wire_dragger import WireDragger

            # Multi-unit parts place each unit as a separate instance at its own
            # location. The pin's library coordinate is relative to *its* unit's
            # origin, so transform it by that unit's instance — not unit 1's, or
            # pins on units B/C/D collapse onto unit A's position (silently
            # shorting nets when a label snaps to the wrong pin).
            pin_unit = pin_data.get("unit")
            if pin_unit is not None:
                defined_units = {p.get("unit") for p in pins.values()}
                unit_transform = self._get_symbol_transform(
                    schematic_path,
                    symbol_reference,
                    pin_unit=pin_unit,
                    defined_units=defined_units,
                )
                if unit_transform is None:
                    logger.error(
                        f"Pin {symbol_reference}/{pin_number} belongs to unit {pin_unit}, "
                        f"which is not placed on the schematic"
                    )
                    return None
                symbol_x, symbol_y, symbol_rotation, mirror_x, mirror_y, _ = unit_transform

            abs_x, abs_y = WireDragger.pin_world_xy(
                pin_data["x"],
                pin_data["y"],
                symbol_x,
                symbol_y,
                symbol_rotation,
                mirror_x,
                mirror_y,
            )

            logger.debug(f"Pin {symbol_reference}/{pin_number} located at ({abs_x}, {abs_y})")
            return [abs_x, abs_y]

        except (OSError, AttributeError, KeyError, ValueError, TypeError) as e:
            # API boundary catch — same set of types as get_symbol_pins.
            # Logged with full traceback via logger.exception.
            logger.exception(f"Error getting pin location: {e}")
            return None

    def get_all_symbol_pins(
        self, schematic_path: Path, symbol_reference: str
    ) -> Dict[str, List[float]]:
        """
        Get locations of all pins on a symbol instance

        Args:
            schematic_path: Path to .kicad_sch file
            symbol_reference: Symbol reference designator (e.g., "R1", "U1")

        Returns:
            Dictionary mapping pin number -> [x, y] coordinates
        """
        try:
            # Load schematic (shared, signature-keyed cache)
            sch = self._load_skip_schematic(schematic_path)

            target_symbol = None
            for symbol in sch.symbol:
                if symbol.property.Reference.value.rstrip("_") == symbol_reference:
                    target_symbol = symbol
                    break

            if not target_symbol:
                logger.error(f"Symbol {symbol_reference} not found")
                return {}

            lib_id = target_symbol.lib_id.value if hasattr(target_symbol, "lib_id") else None
            if not lib_id:
                logger.error(f"Symbol {symbol_reference} has no lib_id")
                return {}

            pins = self.get_symbol_pins(schematic_path, lib_id)
            if not pins:
                return {}

            result = {}
            for pin_num in pins.keys():
                location = self.get_pin_location(schematic_path, symbol_reference, pin_num)
                if location:
                    result[pin_num] = location

            logger.debug(f"Located {len(result)} pins on {symbol_reference}")
            return result

        except (OSError, AttributeError, KeyError, ValueError, TypeError) as e:
            # API boundary catch.  Was `except Exception` without a traceback —
            # several past fix PRs were debugging issues whose stacks ended at
            # this swallow.  Use logger.exception so the trace reaches the log.
            logger.exception(f"Error getting all symbol pins for {symbol_reference}: {e}")
            return {}

    def get_unit_placement(
        self, schematic_path: Path, symbol_reference: str
    ) -> Optional[Dict[str, Any]]:
        """Report the unit situation for a (possibly multi-unit) placed symbol.

        A multi-unit part (op-amp, MCU split into GPIO + power banks, …) is
        placed as one ``(symbol …)`` per unit, all sharing the reference. Pins
        of a unit that is NOT on the sheet have no real location — locating them
        fabricates coordinates (the F1 bug). Callers use this to warn about, and
        refuse to target, unplaced units.

        Returns, or None when the reference can't be read::

            {
              "lib_id": str | None,
              "defined_units": [int, ...],   # numbered units the symbol defines
              "total_units": int,            # len(defined_units), min 1
              "placed_units": [int, ...],    # numbered units with an instance
              "unplaced_units": [int, ...],  # defined but not placed
              "is_multi_unit": bool,
            }
        """
        from commands.wire_dragger import WireDragger

        try:
            sexp = self._load_sexp(schematic_path)
        except (OSError, ValueError) as e:
            logger.debug(f"get_unit_placement: failed to parse {schematic_path}: {e}")
            return None

        instances = WireDragger.find_symbol_instances(sexp, symbol_reference)
        if not instances:
            return None

        lib_id = next((inst[4] for inst in instances if inst[4]), None)
        placed_units: List[int] = sorted(
            {int(inst[7]) for inst in instances if inst[7] not in (None, 0)}
        )

        defined_units: List[int] = []
        if lib_id:
            pins = self.get_symbol_pins(schematic_path, lib_id)
            defined_units = sorted(
                {int(p["unit"]) for p in pins.values() if p.get("unit") not in (None, 0)}
            )
        # A symbol whose pins are all common (unit 0) or that we couldn't read
        # is effectively single-unit — normalise to [1] so callers see one unit.
        if not defined_units:
            defined_units = placed_units or [1]

        unplaced_units = [u for u in defined_units if u not in placed_units]
        return {
            "lib_id": lib_id,
            "defined_units": defined_units,
            "total_units": len(defined_units),
            "placed_units": placed_units,
            "unplaced_units": unplaced_units,
            "is_multi_unit": len(defined_units) > 1,
        }

    def diagnose_missing_pin(
        self, schematic_path: Path, symbol_reference: str, pin_number: str
    ) -> Dict[str, Any]:
        """Explain why ``get_pin_location`` could not place a pin.

        Returns a dict with ``reason`` one of:
          * ``"no_symbol"``    — the reference isn't placed at all.
          * ``"unplaced_unit"`` — the pin exists but its multi-unit part's unit
            is not on the sheet (the F1 phantom-pin case). Carries ``pin_unit``,
            ``resolved_pin``, ``lib_id`` and the unit lists so the caller can
            tell the user exactly which ``add_schematic_component(..., unit=N)``
            call fixes it.
          * ``"not_found"``    — the pin number/name isn't in the symbol at all.
        """
        info = self.get_unit_placement(schematic_path, symbol_reference)
        if info is None:
            return {"reason": "no_symbol"}

        lib_id = info.get("lib_id")
        pins = self.get_symbol_pins(schematic_path, lib_id) if lib_id else {}
        # Valid pin numbers/names carried on every "not_found" diagnosis so
        # callers can tell the user which pins DO exist (S10).
        valid_pins = list(pins.keys())
        valid_pin_names = [str(d.get("name", "")) for d in pins.values()]
        pin_key = str(pin_number)
        if pin_key not in pins:
            matched = next(
                (num for num, d in pins.items() if d.get("name") == pin_key),
                None,
            )
            if matched is None:
                return {
                    "reason": "not_found",
                    "valid_pins": valid_pins,
                    "valid_pin_names": valid_pin_names,
                    **info,
                }
            pin_key = matched

        pin_unit = pins[pin_key].get("unit")
        if pin_unit not in (None, 0) and pin_unit in info.get("unplaced_units", []):
            return {
                "reason": "unplaced_unit",
                "pin_unit": pin_unit,
                "resolved_pin": pin_key,
                **info,
            }
        return {
            "reason": "not_found",
            "resolved_pin": pin_key,
            "valid_pins": valid_pins,
            "valid_pin_names": valid_pin_names,
            **info,
        }

    @staticmethod
    def format_unplaced_unit_error(symbol_reference: str, diag: Dict[str, Any]) -> str:
        """Build the user-facing refusal message for a pin on an unplaced unit."""
        unit = diag.get("pin_unit")
        pin = diag.get("resolved_pin")
        unplaced = diag.get("unplaced_units", [])
        symbol_arg = diag.get("lib_id") or "<library>:<name>"
        return (
            f"Pin {pin} of {symbol_reference} belongs to unit {unit}, a unit of a "
            f"multi-unit symbol that is NOT placed on the sheet — it has no real "
            f"location, so it cannot be labeled or connected (labeling it would "
            f"land in empty space). Place the missing unit first with: "
            f'add_schematic_component(symbol="{symbol_arg}", '
            f'reference="{symbol_reference}", unit={unit})  '
            f"(unplaced unit(s): {unplaced}). Then retry this call."
        )

    @staticmethod
    def format_missing_pin_error(symbol_reference: str, pin_name: str, diag: Dict[str, Any]) -> str:
        """Build a user-facing message that distinguishes a MISSING COMPONENT
        from a missing PIN (S10).

        ``diag`` is the output of :meth:`diagnose_missing_pin`. Callers should
        handle ``reason == "unplaced_unit"`` separately (via
        :meth:`format_unplaced_unit_error`) before calling this; it is covered
        here only as a fallback.
        """
        reason = diag.get("reason")
        if reason == "no_symbol":
            return (
                f"Component {symbol_reference} not found in schematic "
                f"(no placed symbol has that reference)."
            )
        if reason == "not_found":
            hint = ""
            nums = [str(n) for n in (diag.get("valid_pins") or []) if str(n) != ""]
            names = [n for n in (diag.get("valid_pin_names") or []) if n and n not in ("~", "")]
            if nums:
                shown = ", ".join(nums[:12])
                more = "" if len(nums) <= 12 else f", … (+{len(nums) - 12} more)"
                hint += f" Valid pin numbers: {shown}{more}."
            if names:
                uniq = list(dict.fromkeys(names))
                shown_n = ", ".join(uniq[:8])
                hint += f" Pin names: {shown_n}{'' if len(uniq) <= 8 else ', …'}."
            return f"Component {symbol_reference} exists but has no pin '{pin_name}'.{hint}"
        if reason == "unplaced_unit":
            return PinLocator.format_unplaced_unit_error(symbol_reference, diag)
        return f"Could not locate pin {pin_name} on {symbol_reference}."


if __name__ == "__main__":
    import shutil
    import sys
    from pathlib import Path

    from commands.component_schematic import ComponentManager
    from commands.schematic import SchematicManager

    sys.path.insert(0, str(Path(__file__).parent.parent))

    print("=" * 80)
    print("PIN LOCATOR TEST")
    print("=" * 80)

    # Create test schematic with components (cross-platform temp directory)
    test_path = Path(tempfile.gettempdir()) / "test_pin_locator.kicad_sch"
    template_path = Path(__file__).parent.parent / "templates" / "template_with_symbols.kicad_sch"

    shutil.copy(template_path, test_path)
    print(f"\n✓ Created test schematic: {test_path}")

    # Add some components
    print("\n[1/4] Adding test components...")
    sch = SchematicManager.load_schematic(str(test_path))

    r1_def = {
        "type": "R",
        "reference": "R1",
        "value": "10k",
        "x": 100,
        "y": 100,
        "rotation": 0,
    }
    ComponentManager.add_component(sch, r1_def, test_path)

    c1_def = {
        "type": "C",
        "reference": "C1",
        "value": "100nF",
        "x": 150,
        "y": 100,
        "rotation": 90,
    }
    ComponentManager.add_component(sch, c1_def, test_path)

    SchematicManager.save_schematic(sch, str(test_path))
    print("  ✓ Added R1 and C1")

    print("\n[2/4] Testing pin location discovery...")
    locator = PinLocator()

    r1_pin1 = locator.get_pin_location(test_path, "R1", "1")
    r1_pin2 = locator.get_pin_location(test_path, "R1", "2")

    print(f"  R1 pin 1: {r1_pin1}")
    print(f"  R1 pin 2: {r1_pin2}")

    # Find C1 pins (rotated 90 degrees)
    c1_pin1 = locator.get_pin_location(test_path, "C1", "1")
    c1_pin2 = locator.get_pin_location(test_path, "C1", "2")

    print(f"  C1 pin 1: {c1_pin1}")
    print(f"  C1 pin 2: {c1_pin2}")

    print("\n[3/4] Testing get all pins...")
    r1_all_pins = locator.get_all_symbol_pins(test_path, "R1")
    print(f"  R1 all pins: {r1_all_pins}")

    c1_all_pins = locator.get_all_symbol_pins(test_path, "C1")
    print(f"  C1 all pins: {c1_all_pins}")

    print("\n[4/4] Verification...")
    success = True

    if not r1_pin1 or not r1_pin2:
        print("  ✗ Failed to locate R1 pins")
        success = False
    else:
        print("  ✓ R1 pins located")

    if not c1_pin1 or not c1_pin2:
        print("  ✗ Failed to locate C1 pins")
        success = False
    else:
        print("  ✓ C1 pins located")

    # Check rotation (C1 pins should be rotated 90 degrees from R1)
    if r1_pin1 and c1_pin1:
        # R1 is not rotated, pins should be at y offset from symbol center
        # C1 is rotated 90°, pins should be at x offset from symbol center
        print(f"\n  Pin offset analysis:")
        print(f"    R1 (0°):  pin 1 y-offset = {r1_pin1[1] - 100}")
        print(f"    C1 (90°): pin 1 x-offset = {c1_pin1[0] - 150}")

    print("\n" + "=" * 80)
    if success:
        print("✅ PIN LOCATOR TEST PASSED!")
    else:
        print("❌ PIN LOCATOR TEST FAILED!")
    print("=" * 80)
    print(f"\nTest schematic saved: {test_path}")
