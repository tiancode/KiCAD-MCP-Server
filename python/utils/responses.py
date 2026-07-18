"""Shared builders for the ``{"success": False, ...}`` failure dicts.

Every command/handler returns the same failure shape; these constructors
replace the hand-built literals so the message/errorDetails wording — which
``utils.failure.classify_failure`` string-matches to stamp ``errorCode`` and
``hint`` — stays byte-identical across all call sites.
"""

from typing import Any, Dict

from utils.units import InvalidUnitError


def failed(message: str, details: Any) -> Dict[str, Any]:
    """The standard failure dict: ``details`` is stringified into errorDetails.

    A bad ``unit`` is a user-input mistake, not a server fault: when a handler
    forwards an :class:`InvalidUnitError` (raised by ``unit_to_nm_scale``) as
    ``details``, return a truthful ``VALIDATION`` refusal that names the valid
    units instead of a generic INTERNAL_ERROR wrapping a traceback.  Every
    per-command handler funnels its parse failures through ``failed(msg, e)``,
    so centralising here converts the unit refusal for every call site at once.
    """
    if isinstance(details, InvalidUnitError):
        return {
            "success": False,
            "message": str(details),
            "errorDetails": ("Valid units are 'mm', 'mil', 'inch' (omit for the default 'mm')."),
            "errorCode": "VALIDATION",
        }
    return {
        "success": False,
        "message": message,
        "errorDetails": str(details),
    }


def no_board_loaded() -> Dict[str, Any]:
    """The canonical "no board" refusal (classified NO_PROJECT_LOADED downstream)."""
    return failed("No board is loaded", "Load or create a board first")
