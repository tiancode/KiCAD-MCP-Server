"""Shared footprint classification helpers.

Kept backend-agnostic so the SWIG component listing
(``commands/component/_query.py``) and the IPC listing
(``kicad_api/ipc_backend/_board_components.py``) tag components identically —
both take the footprint library id string and the reference designator, so a
mounting hole reads the same regardless of which backend answered the query.
"""

import re

# Reference designators the MCP assigns to auto-generated mounting holes
# (``add_mounting_hole`` emits MH1, MH2, …).  KiCad's own board files use the
# same convention.
_MH_REF_RE = re.compile(r"^MH\d+$", re.IGNORECASE)


def is_mounting_hole(footprint: str, reference: str = "") -> bool:
    """Return True when a footprint is a mounting hole.

    Classified from two backend-independent signals:
      * the footprint library id contains ``MountingHole`` (KiCad's stock
        library nickname / the id ``add_mounting_hole`` synthesises), or
      * the reference matches the ``MH<n>`` designator convention.

    A mounting hole is still a real footprint with a real reference, so it is
    never dropped from a component list — this flag just lets a consumer filter
    it out intentionally (e.g. for BOM/pick-and-place), and guarantees the SWIG
    and IPC listings agree on which parts are holes.
    """
    fpid = (footprint or "").lower()
    if "mountinghole" in fpid:
        return True
    return bool(_MH_REF_RE.match(reference or ""))
