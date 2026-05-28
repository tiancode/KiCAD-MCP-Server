"""Regression tests for the kipy-10 ``get_open_documents`` signature + the
IPC place_component footprint mapping.

End-to-end PCB flow failures these lock in:

* kipy 10's ``KiCad.get_open_documents(doc_type)`` REQUIRES the arg.  The
  old no-arg call raised ``TypeError``, which every "is a board open?"
  check swallowed — so the PCB-editor gate always claimed no board was
  open even with the editor open, blocking every IPC board op.
* The IPC ``place_component`` handler read ``footprint`` but the MCP
  schema's footprint-library field is ``componentId``; the footprint
  arrived empty and placement failed.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


# ---------------------------------------------------------------------------
# get_open_documents_compat: kipy-10 requires a doc_type argument
# ---------------------------------------------------------------------------
class _Kipy10:
    """Stand-in for kipy 10's KiCad: get_open_documents REQUIRES doc_type."""

    def __init__(self, docs):
        self._docs = docs

    def get_open_documents(self, doc_type):  # noqa: D401 — positional required
        return list(self._docs)


class _Kipy9:
    """Stand-in for kipy 9's KiCad: get_open_documents takes NO argument."""

    def __init__(self, docs):
        self._docs = docs

    def get_open_documents(self):
        return list(self._docs)


class _PcbDoc:
    def __init__(self, board_filename="board.kicad_pcb", project_path="/tmp/p"):
        self.board_filename = board_filename
        self.project = MagicMock(path=project_path)


def test_compat_calls_kipy10_with_doc_type():
    from kicad_api.ipc_backend import get_open_documents_compat

    doc = _PcbDoc()
    docs = get_open_documents_compat(_Kipy10([doc]), doc_type=3)
    assert docs == [doc]


def test_compat_falls_back_to_kipy9_no_arg():
    from kicad_api.ipc_backend import get_open_documents_compat

    doc = _PcbDoc()
    # Passing a doc_type to a kipy-9 object TypeErrors -> compat retries no-arg.
    docs = get_open_documents_compat(_Kipy9([doc]), doc_type=3)
    assert docs == [doc]


def test_has_open_pcb_document_true_for_open_board(real_kipy):
    from kicad_api.ipc_backend import has_open_pcb_document

    assert has_open_pcb_document(_Kipy10([_PcbDoc("example.kicad_pcb")])) is True


def test_has_open_pcb_document_false_when_no_pcb(real_kipy):
    from kicad_api.ipc_backend import has_open_pcb_document

    # A schematic-only doc must not satisfy a PCB-editor gate.
    sch = MagicMock(board_filename="x.kicad_sch", project=MagicMock(path="/tmp"))
    # No board_filename ending in .kicad_pcb, and type doesn't match PCB.
    sch.type = None
    assert has_open_pcb_document(_Kipy10([sch])) is False


def test_has_open_pcb_document_false_on_empty(real_kipy):
    from kicad_api.ipc_backend import has_open_pcb_document

    assert has_open_pcb_document(_Kipy10([])) is False


# ---------------------------------------------------------------------------
# IPC place_component: componentId is the footprint-library field
# ---------------------------------------------------------------------------
def test_ipc_place_component_uses_component_id_as_footprint():
    from handlers.ipc_fastpath import handle_place_component

    captured = {}

    def _place(reference, footprint, x, y, rotation, layer, value):
        captured.update(
            reference=reference, footprint=footprint, x=x, y=y, layer=layer, value=value
        )
        return True

    iface = MagicMock()
    iface.ipc_board_api._current_commit = None
    iface.ipc_board_api.place_component = _place

    out = handle_place_component(
        iface,
        {
            "componentId": "Resistor_SMD:R_0805_2012Metric",
            "reference": "R1",
            "value": "1k",
            "position": {"x": 120, "y": 120, "unit": "mm"},
        },
    )

    assert out["success"] is True
    # The library ID from componentId must reach the backend as the footprint.
    assert captured["footprint"] == "Resistor_SMD:R_0805_2012Metric"
    assert captured["reference"] == "R1"


def test_ipc_place_component_explicit_footprint_overrides_component_id():
    from handlers.ipc_fastpath import handle_place_component

    captured = {}
    iface = MagicMock()
    iface.ipc_board_api._current_commit = None
    iface.ipc_board_api.place_component = lambda **kw: captured.update(kw) or True

    handle_place_component(
        iface,
        {
            "componentId": "Lib:FromId",
            "footprint": "Lib:ExplicitOverride",
            "reference": "U1",
            "position": {"x": 1, "y": 2, "unit": "mm"},
        },
    )
    assert captured["footprint"] == "Lib:ExplicitOverride"


# ---------------------------------------------------------------------------
# IPC add_board_outline: rectangle drawn via IPC segments (not SWIG)
# ---------------------------------------------------------------------------
def test_ipc_add_board_outline_rectangle_builds_four_corner_points(monkeypatch):
    """A rectangle (width/height/x/y) must NOT delegate to the SWIG path
    (which has no board loaded in IPC mode); it should draw 4 Edge.Cuts
    segments over IPC."""
    import handlers.ipc_fastpath as fp

    iface = MagicMock()
    # If the code delegates to SWIG, this would be called — assert it isn't.
    iface.board_commands.add_board_outline = MagicMock(
        side_effect=AssertionError("must not delegate rectangle to SWIG")
    )

    # The IPC branch imports kipy types at call time; if kipy isn't present
    # the test still proves we didn't hit the SWIG delegate (the import
    # error is caught and returned as success:false, not the assertion).
    out = fp.handle_add_board_outline(
        iface,
        {
            "shape": "rectangle",
            "params": {"width": 80, "height": 60, "x": 100, "y": 100, "unit": "mm"},
        },
    )
    # Either it built segments (kipy present) or failed on the kipy import —
    # but it must never have delegated to the SWIG board.
    assert iface.board_commands.add_board_outline.call_count == 0
    assert isinstance(out, dict) and "success" in out
