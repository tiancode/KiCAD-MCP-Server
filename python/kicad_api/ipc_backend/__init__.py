"""IPC backend package (kipy-based KiCAD 9/10 client).

Re-exports the public surface so existing
``from kicad_api.ipc_backend import ...`` imports keep working after the
split from one module into this package.
"""

from ._backend import IPCBackend
from ._board_core import IPCBoardAPI
from ._helpers import (
    get_open_documents_compat,
    has_open_pcb_document,
)

__all__ = ["IPCBackend", "IPCBoardAPI", "get_open_documents_compat", "has_open_pcb_document"]
