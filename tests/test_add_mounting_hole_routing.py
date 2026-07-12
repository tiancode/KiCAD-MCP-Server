"""B9(c): honest routing for add_mounting_hole.

``add_mounting_hole`` has no native IPC fast-path — the IPC handler
(``handlers/ipc_fastpath/_board.handle_add_mounting_hole``) delegates to the
SWIG implementation.  Because the command still sits in
``IPC_CAPABLE_COMMANDS``, the dispatcher applies the wrong-direction
``attempting="ipc"`` cross-backend pre-gate before that SWIG delegation runs
(``swig_fallback_mutation`` then applies the *correct* ``attempting="swig"``
gate).  The honest fix is to drop it from ``IPC_CAPABLE_COMMANDS`` so the
dispatcher routes it down the SWIG branch with the right gate.

These tests pin the fast-path-is-a-stub behavior (green today) and assert the
membership invariant (xfail until the one-line kicad_interface.py removal —
owned by another agent — lands; see fix-component-consistency.md).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def test_mounting_hole_fastpath_delegates_to_swig():
    """The IPC fast-path runs the SWIG add_mounting_hole (no native IPC path)."""
    from handlers.ipc_fastpath import handle_add_mounting_hole

    iface = MagicMock()
    iface._cross_backend_conflict.return_value = None
    iface.board_commands.add_mounting_hole.return_value = {
        "success": True,
        "mountingHole": {},
    }
    iface._auto_save_board.return_value = {"saved": False}

    params = {"position": {"x": 5, "y": 5, "unit": "mm"}, "diameter": 3.2}
    out = handle_add_mounting_hole(iface, params)

    assert out["success"] is True
    # It ran the SWIG implementation with the caller's params unchanged.
    iface.board_commands.add_mounting_hole.assert_called_once_with(params)


def test_add_mounting_hole_not_in_ipc_capable_commands():
    # add_mounting_hole has no native IPC fast-path (its handler delegates to
    # SWIG); advertising IPC capability misrouted the cross-backend gate (B9c).
    from kicad_interface import KiCADInterface

    assert "add_mounting_hole" not in KiCADInterface.IPC_CAPABLE_COMMANDS
