"""Tests for ``delete_no_connect`` and ``edit_schematic_net_label``.

Two tooling gaps closed:

1. ``delete_no_connect`` — remove a misplaced no-connect (X) flag without
   deleting the whole component.  It is the inverse of ``add_no_connect``;
   a flag carries no name, so it is matched by position.
2. ``edit_schematic_net_label`` — change a label's type (local <-> global
   <-> hierarchical) and/or rename it *in place*, keeping the same uuid and
   position.  Fixing a page-local net mistakenly created as a global label
   then needs no wire/junction rework — the inverse of delete + re-add.
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import sexpdata

# Stub heavy / optional deps before loading wire_manager standalone.
for _modname in ("pcbnew", "skip"):
    sys.modules.setdefault(_modname, MagicMock())

PYTHON_DIR = Path(__file__).parent.parent / "python"
sys.path.insert(0, str(PYTHON_DIR))

from commands.wire_manager import (  # noqa: E402
    WireManager,
    _validate_schematic_sexpr,
)

_EMPTY_SCH = """\
(kicad_sch (version 20250114) (generator "KiCAD-MCP-Server")
  (lib_symbols)
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""

_LABEL_TYPES = ("label", "global_label", "hierarchical_label")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _top_level(text):
    tree = sexpdata.loads(text)
    return [item for item in tree[1:] if isinstance(item, list) and item]


def _names(text):
    return [str(item[0]) for item in _top_level(text)]


def _find_label(text, name, at=None, tol=0.5):
    for item in _top_level(text):
        if str(item[0]) not in _LABEL_TYPES or len(item) < 2 or item[1] != name:
            continue
        if at is not None:
            at_sub = _sub(item, "at")
            if at_sub is None:
                continue
            if not (abs(float(at_sub[1]) - at[0]) < tol and abs(float(at_sub[2]) - at[1]) < tol):
                continue
        return item
    return None


def _sub(item, tag):
    for p in item[1:]:
        if isinstance(p, list) and p and str(p[0]) == tag:
            return p
    return None


# ===========================================================================
# delete_no_connect
# ===========================================================================
@pytest.mark.unit
class TestDeleteNoConnect:
    def _sch_with_nc(self, tmp_path, positions):
        sch = tmp_path / "nc.kicad_sch"
        sch.write_text(_EMPTY_SCH)
        for pos in positions:
            assert WireManager.add_no_connect(sch, pos) is True
        return sch

    def test_removes_matching_flag(self, tmp_path):
        sch = self._sch_with_nc(tmp_path, [[50.0, 60.0]])
        assert "no_connect" in _names(sch.read_text())

        assert WireManager.delete_no_connect(sch, [50.0, 60.0]) is True

        out = sch.read_text()
        assert "no_connect" not in _names(out)
        _validate_schematic_sexpr(out)  # still loadable

    def test_no_match_returns_false_and_keeps_flag(self, tmp_path):
        sch = self._sch_with_nc(tmp_path, [[50.0, 60.0]])
        before = sch.read_text()

        assert WireManager.delete_no_connect(sch, [99.0, 99.0]) is False

        # The flag is untouched (the file is not rewritten on a miss).
        assert sch.read_text() == before
        assert _names(sch.read_text()).count("no_connect") == 1

    def test_within_tolerance_matches(self, tmp_path):
        sch = self._sch_with_nc(tmp_path, [[50.0, 60.0]])
        # 0.3 mm off, default tolerance 0.5 mm -> match
        assert WireManager.delete_no_connect(sch, [50.3, 60.0]) is True
        assert "no_connect" not in _names(sch.read_text())

    def test_outside_tolerance_does_not_match(self, tmp_path):
        sch = self._sch_with_nc(tmp_path, [[50.0, 60.0]])
        # 1 mm off, default tolerance 0.5 mm -> no match
        assert WireManager.delete_no_connect(sch, [51.0, 61.0]) is False
        assert "no_connect" in _names(sch.read_text())

    def test_deletes_only_one_of_many(self, tmp_path):
        sch = self._sch_with_nc(tmp_path, [[50.0, 60.0], [80.0, 90.0]])
        assert _names(sch.read_text()).count("no_connect") == 2

        assert WireManager.delete_no_connect(sch, [50.0, 60.0]) is True

        out = sch.read_text()
        # One removed, the other survives.
        assert _names(out).count("no_connect") == 1
        # The survivor is the [80, 90] flag.
        nc = next(i for i in _top_level(out) if str(i[0]) == "no_connect")
        at = _sub(nc, "at")
        assert (float(at[1]), float(at[2])) == (80.0, 90.0)


# ===========================================================================
# edit_label (the WireManager core)
# ===========================================================================
@pytest.mark.unit
class TestEditLabel:
    def _sch_with_label(self, tmp_path, name="NET1", at=(50.0, 60.0), label_type="label"):
        sch = tmp_path / "lbl.kicad_sch"
        sch.write_text(_EMPTY_SCH)
        assert WireManager.add_label(sch, name, list(at), label_type=label_type) is True
        return sch

    def test_local_to_global_preserves_uuid_and_position(self, tmp_path):
        sch = self._sch_with_label(tmp_path)
        before = _find_label(sch.read_text(), "NET1")
        uuid_before = _sub(before, "uuid")[1]
        at_before = _sub(before, "at")

        result = WireManager.edit_label(sch, "NET1", new_type="global_label")

        assert result is not None
        assert result["old_type"] == "label"
        assert result["new_type"] == "global_label"

        out = sch.read_text()
        assert "global_label" in _names(out)
        assert "label" not in _names(out)  # the local label is gone
        _validate_schematic_sexpr(out)

        after = _find_label(out, "NET1")
        # uuid + position survive the retype
        assert _sub(after, "uuid")[1] == uuid_before
        assert _sub(after, "at")[1:] == at_before[1:]
        # global labels carry a shape
        assert _sub(after, "shape") is not None

    def test_round_trip_global_to_local_drops_shape(self, tmp_path):
        sch = self._sch_with_label(tmp_path, name="RT")
        uuid0 = _sub(_find_label(sch.read_text(), "RT"), "uuid")[1]

        WireManager.edit_label(sch, "RT", new_type="global_label")
        glob = _find_label(sch.read_text(), "RT")
        assert str(glob[0]) == "global_label"
        assert _sub(glob, "shape") is not None

        WireManager.edit_label(sch, "RT", new_type="label")
        loc = _find_label(sch.read_text(), "RT")
        assert str(loc[0]) == "label"
        # local labels have no shape — it must be dropped on the way back
        assert _sub(loc, "shape") is None
        # uuid is stable across both conversions
        assert _sub(loc, "uuid")[1] == uuid0
        _validate_schematic_sexpr(sch.read_text())

    def test_rename_only_keeps_type(self, tmp_path):
        sch = self._sch_with_label(tmp_path, name="OLD")

        result = WireManager.edit_label(sch, "OLD", new_name="NEW")

        assert result is not None
        assert result["old_name"] == "OLD"
        assert result["new_name"] == "NEW"
        out = sch.read_text()
        assert _find_label(out, "OLD") is None
        renamed = _find_label(out, "NEW")
        assert renamed is not None
        assert str(renamed[0]) == "label"  # type unchanged

    def test_type_alias_accepted(self, tmp_path):
        sch = self._sch_with_label(tmp_path)
        result = WireManager.edit_label(sch, "NET1", new_type="global")  # alias
        assert result["new_type"] == "global_label"
        assert "global_label" in _names(sch.read_text())

    def test_not_found_returns_none_and_leaves_file_intact(self, tmp_path):
        sch = self._sch_with_label(tmp_path, name="NET1")
        before = sch.read_text()

        assert WireManager.edit_label(sch, "NOPE", new_type="global_label") is None
        assert sch.read_text() == before

    def test_unknown_type_raises_before_io(self, tmp_path):
        sch = self._sch_with_label(tmp_path)
        before = sch.read_text()

        with pytest.raises(ValueError):
            WireManager.edit_label(sch, "NET1", new_type="banana")

        # Type rejected before any read-modify-write — file untouched.
        assert sch.read_text() == before

    def test_disambiguate_by_position(self, tmp_path):
        sch = tmp_path / "dup.kicad_sch"
        sch.write_text(_EMPTY_SCH)
        WireManager.add_label(sch, "DUP", [50.0, 60.0], label_type="label")
        WireManager.add_label(sch, "DUP", [80.0, 90.0], label_type="label")

        # Edit only the one at (80, 90).
        result = WireManager.edit_label(sch, "DUP", new_type="global_label", position=[80.0, 90.0])
        assert result is not None
        assert result["position"] == {"x": 80.0, "y": 90.0}

        out = sch.read_text()
        at_50 = _find_label(out, "DUP", at=(50.0, 60.0))
        at_80 = _find_label(out, "DUP", at=(80.0, 90.0))
        assert str(at_50[0]) == "label"  # untouched
        assert str(at_80[0]) == "global_label"  # converted


# ===========================================================================
# Handler-level guards (no real KiCAD needed)
# ===========================================================================
@pytest.mark.unit
class TestHandlers:
    def _sch_with_label(self, tmp_path, name="NET1", at=(50.0, 60.0)):
        sch = tmp_path / "h.kicad_sch"
        sch.write_text(_EMPTY_SCH)
        assert WireManager.add_label(sch, name, list(at), label_type="label") is True
        return sch

    def test_edit_handler_requires_a_change(self):
        from handlers.schematic_wire import handle_edit_schematic_net_label

        # Neither newLabelType nor newName -> rejected before any file access.
        res = handle_edit_schematic_net_label(
            None, {"schematicPath": "/nonexistent.kicad_sch", "netName": "X"}
        )
        assert res["success"] is False
        assert "at least one" in res["message"].lower()

    def test_edit_handler_unknown_type_surfaces_message(self, tmp_path):
        from handlers.schematic_wire import handle_edit_schematic_net_label

        sch = self._sch_with_label(tmp_path)
        res = handle_edit_schematic_net_label(
            None, {"schematicPath": str(sch), "netName": "NET1", "newLabelType": "banana"}
        )
        assert res["success"] is False
        assert "unknown label type" in res["message"].lower()

    def test_edit_handler_end_to_end(self, tmp_path):
        from handlers.schematic_wire import handle_edit_schematic_net_label

        sch = self._sch_with_label(tmp_path)
        res = handle_edit_schematic_net_label(
            None, {"schematicPath": str(sch), "netName": "NET1", "newLabelType": "global_label"}
        )
        assert res["success"] is True
        assert res["new_type"] == "global_label"
        assert "global_label" in _names(sch.read_text())

    def test_delete_no_connect_handler_requires_target(self):
        from handlers.schematic_wire import handle_delete_no_connect

        res = handle_delete_no_connect(None, {"schematicPath": "/x.kicad_sch"})
        assert res["success"] is False
        assert "provide either position" in res["message"].lower()

    def test_delete_no_connect_handler_end_to_end(self, tmp_path):
        from handlers.schematic_wire import handle_delete_no_connect

        sch = tmp_path / "nc.kicad_sch"
        sch.write_text(_EMPTY_SCH)
        assert WireManager.add_no_connect(sch, [50.0, 60.0]) is True

        res = handle_delete_no_connect(None, {"schematicPath": str(sch), "position": [50.0, 60.0]})
        assert res["success"] is True
        assert "no_connect" not in _names(sch.read_text())
