"""Public RoutingCommands class, composed from per-area mixins.

commands/routing.py was a single 2300-line class; it is now a package
whose RoutingCommands inherits cohesive mixins (traces, vias, zones,
nets, geometry). The public API and behaviour are unchanged.
"""

from typing import Optional

import pcbnew

from ._geometry import GeometryMixin
from ._nets import NetMixin
from ._traces import TraceMixin
from ._vias import ViaMixin
from ._zones import ZoneMixin


class RoutingCommands(TraceMixin, ViaMixin, ZoneMixin, NetMixin, GeometryMixin):
    """Handles routing-related KiCAD operations"""

    def __init__(self, board: Optional[pcbnew.BOARD] = None):
        """Initialize with optional board instance"""
        self.board = board
