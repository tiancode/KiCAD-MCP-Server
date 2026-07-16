"""Net routed-length reporting for RoutingCommands.

Pure length computation lives in module-level functions (testable without
pcbnew); the LengthMixin method extracts track/via dicts from the SWIG
board and delegates here. Via barrel length is not included in totals —
`viaCount` is reported so callers can add a per-via allowance themselves.
"""

import fnmatch
import logging
import math
from typing import Any, Dict, List, Optional, Tuple

from utils.responses import failed, no_board_loaded

logger = logging.getLogger("kicad_interface")

_NM_PER_MM = 1_000_000


def extract_track_via_dicts(board: Any) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Extract plain track/via dicts from a SWIG board for ``compute_net_lengths``.

    Returns ``(tracks, vias)``: vias carry only their net; straight tracks carry
    net + endpoints (mm) + layer, and arcs additionally carry their true curved
    ``length`` (mm). Unreadable items are skipped with a warning, never fatal.
    Shared by ``report_net_lengths`` and ``get_nets_list`` stats so the two
    tools can't drift on how copper is read off the board.
    """
    import pcbnew

    tracks: List[Dict[str, Any]] = []
    vias: List[Dict[str, Any]] = []
    for track in list(board.Tracks()):
        try:
            if track.Type() == pcbnew.PCB_VIA_T:
                vias.append({"net": track.GetNetname()})
                continue
            start = track.GetStart()
            end = track.GetEnd()
            item: Dict[str, Any] = {
                "net": track.GetNetname(),
                "startX": start.x / _NM_PER_MM,
                "startY": start.y / _NM_PER_MM,
                "endX": end.x / _NM_PER_MM,
                "endY": end.y / _NM_PER_MM,
                "layer": board.GetLayerName(track.GetLayer()),
            }
            # Arcs report their true curved length; straight segments are
            # computed from endpoints in compute_net_lengths.
            if track.Type() == pcbnew.PCB_ARC_T and hasattr(track, "GetLength"):
                item["length"] = track.GetLength() / _NM_PER_MM
            tracks.append(item)
        except Exception as track_err:  # noqa: BLE001 — skip unreadable items
            logger.warning(f"extract_track_via_dicts: skipping track: {track_err}")
    return tracks, vias


def compute_net_lengths(
    tracks: List[Dict[str, Any]],
    vias: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Aggregate routed length per net from plain track/via dicts.

    tracks: [{"net": str, "startX","startY","endX","endY" (mm), "layer": str,
              "length": optional precomputed mm (arcs)}]
    vias:   [{"net": str}]
    """
    report: Dict[str, Dict[str, Any]] = {}
    for t in tracks:
        net = t.get("net") or ""
        entry = report.setdefault(
            net, {"lengthMm": 0.0, "segmentCount": 0, "viaCount": 0, "layers": set()}
        )
        length = t.get("length")
        if length is None:
            length = math.hypot(t["endX"] - t["startX"], t["endY"] - t["startY"])
        entry["lengthMm"] += float(length)
        entry["segmentCount"] += 1
        if t.get("layer"):
            entry["layers"].add(t["layer"])
    for v in vias:
        net = v.get("net") or ""
        if net in report:
            report[net]["viaCount"] += 1
        else:
            report.setdefault(
                net, {"lengthMm": 0.0, "segmentCount": 0, "viaCount": 1, "layers": set()}
            )
    for entry in report.values():
        entry["lengthMm"] = round(entry["lengthMm"], 4)
        entry["layers"] = sorted(entry["layers"])
    return report


def build_length_report(
    per_net: Dict[str, Dict[str, Any]],
    *,
    nets: Optional[List[str]] = None,
    pattern: Optional[str] = None,
) -> Dict[str, Any]:
    """Filter per-net data and add group skew statistics.

    nets: exact net names to report (order preserved in the skew group).
    pattern: fnmatch wildcard (e.g. "DDR_DQ*"); combined with `nets` by union.
    With no filter, all routed nets are reported and skew covers all of them.
    """
    selected: Dict[str, Dict[str, Any]] = {}
    missing: List[str] = []
    if nets:
        for name in nets:
            if name in per_net:
                selected[name] = per_net[name]
            else:
                missing.append(name)
    if pattern:
        for name in sorted(per_net):
            if fnmatch.fnmatchcase(name, pattern):
                selected.setdefault(name, per_net[name])
    if not nets and not pattern:
        selected = dict(sorted(per_net.items()))

    rows = [{"net": name, **{k: v for k, v in data.items()}} for name, data in selected.items()]
    rows.sort(key=lambda r: (-r["lengthMm"], r["net"]))

    skew: Optional[Dict[str, Any]] = None
    if len(rows) >= 2:
        lengths = [r["lengthMm"] for r in rows]
        longest = max(lengths)
        shortest = min(lengths)
        skew = {
            "longestNet": next(r["net"] for r in rows if r["lengthMm"] == longest),
            "shortestNet": next(r["net"] for r in rows if r["lengthMm"] == shortest),
            "maxSkewMm": round(longest - shortest, 4),
        }

    result: Dict[str, Any] = {"nets": rows, "netCount": len(rows)}
    if skew is not None:
        result["skew"] = skew
    if missing:
        result["missingNets"] = missing
    return result


class LengthMixin:
    """Adds report_net_lengths to RoutingCommands."""

    def report_net_lengths(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Report total routed copper length per net, with group skew stats.

        Params: nets (list[str], optional), pattern (wildcard str, optional).
        Via barrel length is excluded; viaCount lets callers budget it.
        """
        try:
            if not self.board:
                return no_board_loaded()

            tracks, vias = extract_track_via_dicts(self.board)
            per_net = compute_net_lengths(tracks, vias)
            report = build_length_report(
                per_net,
                nets=params.get("nets"),
                pattern=params.get("pattern"),
            )
            return {
                "success": True,
                "unit": "mm",
                "viaLengthIncluded": False,
                **report,
            }
        except Exception as e:  # API boundary; bucket: catch + return
            logger.error(f"Error reporting net lengths: {str(e)}")
            return failed("Failed to report net lengths", e)
