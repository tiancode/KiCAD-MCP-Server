"""place_component must refuse to run inside an open IPC transaction.

The IPC place_component path falls through to pcbnew SWIG when the
footprint is found in the library — that branch writes directly to the
.kicad_pcb file and then calls board.revert() to re-sync the IPC view.
That revert invalidates the open commit handle, *and* the placement is
already persisted to disk, so rollback_transaction can't undo it.  The
guard fails the call fast rather than silently break atomicity.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _make_iface(transaction_open: bool):
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = True
    iface.ipc_backend = MagicMock()
    iface.ipc_board_api = MagicMock()
    iface.ipc_board_api._current_commit = "fake-commit" if transaction_open else None
    iface.ipc_board_api.place_component = MagicMock(return_value=True)
    iface.board = None
    iface.command_routes = {}
    iface._board_disk_signature = None
    iface._current_project_path = None
    iface._last_auto_save_status = None
    return iface


def test_place_component_rejected_inside_open_transaction():
    """begin_transaction → place_component returns success:False with a
    message that points at the SWIG/disk-write mechanism."""
    iface = _make_iface(transaction_open=True)
    out = iface._ipc_place_component(
        {
            "reference": "U1",
            "footprint": "Library:LM7805",
            "x": 10,
            "y": 10,
        }
    )
    assert out["success"] is False
    assert "transaction" in out["message"].lower()
    # Backend not called — the guard short-circuits before reaching kipy.
    iface.ipc_board_api.place_component.assert_not_called()


def test_place_component_runs_normally_outside_transaction():
    """No transaction open → normal path forwards to the backend."""
    iface = _make_iface(transaction_open=False)
    out = iface._ipc_place_component(
        {
            "reference": "U1",
            "footprint": "Library:LM7805",
            "x": 10,
            "y": 10,
        }
    )
    assert out["success"] is True
    iface.ipc_board_api.place_component.assert_called_once()
