"""Regression tests for the fatal "MCP writes an unloadable .kicad_sch" bug.

User report: a schematic built via the MCP tools (generator
"KiCAD-MCP-Server") could not be loaded by KiCad / ``kicad-cli sch erc``
("Failed to load schematic").  Bisecting the build showed the breaking
step was ``add_schematic_net_label`` with ``labelType: "global"``:
``WireManager.add_label`` did a bare ``Symbol(label_type)``, emitting an
invalid ``(global ...)`` element (KiCad's element is ``global_label``),
which makes the parser reject the WHOLE file even though the parens are
balanced.

Two defences are verified here:

1. ``_normalize_label_type`` maps friendly aliases ("global" ->
   "global_label") to the canonical element name and raises on anything
   unrecognised — so a near-miss becomes a valid label instead of silent
   corruption, and a typo surfaces as a clear error.
2. ``_validate_schematic_sexpr`` is a pre-write backstop: it rejects an
   unbalanced or non-``kicad_sch`` serialization BEFORE it can be
   written, so a corrupt edit never truncates the on-disk schematic.
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock

import pytest
import sexpdata
from sexpdata import Symbol

# Stub heavy / optional deps before loading wire_manager.
for modname in ("pcbnew", "skip"):
    sys.modules.setdefault(modname, MagicMock())

_wm_spec = importlib.util.spec_from_file_location(
    "wire_manager",
    os.path.join(os.path.dirname(__file__), "..", "python", "commands", "wire_manager.py"),
)
_wm_mod = importlib.util.module_from_spec(_wm_spec)
_wm_spec.loader.exec_module(_wm_mod)
WireManager = _wm_mod.WireManager
_normalize_label_type = _wm_mod._normalize_label_type
_validate_schematic_sexpr = _wm_mod._validate_schematic_sexpr


_EMPTY_SCH = """\
(kicad_sch (version 20250114) (generator "KiCAD-MCP-Server")
  (lib_symbols)
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""


def _top_level_element_names(text):
    """Element names of every top-level child of the (kicad_sch ...) root."""
    tree = sexpdata.loads(text)
    return [str(item[0]) for item in tree[1:] if isinstance(item, list) and item]


# ---------------------------------------------------------------------------
# _normalize_label_type
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "given,expected",
    [
        ("label", "label"),
        ("local", "label"),
        ("net", "label"),
        ("global", "global_label"),  # the bug trigger
        ("global_label", "global_label"),
        ("GLOBAL", "global_label"),  # case-insensitive
        (" hierarchical ", "hierarchical_label"),  # whitespace-tolerant
        ("hier", "hierarchical_label"),
        ("hierarchical_label", "hierarchical_label"),
    ],
)
def test_normalize_label_type_aliases(given, expected):
    assert _normalize_label_type(given) == expected


def test_normalize_label_type_rejects_unknown():
    with pytest.raises(ValueError) as exc:
        _normalize_label_type("banana")
    assert "Unknown label type" in str(exc.value)


# ---------------------------------------------------------------------------
# _validate_schematic_sexpr
# ---------------------------------------------------------------------------
def test_validate_accepts_well_formed():
    _validate_schematic_sexpr(_EMPTY_SCH)  # no raise


def test_validate_ignores_parens_inside_strings():
    # Parens inside quoted strings must not count toward the balance.
    text = '(kicad_sch (property "Desc" "Timer (e.g. NE555)") (paper "A4"))'
    _validate_schematic_sexpr(text)  # no raise


def test_validate_rejects_unclosed_paren():
    with pytest.raises(ValueError) as exc:
        _validate_schematic_sexpr('(kicad_sch (paper "A4")')
    assert "unclosed" in str(exc.value)


def test_validate_rejects_extra_close_paren():
    with pytest.raises(ValueError) as exc:
        _validate_schematic_sexpr('(kicad_sch (paper "A4")))')
    assert "unbalanced" in str(exc.value)


def test_validate_rejects_non_kicad_sch_root():
    with pytest.raises(ValueError) as exc:
        _validate_schematic_sexpr('(some_other_root (paper "A4"))')
    assert "kicad_sch" in str(exc.value)


# ---------------------------------------------------------------------------
# add_label end-to-end: the alias now produces a valid element
# ---------------------------------------------------------------------------
def test_add_label_global_alias_emits_valid_global_label(tmp_path):
    sch = tmp_path / "g.kicad_sch"
    sch.write_text(_EMPTY_SCH)

    ok = WireManager.add_label(sch, "GND", [50.0, 60.0], label_type="global", orientation=0)
    assert ok is True

    out = sch.read_text()
    names = _top_level_element_names(out)
    assert "global_label" in names, f"expected a global_label element, got {names}"
    # The invalid bare (global ...) element must NOT appear.
    assert "global" not in names, "an invalid bare (global ...) element was emitted"
    # And the whole file must still validate.
    _validate_schematic_sexpr(out)


def test_add_label_unknown_type_raises_and_leaves_file_intact(tmp_path):
    sch = tmp_path / "u.kicad_sch"
    sch.write_text(_EMPTY_SCH)

    with pytest.raises(ValueError):
        WireManager.add_label(sch, "X", [10.0, 10.0], label_type="banana")

    # The schematic on disk is untouched — the type was rejected before
    # any read-modify-write happened.
    assert sch.read_text() == _EMPTY_SCH


def test_add_label_canonical_types_all_load(tmp_path):
    for lt, element in [
        ("label", "label"),
        ("global_label", "global_label"),
        ("hierarchical_label", "hierarchical_label"),
    ]:
        sch = tmp_path / f"{lt}.kicad_sch"
        sch.write_text(_EMPTY_SCH)
        assert WireManager.add_label(sch, "N", [5.0, 5.0], label_type=lt) is True
        names = _top_level_element_names(sch.read_text())
        assert element in names
