"""Module-level Symbol constants and aliases for the wire-manager package.

Split out of the former monolithic commands/wire_manager.py.
"""

from sexpdata import Symbol

# Module-level Symbol constants — avoids repeated allocation on every call
_SYM_WIRE = Symbol("wire")
_SYM_PTS = Symbol("pts")
_SYM_XY = Symbol("xy")
_SYM_AT = Symbol("at")
_SYM_LABEL = Symbol("label")
_SYM_GLOBAL_LABEL = Symbol("global_label")
_SYM_HIERARCHICAL_LABEL = Symbol("hierarchical_label")
_SYM_STROKE = Symbol("stroke")
_SYM_WIDTH = Symbol("width")
_SYM_TYPE = Symbol("type")
_SYM_UUID = Symbol("uuid")
_SYM_SHEET_INSTANCES = Symbol("sheet_instances")
_SYM_JUNCTION = Symbol("junction")
_SYM_LIB_SYMBOLS = Symbol("lib_symbols")
_SYM_LIB_ID = Symbol("lib_id")
_SYM_MIRROR = Symbol("mirror")
_SYM_PIN = Symbol("pin")
_SYM_SYMBOL = Symbol("symbol")
_SYM_UNIT = Symbol("unit")
_SYM_KICAD_SCH = Symbol("kicad_sch")
_IU_PER_MM = 10000

# Friendly aliases → the canonical KiCad element name. The MCP schema advertises
# only the three canonical names, but direct Python calls bypass that check, and a
# bare ``Symbol(bad_type)`` emits e.g. ``(global ...)`` — an element KiCad rejects,
# breaking the WHOLE schematic. Normalising a near-miss like "global" avoids that.
_LABEL_TYPE_ALIASES = {
    "label": "label",
    "local": "label",
    "local_label": "label",
    "net": "label",
    "net_label": "label",
    "global": "global_label",
    "global_label": "global_label",
    "hier": "hierarchical_label",
    "hierarchical": "hierarchical_label",
    "hierarchical_label": "hierarchical_label",
    "sheet": "hierarchical_label",
}
