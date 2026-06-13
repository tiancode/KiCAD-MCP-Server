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
    message = str(result.get("message") or "")
    details = str(result.get("errorDetails") or result.get("errorDetail") or "")
    # The PCB-editor gate is a known, pre-classified recoverable state.
    if result.get("needs_pcb_editor"):
        result["errorCode"] = "PCB_EDITOR_REQUIRED"
        result.setdefault(
            "hint", "Ask the user to open the board in KiCAD's PCB editor, then retry."
        )
        return result

    code, hint = classify_failure(message, details)
    result["errorCode"] = code
    if hint and not result.get("hint"):
        result["hint"] = hint
    return result
