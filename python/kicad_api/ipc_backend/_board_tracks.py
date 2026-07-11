"""IPCBoardAPI track / via / text / net operations.

Split out of the former monolithic kicad_api/ipc_backend.py.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("kicad_interface")


class _TrackMixin:
    def add_track(
        self,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        width: float = 0.25,
        layer: str = "F.Cu",
        net_name: Optional[str] = None,
    ) -> bool:
        """
        Add a track (trace) to the board.

        The track appears immediately in the KiCAD UI.
        """
        try:
            from kipy.board_types import Track
            from kipy.geometry import Vector2
            from kipy.proto.board.board_types_pb2 import BoardLayer
            from kipy.util.units import from_mm

            board = self._get_board()

            # Create track
            track = Track()
            track.start = Vector2.from_xy(from_mm(start_x), from_mm(start_y))
            track.end = Vector2.from_xy(from_mm(end_x), from_mm(end_y))
            track.width = from_mm(width)

            # Set layer
            layer_map = {
                "F.Cu": BoardLayer.BL_F_Cu,
                "B.Cu": BoardLayer.BL_B_Cu,
                "In1.Cu": BoardLayer.BL_In1_Cu,
                "In2.Cu": BoardLayer.BL_In2_Cu,
            }
            track.layer = layer_map.get(layer, BoardLayer.BL_F_Cu)

            # Set net if specified
            if net_name:
                nets = board.get_nets()
                for net in nets:
                    if net.name == net_name:
                        track.net = net
                        break

            self._apply_create(board, track, "Added track")

            self._notify(
                "track_added",
                {
                    "start": {"x": start_x, "y": start_y},
                    "end": {"x": end_x, "y": end_y},
                    "width": width,
                    "layer": layer,
                    "net": net_name,
                },
            )

            logger.info(f"Added track from ({start_x}, {start_y}) to ({end_x}, {end_y}) mm")
            return True

        except Exception as e:
            logger.error(f"Failed to add track: {e}")
            return False

    def add_arc_track(
        self,
        start_x: float,
        start_y: float,
        mid_x: float,
        mid_y: float,
        end_x: float,
        end_y: float,
        width: float = 0.25,
        layer: str = "F.Cu",
        net_name: Optional[str] = None,
    ) -> bool:
        """Add a copper arc track to the board."""
        try:
            from kipy.board_types import ArcTrack
            from kipy.geometry import Vector2
            from kipy.proto.board.board_types_pb2 import BoardLayer
            from kipy.util.units import from_mm

            board = self._get_board()

            arc = ArcTrack()
            arc.start = Vector2.from_xy(from_mm(start_x), from_mm(start_y))
            arc.mid = Vector2.from_xy(from_mm(mid_x), from_mm(mid_y))
            arc.end = Vector2.from_xy(from_mm(end_x), from_mm(end_y))
            arc.width = from_mm(width)

            layer_map = {
                "F.Cu": BoardLayer.BL_F_Cu,
                "B.Cu": BoardLayer.BL_B_Cu,
                "In1.Cu": BoardLayer.BL_In1_Cu,
                "In2.Cu": BoardLayer.BL_In2_Cu,
            }
            arc.layer = layer_map.get(layer, BoardLayer.BL_F_Cu)

            if net_name:
                nets = board.get_nets()
                for net in nets:
                    if net.name == net_name:
                        arc.net = net
                        break

            self._apply_create(board, arc, "Added arc track")

            self._notify(
                "arc_track_added",
                {
                    "start": {"x": start_x, "y": start_y},
                    "mid": {"x": mid_x, "y": mid_y},
                    "end": {"x": end_x, "y": end_y},
                    "width": width,
                    "layer": layer,
                    "net": net_name,
                },
            )
            logger.info(
                f"Added arc track start=({start_x}, {start_y}) mid=({mid_x}, {mid_y}) end=({end_x}, {end_y}) mm"
            )
            return True
        except Exception as e:
            logger.error(f"Failed to add arc track: {e}")
            return False

    def add_via(
        self,
        x: float,
        y: float,
        diameter: float = 0.8,
        drill: float = 0.4,
        net_name: Optional[str] = None,
        via_type: str = "through",
    ) -> bool:
        """
        Add a via to the board.

        The via appears immediately in the KiCAD UI.
        """
        try:
            from kipy.board_types import Via
            from kipy.geometry import Vector2
            from kipy.proto.board.board_types_pb2 import ViaType
            from kipy.util.units import from_mm

            board = self._get_board()

            # Create via
            via = Via()
            via.position = Vector2.from_xy(from_mm(x), from_mm(y))
            via.diameter = from_mm(diameter)
            via.drill_diameter = from_mm(drill)

            # Set via type (enum values: VT_THROUGH=1, VT_BLIND_BURIED=2, VT_MICRO=3)
            type_map = {
                "through": ViaType.VT_THROUGH,
                "blind": ViaType.VT_BLIND_BURIED,
                "micro": ViaType.VT_MICRO,
            }
            via.type = type_map.get(via_type, ViaType.VT_THROUGH)

            # Set net if specified
            if net_name:
                nets = board.get_nets()
                for net in nets:
                    if net.name == net_name:
                        via.net = net
                        break

            self._apply_create(board, via, "Added via")

            self._notify(
                "via_added",
                {
                    "position": {"x": x, "y": y},
                    "diameter": diameter,
                    "drill": drill,
                    "net": net_name,
                    "type": via_type,
                },
            )

            logger.info(f"Added via at ({x}, {y}) mm")
            return True

        except Exception as e:
            logger.error(f"Failed to add via: {e}")
            return False

    def add_text(
        self,
        text: str,
        x: float,
        y: float,
        layer: str = "F.SilkS",
        size: float = 1.0,
        rotation: float = 0,
    ) -> bool:
        """Add text to the board."""
        try:
            from kipy.board_types import BoardText
            from kipy.geometry import Angle, Vector2
            from kipy.proto.board.board_types_pb2 import BoardLayer
            from kipy.util.units import from_mm

            board = self._get_board()

            # Create text
            board_text = BoardText()
            board_text.value = text
            board_text.position = Vector2.from_xy(from_mm(x), from_mm(y))
            board_text.angle = Angle.from_degrees(rotation)

            # Set layer
            layer_map = {
                "F.SilkS": BoardLayer.BL_F_SilkS,
                "B.SilkS": BoardLayer.BL_B_SilkS,
                "F.Cu": BoardLayer.BL_F_Cu,
                "B.Cu": BoardLayer.BL_B_Cu,
            }
            board_text.layer = layer_map.get(layer, BoardLayer.BL_F_SilkS)

            self._apply_create(board, board_text, f"Added text: {text}")

            self._notify("text_added", {"text": text, "position": {"x": x, "y": y}, "layer": layer})

            return True

        except Exception as e:
            logger.error(f"Failed to add text: {e}")
            return False

    def get_tracks(self) -> List[Dict[str, Any]]:
        """Get all tracks on the board."""
        try:
            from kipy.util.units import to_mm

            board = self._get_board()
            tracks = board.get_tracks()

            result = []
            for track in tracks:
                try:
                    result.append(
                        {
                            "start": {"x": to_mm(track.start.x), "y": to_mm(track.start.y)},
                            "end": {"x": to_mm(track.end.x), "y": to_mm(track.end.y)},
                            "width": to_mm(track.width),
                            "layer": str(track.layer),
                            "net": track.net.name if track.net else "",
                            "id": str(track.id) if hasattr(track, "id") else "",
                        }
                    )
                except Exception as e:
                    logger.warning(f"Error processing track: {e}")
                    continue

            return result

        except Exception as e:
            logger.error(f"Failed to get tracks: {e}")
            return []

    def get_vias(self) -> List[Dict[str, Any]]:
        """Get all vias on the board."""
        try:
            from kipy.util.units import to_mm

            board = self._get_board()
            vias = board.get_vias()

            result = []
            for via in vias:
                try:
                    result.append(
                        {
                            "position": {"x": to_mm(via.position.x), "y": to_mm(via.position.y)},
                            "diameter": to_mm(via.diameter),
                            "drill": to_mm(via.drill_diameter),
                            "net": via.net.name if via.net else "",
                            "type": str(via.type),
                            "id": str(via.id) if hasattr(via, "id") else "",
                        }
                    )
                except Exception as e:
                    logger.warning(f"Error processing via: {e}")
                    continue

            return result

        except Exception as e:
            logger.error(f"Failed to get vias: {e}")
            return []

    def get_nets(self) -> List[Dict[str, Any]]:
        """Get all nets on the board."""
        try:
            board = self._get_board()
            nets = board.get_nets()

            result = []
            for net in nets:
                try:
                    result.append(
                        {"name": net.name, "code": net.code if hasattr(net, "code") else 0}
                    )
                except Exception as e:
                    logger.warning(f"Error processing net: {e}")
                    continue

            return result

        except Exception as e:
            logger.error(f"Failed to get nets: {e}")
            return []
