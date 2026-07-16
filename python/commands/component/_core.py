"""Public ComponentCommands class, composed from per-area mixins.

commands/component.py was a single 1600-line class; it is now a package
whose ComponentCommands inherits cohesive mixins. Public API and
behaviour are unchanged.
"""

from typing import Optional

import pcbnew
from commands.library import LibraryManager, get_library_manager

from ._annotate_group_replace import AnnotateGroupReplaceMixin
from ._arrays import ArrayMixin
from ._courtyard import CourtyardMixin
from ._pads import PadsMixin
from ._placement import PlacementMixin
from ._query import QueryMixin


class ComponentCommands(
    PlacementMixin,
    QueryMixin,
    PadsMixin,
    ArrayMixin,
    CourtyardMixin,
    AnnotateGroupReplaceMixin,
):
    """Handles component-related KiCAD operations"""

    def __init__(
        self, board: Optional[pcbnew.BOARD] = None, library_manager: Optional[LibraryManager] = None
    ):
        """Initialize with optional board instance and library manager"""
        self.board = board
        self.library_manager = library_manager or get_library_manager()
