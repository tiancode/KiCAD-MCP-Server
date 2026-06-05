"""Module-level helpers and constants for the IPC backend.

Split out of the former monolithic kicad_api/ipc_backend.py.
"""

import logging
from typing import Any, List

from kicad_api.base import APINotAvailableError, ConnectionError

logger = logging.getLogger("kicad_interface")


# Unit conversion constant: KiCAD IPC uses nanometers internally
MM_TO_NM = 1_000_000
INCH_TO_NM = 25_400_000


def get_open_documents_compat(kicad: Any, doc_type: Any = None) -> List[Any]:
    """Call ``KiCad.get_open_documents`` across kipy 9 and 10.

    kipy 10's signature is ``get_open_documents(doc_type)`` — the arg is
    REQUIRED, so the older no-arg call raises ``TypeError`` and (when
    swallowed) made every "is a board open?" check report False even with
    the PCB editor open.  kipy 9 took no argument.

    * ``doc_type`` given → query just that type (kipy 10), falling back to
      the no-arg form on kipy 9.
    * ``doc_type`` None → aggregate across PCB / schematic / project so
      callers that want "any open document" still work.
    """
    DocumentType = _document_type_enum()

    def _query(dt: Any) -> List[Any]:
        try:
            return list(kicad.get_open_documents(dt) or [])
        except TypeError:
            # kipy 9: no-arg signature.
            try:
                return list(kicad.get_open_documents() or [])
            except Exception:
                return []
        except Exception as e:
            logger.debug(f"get_open_documents({dt}) failed: {e}")
            return []

    if doc_type is not None:
        return _query(doc_type)

    if DocumentType is not None:
        out: List[Any] = []
        for dt in (
            DocumentType.DOCTYPE_PCB,
            DocumentType.DOCTYPE_SCHEMATIC,
            DocumentType.DOCTYPE_PROJECT,
        ):
            out.extend(_query(dt))
        return out

    # No DocumentType enum importable — last resort: kipy 9 no-arg.
    try:
        return list(kicad.get_open_documents() or [])
    except Exception:
        return []


def _document_type_enum() -> Any:
    """Return kipy's ``DocumentType`` enum, or None if unavailable."""
    try:
        from kipy.proto.common.types import DocumentType

        return DocumentType
    except Exception:
        return None


def has_open_pcb_document(kicad: Any) -> bool:
    """True iff KiCAD has at least one ``.kicad_pcb`` document open over IPC."""
    DocumentType = _document_type_enum()
    doc_type = DocumentType.DOCTYPE_PCB if DocumentType is not None else None
    for doc in get_open_documents_compat(kicad, doc_type):
        # Real kipy docs expose ``board_filename`` (+ ``project.path``); some
        # call paths / older stubs expose a single ``path``.  Accept either.
        for attr in ("board_filename", "path"):
            value = getattr(doc, attr, "") or ""
            if str(value).endswith(".kicad_pcb"):
                return True
        dtype = getattr(doc, "type", None)
        # When we queried DOCTYPE_PCB explicitly, any returned doc is a PCB.
        if doc_type is not None and dtype == doc_type:
            return True
        type_name = getattr(dtype, "name", "") if dtype is not None else ""
        if type_name in {"DOCTYPE_PCB", "PCB"}:
            return True
    return False
