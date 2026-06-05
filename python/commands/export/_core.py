"""Public ExportCommands class, composed from per-area mixins.

commands/export.py was a single ~970-line class; it is now a package whose
ExportCommands inherits cohesive mixins. Public API and behaviour are
unchanged.
"""

from typing import Optional

import pcbnew

from ._bom import BomMixin
from ._common import CommonMixin
from ._documents import DocumentMixin
from ._fabrication import FabricationMixin


class ExportCommands(FabricationMixin, DocumentMixin, BomMixin, CommonMixin):
    """Handles export-related KiCAD operations"""

    def __init__(self, board: Optional[pcbnew.BOARD] = None):
        """Initialize with optional board instance"""
        self.board = board
