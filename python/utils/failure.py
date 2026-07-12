"""Central failure classification for KiCAD MCP command results.

Extracted from kicad_interface.py: pure functions with no KiCAD/pcbnew
dependency, so they can be unit-tested standalone. KiCADInterface.handle_command
routes every handler result through enrich_failure to stamp a stable
``errorCode`` and actionable ``hint``.
"""

from typing import Any, Dict, Optional, Tuple

# ----- Failure classification -----
# Every handler failure ultimately flows back through KiCADInterface.handle_command.
# Rather than rewrite the ~165 handlers that return a bare
# ``{"success": False, "message": str(e)}``, we classify the failure centrally
# and attach a stable ``errorCode`` plus an actionable ``hint`` the agent can
# act on (e.g. "call open_project first") instead of surfacing a raw traceback.
# Codes are intentionally coarse and stable — they are part of the tool's
# observable contract.


def classify_failure(
    message: str = "",
    details: str = "",
    exc: "Optional[BaseException]" = None,
) -> "Tuple[str, Optional[str]]":
    """Map a failure to ``(errorCode, hint)``.

    ``hint`` is ``None`` when no better next step than "inspect errorDetails"
    exists, so callers should only attach it when truthy.
    """
    text = f"{message}\n{details}".lower()

    # Exception-type signals are more reliable than string matching.
    if isinstance(exc, KeyError):
        missing = str(exc).strip("'\"")
        return (
            "INVALID_PARAMS",
            f"Missing required parameter: {missing}. Check the tool's input schema.",
        )
    if isinstance(exc, (FileNotFoundError,)):
        return (
            "FILE_NOT_FOUND",
            "Check the path is absolute and the file exists. open_project needs the "
            "full path to a .kicad_pro / .kicad_pcb file.",
        )
    if isinstance(exc, (TypeError, ValueError)) and "param" in text:
        return (
            "INVALID_PARAMS",
            "A parameter has the wrong type or value; check the input schema.",
        )

    # No project / board loaded — the single most common recoverable failure.
    # Match the specific phrasings handlers actually emit (dominant: "No board
    # is loaded", 57 call sites) rather than bare "no board" / "no project",
    # which false-positive on benign text like "...no board errors". A
    # dehydrated SWIG board surfaces as AttributeError on a NoneType board.
    if (
        "no board is loaded" in text
        or "no board loaded" in text
        or "board is none" in text
        or "no current project" in text
        or "open a project" in text
        or "no project is loaded" in text
        or "no kicad project is loaded" in text
        or ("nonetype" in text and "board" in text)
    ):
        return (
            "NO_PROJECT_LOADED",
            "No KiCAD project is loaded. Call open_project (or create_project) first, then retry.",
        )

    # The PCB editor gate already carries needs_pcb_editor; give it a code too.
    if "requires the pcb editor" in text or "needs_pcb_editor" in text:
        return (
            "PCB_EDITOR_REQUIRED",
            "Ask the user to open the board in KiCAD's PCB editor, then retry.",
        )

    if "no such file" in text or ("not found" in text and (".kicad" in text or "file" in text)):
        return (
            "FILE_NOT_FOUND",
            "Check the path is absolute and the file exists.",
        )

    # Component/footprint/symbol lookups.
    if ("not found" in text or "does not exist" in text or "no component" in text) and (
        "component" in text or "reference" in text or "footprint" in text or "symbol" in text
    ):
        return (
            "NOT_FOUND",
            "Verify the reference/name with a list_* or find_* tool before operating on it.",
        )

    return ("INTERNAL_ERROR", None)


def _hint_for_reconcile(result: "Dict[str, Any]") -> str:
    """Hint naming the exact reconcile_backends call for a cross-backend conflict."""
    direction = result.get("direction")
    call = f'reconcile_backends(direction="{direction}")' if direction else "reconcile_backends"
    return (
        "Cross-backend conflict: the other backend has unflushed writes. "
        f"Call {call} to sync the backends, then retry."
    )


# Structured refusal flags a handler may stamp on a ``success: False`` dict.
# Each maps to a stable, truthful ``errorCode`` and a builder for a default
# ``hint`` (some read sibling fields like ``direction``). These are known,
# pre-classified recoverable states, so enrich_failure applies them BEFORE the
# generic string-matching classifier — a needs_* refusal must never be
# mislabeled ``INTERNAL_ERROR`` (the bug that motivated NEEDS_ZONE_FILL). The
# first flag present on the result wins; flags are mutually exclusive in
# practice (each gate sets exactly one).
_NEEDS_FLAGS: "Tuple[Tuple[str, str, Any], ...]" = (
    (
        "needs_pcb_editor",
        "PCB_EDITOR_REQUIRED",
        lambda r: "Ask the user to open the board in KiCAD's PCB editor, then retry.",
    ),
    (
        "needs_reconcile",
        "NEEDS_RECONCILE",
        _hint_for_reconcile,
    ),
    (
        "needs_manual_action",
        "MANUAL_ACTION_REQUIRED",
        lambda r: (
            "This can't be resolved automatically; follow the 'steps' in the "
            "response, then retry."
        ),
    ),
    (
        "needs_unit_placement",
        "NEEDS_UNIT_PLACEMENT",
        lambda r: (
            "Place the required symbol unit first (see the suggested "
            "add_schematic_component call in the response), then retry."
        ),
    ),
    (
        "needs_zone_fill",
        "NEEDS_ZONE_FILL",
        lambda r: (
            "Fill the copper zone(s) first: call "
            "copper_pour(action=refill, force=true), then retry."
        ),
    ),
)


def enrich_failure(command: str, result: "Dict[str, Any]") -> "Dict[str, Any]":
    """Attach errorCode/hint to a handler's failure dict if it lacks them.

    Only touches dicts whose ``success`` is explicitly ``False``; success
    payloads and non-dicts pass through untouched.
    """
    if not isinstance(result, dict) or result.get("success") is not False:
        return result
    if result.get("errorCode"):
        return result

    # Copy before stamping so a handler that returns a shared/cached failure
    # constant isn't permanently mutated (which would then trip the
    # errorCode-present guard above on the next, unrelated failure).
    result = dict(result)

    # Structured refusal flags are truthful, pre-classified recoverable states —
    # prefer them over the generic string classifier so none is mislabeled
    # INTERNAL_ERROR.
    for flag, code, hint_fn in _NEEDS_FLAGS:
        if result.get(flag):
            result["errorCode"] = code
            if not result.get("hint"):
                result["hint"] = hint_fn(result)
            return result

    message = str(result.get("message") or "")
    details = str(result.get("errorDetails") or result.get("errorDetail") or "")
    code, hint = classify_failure(message, details)
    result["errorCode"] = code
    if hint and not result.get("hint"):
        result["hint"] = hint
    return result
