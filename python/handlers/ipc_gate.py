"""Shared IPC + PCB-editor gating for board-mutating handler modules.

The board-mutating handler modules (board_meta, transactions, selection,
ipc, shapes) all gate their operations on the IPC backend plus an open PCB
editor frame using identical logic. ``require_ipc`` centralizes that gate so
the contract lives in one place; each module keeps its own category-specific
``_ipc_unavailable`` message and passes it in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Dict

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface


def require_ipc(
    iface: "KiCADInterface",
    ipc_unavailable: Callable[[str], Dict[str, Any]],
) -> Dict[str, Any]:
    """Gate a board op on IPC + an open PCB editor frame.

    Routes through ``KiCADInterface.require_ipc_board_op``:
    - ``{}`` → ready, caller falls through.
    - ``needs_pcb_editor: True`` → pass the editor-gate response through
      unchanged (so it reaches the agent).
    - else → wrap the raw ``_ipc_reason`` in the caller's domain envelope so
      the message reads cleanly instead of nesting two "IPC backend not
      available" prefixes.
    """
    gate = iface.require_ipc_board_op(allow_launch=True)
    if not gate:
        return {}
    if gate.get("needs_pcb_editor"):
        return gate
    return ipc_unavailable(gate.get("_ipc_reason", ""))
