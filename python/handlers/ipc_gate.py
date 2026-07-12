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

    Routes through ``KiCADInterface.require_ipc_board_op``, which returns one
    of:
    - ``{}`` → ready, caller falls through.
    - a structured refusal WITHOUT ``_ipc_reason`` — the editor-frame gate
      (``needs_pcb_editor: True``) or the cross-backend conflict gate
      (``needs_reconcile: True`` + ``direction``).  These carry their own
      actionable payload (which remedy to run, which reconcile direction), so
      they are passed through verbatim to reach the agent intact.
    - ``{"success": False, "_ipc_reason": <raw reason>}`` — the *only* shape we
      rewrap, in the caller's domain envelope, so the message reads cleanly
      instead of nesting two "IPC backend not available" prefixes.

    Regression context (finding B2): this wrapper used to special-case only
    ``needs_pcb_editor`` and funnel *everything else* — including a
    ``needs_reconcile`` cross-backend conflict — through ``ipc_unavailable``.
    That discarded ``needs_reconcile`` / ``direction`` and told the user to
    "Launch KiCAD / enable IPC" while IPC was already connected, hiding the
    real remedy (``reconcile_backends``).  Keying the rewrap on the presence of
    ``_ipc_reason`` (the raw-reason envelope) instead means every structured
    refusal shape — current or future — is forwarded untouched.
    """
    gate = iface.require_ipc_board_op(allow_launch=True)
    if not gate:
        return {}
    if "_ipc_reason" in gate:
        return ipc_unavailable(gate.get("_ipc_reason", ""))
    return gate
