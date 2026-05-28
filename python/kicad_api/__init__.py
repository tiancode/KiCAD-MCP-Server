"""
KiCAD API layer.

The live backend is the IPC implementation (kipy). Import it directly:

    from kicad_api.ipc_backend import IPCBackend, IPCBoardAPI

The SWIG path is NOT a backend object — it is direct ``pcbnew`` access behind
``KiCADInterface.command_routes`` in ``kicad_interface.py``. There is no
runtime backend factory; selection happens in ``kicad_interface.py`` at import
time based on whether KiCAD is reachable over IPC.
"""

from kicad_api.base import BoardAPI, KiCADBackend

__all__ = ["KiCADBackend", "BoardAPI"]
__version__ = "2.0.0-alpha.1"
