"""Shared builders for the ``{"success": False, ...}`` failure dicts.

Every command/handler returns the same failure shape; these constructors
replace the hand-built literals so the message/errorDetails wording — which
``utils.failure.classify_failure`` string-matches to stamp ``errorCode`` and
``hint`` — stays byte-identical across all call sites.
"""

from typing import Any, Dict


def failed(message: str, details: Any) -> Dict[str, Any]:
    """The standard failure dict: ``details`` is stringified into errorDetails."""
    return {
        "success": False,
        "message": message,
        "errorDetails": str(details),
    }


def no_board_loaded() -> Dict[str, Any]:
    """The canonical "no board" refusal (classified NO_PROJECT_LOADED downstream)."""
    return failed("No board is loaded", "Load or create a board first")
