"""
Transaction handlers (IPC-only).

Group a sequence of mutating MCP calls into a single KiCad undo step.
Without this, every place_component / route_trace / add_zone call ends
up as its own entry in the undo history — an AI agent that does a
five-step refactor leaves five Ctrl-Z steps for the human to walk
back through.  With ``begin_transaction`` open, those five calls
collapse into one.

Caveat: only mutations that go through the board's create_items /
update_items / remove_items path participate.  Property mutations like
``set_origin`` and ``set_title_block_info`` are sent as direct kipy
commands and do NOT join the transaction (kipy treats them as
out-of-band — they apply immediately and are individually undoable).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger(__name__)


def _ipc_unavailable() -> Dict[str, Any]:
    return {
        "success": False,
        "message": (
            "Transaction commands require the IPC backend. Launch KiCAD "
            "with Preferences > Plugins > Enable IPC API Server, then retry."
        ),
    }


def handle_begin_transaction(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Open a transaction. Refuses to nest — commit or rollback first.

    Forwards ``description`` as-is (including explicit empty string).
    The backend supplies the default label when the key is omitted /
    null; this keeps the "MCP Operation" default in one place.
    """
    if not iface.use_ipc or not iface.ipc_board_api:
        return _ipc_unavailable()
    description = params.get("description")
    if description is not None:
        description = str(description)
    return iface.ipc_board_api.begin_transaction(description)


def handle_commit_transaction(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Push the open transaction as one undo step.

    ``description`` overrides the label set at begin_transaction; omit it
    to keep the original label.
    """
    if not iface.use_ipc or not iface.ipc_board_api:
        return _ipc_unavailable()
    description = params.get("description")
    if description is not None:
        description = str(description)
    return iface.ipc_board_api.commit_transaction(description)


def handle_rollback_transaction(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Discard the open transaction — every change since begin is reverted."""
    if not iface.use_ipc or not iface.ipc_board_api:
        return _ipc_unavailable()
    return iface.ipc_board_api.rollback_transaction()


def handle_get_transaction_status(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Report whether a transaction is currently open."""
    if not iface.use_ipc or not iface.ipc_board_api:
        return _ipc_unavailable()
    return iface.ipc_board_api.get_transaction_status()
