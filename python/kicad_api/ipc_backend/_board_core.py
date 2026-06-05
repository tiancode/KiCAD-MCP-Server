"""Public IPCBoardAPI class, composed from per-area mixins.

Split out of the former monolithic kicad_api/ipc_backend.py; public API and
behaviour are unchanged.
"""

import logging
import os
import platform
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from kicad_api.base import APINotAvailableError, BoardAPI, ConnectionError, KiCADBackend

from ._helpers import (
    INCH_TO_NM,
    MM_TO_NM,
    _document_type_enum,
    get_open_documents_compat,
    has_open_pcb_document,
)

logger = logging.getLogger("kicad_interface")

from ._board_common import _CommonMixin
from ._board_components import _ComponentMixin
from ._board_geometry import _GeometryMixin
from ._board_selection import _SelectionMixin
from ._board_shapes import _ShapeMixin
from ._board_tracks import _TrackMixin
from ._board_transactions import _TransactionMixin
from ._board_zones import _ZoneMixin


class IPCBoardAPI(
    _TransactionMixin,
    _GeometryMixin,
    _ComponentMixin,
    _TrackMixin,
    _ZoneMixin,
    _SelectionMixin,
    _ShapeMixin,
    _CommonMixin,
    BoardAPI,
):
    def __init__(self, kicad_instance: Any, notify_callback: Callable) -> None:
        self._kicad = kicad_instance
        self._board = None
        self._notify = notify_callback
        # Active transaction state. When _current_commit is set, every
        # _apply_create/update/remove call piggy-backs on it instead of
        # opening its own per-call commit, so the whole sequence lands
        # as one undo step in KiCad.
        self._current_commit: Optional[Any] = None
        self._current_commit_description: Optional[str] = None
