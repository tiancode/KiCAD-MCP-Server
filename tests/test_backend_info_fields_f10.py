"""F10 regression: get_backend_info must always expose the cross-backend
sync flags ``ipcWritesPending`` / ``swigWritesLanded``.

CLAUDE.md documents that ``get_backend_info`` surfaces these so callers can
pre-empt the ``needs_reconcile`` gate, but the response had drifted to omit
them.  These tests pin the response shape on BOTH the IPC and SWIG return
branches (the fields are set before the branch split, so both must carry them).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _make_iface(*, use_ipc: bool, ipc_pending: bool, swig_landed: bool):
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = use_ipc
    iface._ipc_writes_pending = ipc_pending
    iface._swig_writes_landed = swig_landed
    if use_ipc:
        backend = MagicMock()
        backend.is_connected.return_value = True
        backend.get_version.return_value = "10.0.4"
        iface.ipc_backend = backend
    else:
        iface.ipc_backend = None
    return iface


@pytest.fixture(autouse=True)
def _no_side_effect_attach(monkeypatch):
    """handle_get_backend_info force-attaches IPC and probes the process list;
    neutralise both so tests are hermetic."""
    from kicad_interface import KiCADInterface
    from utils.kicad_process import KiCADProcessManager

    monkeypatch.setattr(KiCADInterface, "_try_enable_ipc_backend", lambda self, **kw: False)
    monkeypatch.setattr(KiCADProcessManager, "is_running", lambda: False)


def test_get_backend_info_includes_flags_on_swig_branch():
    from handlers import ui as ui_handler

    iface = _make_iface(use_ipc=False, ipc_pending=False, swig_landed=True)

    out = ui_handler.handle_get_backend_info(iface, {})

    assert out["success"] is True
    assert out["backend"] == "swig"
    # The documented flags must be present, always, as booleans.
    assert out["ipcWritesPending"] is False
    assert out["swigWritesLanded"] is True


def test_get_backend_info_includes_flags_on_ipc_branch():
    from handlers import ui as ui_handler

    iface = _make_iface(use_ipc=True, ipc_pending=True, swig_landed=False)

    out = ui_handler.handle_get_backend_info(iface, {})

    assert out["success"] is True
    assert out["backend"] == "ipc"
    assert out["ipcWritesPending"] is True
    assert out["swigWritesLanded"] is False


def test_get_backend_info_flags_are_always_present_even_when_unset():
    """Both keys must exist regardless of value — callers key off their
    presence to pre-empt the gate."""
    from handlers import ui as ui_handler

    iface = _make_iface(use_ipc=False, ipc_pending=False, swig_landed=False)

    out = ui_handler.handle_get_backend_info(iface, {})

    assert "ipcWritesPending" in out
    assert "swigWritesLanded" in out
    assert out["ipcWritesPending"] is False
    assert out["swigWritesLanded"] is False


def test_get_backend_info_flags_coerced_to_bool_when_attr_missing(monkeypatch):
    """A __new__-built interface that never set the flags must still yield
    booleans (getattr default), not raise or emit None."""
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = False
    iface.ipc_backend = None
    # Deliberately do NOT set _ipc_writes_pending / _swig_writes_landed.

    from handlers import ui as ui_handler

    out = ui_handler.handle_get_backend_info(iface, {})

    assert out["ipcWritesPending"] is False
    assert out["swigWritesLanded"] is False
