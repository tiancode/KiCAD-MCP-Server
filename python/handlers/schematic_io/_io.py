"""Schematic create / load / export(svg,pdf) / sync handlers.

Split out of the former handlers/schematic_io.py module.
See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, Dict, Optional

import pcbnew  # type: ignore[import-not-found]
from commands.schematic import SchematicManager

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger("handlers.schematic_io")


def _reload_swig_after_sync(iface: "KiCADInterface", board_path: str) -> bool:
    """Reload the SWIG in-memory board from ``board_path`` after a sync wrote it.

    ``sync_schematic_to_board`` mutates the long-lived process's
    ``pcbnew.BOARD`` in place (adds footprints/nets, reassigns pad nets) and
    saves it to disk.  Reloading from disk afterwards keeps the process holding
    the *canonical* on-disk board rather than an in-place-mutated proxy:

      * the in-memory board then matches exactly what was written, so a later
        ``get_component_list`` / ``place_component`` / ``move_component`` can
        never read a stale board;
      * the recorded disk signature is re-aligned to the on-disk content, so
        ``reconcile_backends`` doesn't misreport an external change and the
        dispatcher's follow-up ``_auto_save_board`` is a harmless no-op re-save;
      * it inherits ``_safe_load_board``'s SWIG-dehydration recovery.

    Returns True if the reload succeeded.  On failure the in-place-mutated board
    is left in place — it already matches disk (we just saved it) and its
    recorded signature is still correct — so callers degrade gracefully instead
    of dropping to a ``None`` board.
    """
    reloaded = iface._safe_load_board(board_path)
    if reloaded is None:
        logger.warning(
            "sync_schematic_to_board: could not reload SWIG board from %s after "
            "sync; keeping the in-place-mutated board (already saved to disk)",
            board_path,
        )
        return False
    iface.board = reloaded
    iface._update_command_handlers()
    # Re-record against the freshly-loaded board so the signature matches the
    # on-disk content byte-for-byte (the in-place save above already recorded
    # it; this keeps them aligned even if _safe_load_board recovered a
    # dehydrated proxy via a pcbnew module reload).
    iface._record_board_signature(board_path)
    return True


def handle_sync_schematic_to_board(
    iface: "KiCADInterface", params: Dict[str, Any]
) -> Dict[str, Any]:
    """Sync schematic netlist to PCB board (equivalent to KiCAD F8 'Update PCB from Schematic').
    Reads net connections from the schematic and assigns them to the matching pads in the PCB.
    """
    logger.info("Syncing schematic to board")
    try:
        from pathlib import Path

        schematic_path = params.get("schematicPath")
        board_path = params.get("boardPath")

        # Determine board to work with
        board = None
        if board_path:
            board = iface._safe_load_board(board_path)
            if board is None:
                return {
                    "success": False,
                    "message": f"Could not load board from {board_path}",
                    "errorDetails": (
                        "pcbnew.LoadBoard failed or returned a dehydrated "
                        "SWIG proxy that could not be recovered"
                    ),
                }
        elif iface.board:
            board = iface.board
            board_path = board.GetFileName() if not board_path else board_path
        else:
            return {
                "success": False,
                "message": "No board loaded. Use open_project first or provide boardPath.",
            }

        if not board_path:
            board_path = board.GetFileName()

        # Determine schematic path if not provided
        if not schematic_path:
            sch = Path(board_path).with_suffix(".kicad_sch")
            if sch.exists():
                schematic_path = str(sch)
            else:
                project_dir = Path(board_path).parent
                sch_files = list(project_dir.glob("*.kicad_sch"))
                if sch_files:
                    schematic_path = str(sch_files[0])

        if not schematic_path or not Path(schematic_path).exists():
            return {
                "success": False,
                "message": f"Schematic not found. Provide schematicPath. Tried: {schematic_path}",
            }

        # Build pad→net map.  Prefer the authoritative kicad-cli netlist: it
        # names label-less wire nets exactly as KiCad's "Update PCB from
        # Schematic" does (``Net-(D1-A)``), which the label/BFS parser cannot
        # see — so plain-wire connections with no label anywhere are no longer
        # silently dropped off the board (finding B1).  Export the netlist once
        # and reuse it for the missing-footprint pass, so a single sync never
        # spawns kicad-cli twice.
        netlist_root = iface._export_schematic_netlist_xml(schematic_path)
        netlist_source = "fallback-bfs"
        pad_net_map: Dict[Any, str] = {}
        net_names: set = set()
        if netlist_root is not None:
            pad_net_map, net_names = iface._pad_net_map_from_netlist_root(netlist_root)
            if pad_net_map:
                netlist_source = "kicad-cli"
        if not pad_net_map:
            # kicad-cli unavailable / produced nothing — fall back to the
            # label/#PWR/BFS parser (which now also synthesizes
            # Net-(<ref>-<pin>) names for label-less clusters).
            pad_net_map, net_names = iface._build_hierarchical_pad_net_map(schematic_path)
            netlist_source = "fallback-bfs"

        # Add missing footprints from the schematic to the board *before*
        # we add nets and assign pads — F8 in KiCad does this implicitly
        # ("Update PCB from Schematic"), but our previous implementation
        # only mutated nets, leaving newly-added schematic symbols with no
        # PCB footprint at all.  Reuse the netlist we already exported.
        added_footprints, skipped_footprints = iface._add_missing_footprints_from_schematic(
            board, schematic_path, netlist_root=netlist_root
        )

        # Add all nets to board
        netinfo = board.GetNetInfo()
        nets_by_name = netinfo.NetsByName()
        added_nets = []
        for net_name in net_names:
            if not nets_by_name.has_key(net_name):
                net_item = pcbnew.NETINFO_ITEM(board, net_name)
                board.Add(net_item)
                added_nets.append(net_name)

        # Refresh nets map after additions
        netinfo = board.GetNetInfo()
        nets_by_name = netinfo.NetsByName()

        # Assign nets to pads (now also covers any footprints we just added)
        assigned_pads = 0
        assigned_keys: set = set()
        for fp in board.GetFootprints():
            ref = fp.GetReference()
            for pad in fp.Pads():
                pad_num = pad.GetNumber()
                key = (ref, str(pad_num))
                if key in pad_net_map:
                    net_name = pad_net_map[key]
                    if nets_by_name.has_key(net_name):
                        pad.SetNet(nets_by_name[net_name])
                        assigned_pads += 1
                        assigned_keys.add(key)

        # Honest degradation: any schematic connection the netlist expects but
        # that did NOT land on a board pad is a real electrical loss.  Surface
        # the FULL list (not just a sample) plus which nets — if any — dropped
        # entirely, instead of returning a bare success (finding B1).
        from collections import defaultdict as _dd

        unmatched_pads = sorted(f"{ref}/{pin}" for (ref, pin) in (set(pad_net_map) - assigned_keys))
        net_expected: Dict[str, int] = _dd(int)
        net_assigned: Dict[str, int] = _dd(int)
        for _k, _n in pad_net_map.items():
            net_expected[_n] += 1
        for _k in assigned_keys:
            net_assigned[pad_net_map[_k]] += 1
        dropped_nets = sorted(n for n in net_expected if net_assigned[n] == 0)

        # Propagate the schematic symbols' custom fields (MPN, Manufacturer,
        # "LCSC Part", Datasheet, …) onto their matching board footprints —
        # KiCad's own "Update PCB from Schematic" copies these, and without it
        # the entire phase-1 sourcing pipeline is invisible in a board-based
        # export_bom.  Reuses the already-exported netlist (parse only, no
        # extra kicad-cli spawn) and covers BOTH the footprints we just added
        # and the ones already on the board (re-sync updates changed values).
        sch_components = iface._extract_components_from_schematic(
            schematic_path, root=netlist_root
        )
        fields_result = iface._propagate_schematic_fields_to_board(board, sch_components)

        # Route through the iface helper so the in-memory signature tracks
        # the new on-disk hash; otherwise the dispatcher's follow-up
        # _auto_save_board() sees a mismatch and refuses the next write.
        iface._save_board_and_record(board, board_path)

        # If board was loaded fresh, update internal reference
        if params.get("boardPath"):
            iface.board = board
            iface._update_command_handlers()

        # The sync just rewrote the .kicad_pcb on disk.  Reload the SWIG board
        # from disk so the long-lived process holds the canonical post-sync
        # board — otherwise the next place/move/get_component_list would read
        # the in-place-mutated proxy (and, worse, a later auto-save could
        # round-trip that proxy).  On reload failure the in-place board stays
        # (it already matches disk), so we never drop to a None board.
        board_reloaded = _reload_swig_after_sync(iface, board_path)

        # SWIG landed new content on disk that a running KiCad instance (if any)
        # hasn't picked up.  Flag the SWIG->IPC divergence so get_backend_info /
        # reconcile_backends report it truthfully and any later IPC save is
        # gated (the dispatcher's post-handler auto-save sets this too; setting
        # it here keeps the handler correct on its own regardless of the
        # command's auto-save classification).
        iface._swig_writes_landed = True
        ipc_attached = getattr(iface, "ipc_board_api", None) is not None

        logger.info(
            f"sync_schematic_to_board: {len(added_nets)} nets added, "
            f"{len(added_footprints)} footprints added, {assigned_pads} pads assigned, "
            f"{fields_result['footprints_updated']} footprints got sourcing fields"
        )
        # Surface the grid-placement contract so agents know each new
        # footprint landed at a distinct position and which positions
        # they were — previously they all stacked at (0, 0) and the
        # caller had to issue N move_component calls before anything
        # was visible.
        layout_note: Optional[str] = None
        if added_footprints:
            positions = [fp.get("position") for fp in added_footprints if fp.get("position")]
            if positions:
                xs = [p["x_mm"] for p in positions]
                ys = [p["y_mm"] for p in positions]
                layout_note = (
                    f"{len(added_footprints)} new footprints grid-placed: "
                    f"x in [{min(xs)}, {max(xs)}] mm, "
                    f"y in [{min(ys)}, {max(ys)}] mm. "
                    f"Call move_component on each ref to reposition."
                )
        response: Dict[str, Any] = {
            "success": True,
            "message": (
                f"PCB updated from schematic: {len(added_footprints)} footprints added, "
                f"{len(added_nets)} nets added, {assigned_pads} pads assigned"
            ),
            "nets_added": added_nets,
            "nets_total": len(net_names),
            "pads_assigned": assigned_pads,
            # Which parser produced the pad→net map: the authoritative kicad-cli
            # netlist ("kicad-cli", names anonymous wire nets like KiCad) or the
            # label/BFS fallback ("fallback-bfs").
            "netlist_source": netlist_source,
            # FULL list of schematic pins that expected a net but were not
            # assigned on the board (electrical losses), plus a short sample
            # under the historical field name for backward compatibility.
            "unmatched_pads": unmatched_pads,
            "unmatched_pads_sample": unmatched_pads[:10],
            "footprints_added": added_footprints,
            "footprints_skipped": skipped_footprints,
            # How many board footprints received (added or value-changed)
            # custom sourcing fields copied from their schematic symbol.
            "fields_footprints_updated": fields_result["footprints_updated"],
            "fields_written": fields_result["fields_written"],
            "layout_note": layout_note,
            # The in-memory SWIG board was reloaded from disk so subsequent
            # place/move/get_component_list calls see the synced footprints.
            "boardReloaded": board_reloaded,
        }
        if unmatched_pads:
            warn = (
                f"{len(unmatched_pads)} schematic pin(s) expected a net but were not "
                f"assigned on the board"
            )
            if dropped_nets:
                warn += f"; {len(dropped_nets)} net(s) dropped entirely: {dropped_nets}"
            response["warning"] = warn
            response["dropped_nets"] = dropped_nets
        if ipc_attached:
            # KiCad has the same board open over IPC; its live in-memory copy is
            # now OLDER than disk and does NOT reflect this sync.
            response["ipcStale"] = True
            response["ipcStaleHint"] = (
                "This sync wrote new content to the .kicad_pcb on disk via the "
                "SWIG path. KiCad's live in-memory board (open over IPC) is now "
                "OLDER than disk and does NOT show these changes. Call "
                "`reconcile_backends` (direction=swig_to_ipc) to reload KiCad "
                "from disk (via board.revert()), or reload manually in KiCad "
                "(File -> Revert from saved)."
            )
        return response

    except Exception as e:
        logger.error(f"Error in sync_schematic_to_board: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"success": False, "message": str(e)}


def handle_export_schematic_pdf(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Export schematic to PDF"""
    logger.info("Exporting schematic to PDF")
    try:
        schematic_path = params.get("schematicPath")
        output_path = params.get("outputPath")

        if not schematic_path:
            return {"success": False, "message": "Schematic path is required"}
        if not output_path:
            return {"success": False, "message": "Output path is required"}

        if not os.path.exists(schematic_path):
            return {
                "success": False,
                "message": f"Schematic not found: {schematic_path}",
            }

        # Resolve kicad-cli through the shared bundle resolver (PATH first, then
        # platform fallbacks — e.g. KiCad.app/Contents/MacOS on macOS, where
        # kicad-cli is never on PATH). run_erc / get_schematic_view resolve the
        # same way; hardcoding "kicad-cli" here made PDF export fail on macOS
        # even though the binary was installed inside the app bundle.
        kicad_cli = iface._find_kicad_cli_static()
        if not kicad_cli:
            return {
                "success": False,
                "message": (
                    "kicad-cli not found. Install KiCAD 8.0+ or add kicad-cli to "
                    "PATH (on macOS it lives in KiCad.app/Contents/MacOS)."
                ),
            }

        import subprocess

        cmd = [
            kicad_cli,
            "sch",
            "export",
            "pdf",
            "--output",
            output_path,
            schematic_path,
        ]

        if params.get("blackAndWhite"):
            cmd.insert(-1, "--black-and-white")

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        if result.returncode == 0:
            return {"success": True, "file": {"path": output_path}}
        else:
            return {
                "success": False,
                "message": f"kicad-cli failed: {result.stderr}",
            }

    except FileNotFoundError:
        return {
            "success": False,
            "message": (
                "kicad-cli not found. Install KiCAD 8.0+ or add kicad-cli to "
                "PATH (on macOS it lives in KiCad.app/Contents/MacOS)."
            ),
        }
    except Exception as e:
        logger.error(f"Error exporting schematic to PDF: {str(e)}")
        return {"success": False, "message": str(e)}


def handle_create_schematic(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new schematic"""
    logger.info("Creating schematic")
    try:
        # Support multiple parameter naming conventions for compatibility:
        # - TypeScript tools use: name, path
        # - Python schema uses: filename, title
        # - Legacy uses: projectName, path, metadata
        project_name = params.get("projectName") or params.get("name") or params.get("title")

        # Handle filename parameter - it may contain full path
        filename = params.get("filename")
        if filename:
            # If filename provided, extract name and path from it
            if filename.endswith(".kicad_sch"):
                filename = filename[:-10]  # Remove .kicad_sch extension
            path = os.path.dirname(filename) or "."
            project_name = project_name or os.path.basename(filename)
        else:
            path = params.get("path", ".")
        metadata = params.get("metadata", {})

        if not project_name:
            return {
                "success": False,
                "message": "Schematic name is required. Provide 'name', 'projectName', or 'filename' parameter.",
            }

        base_name = (
            project_name if project_name.endswith(".kicad_sch") else f"{project_name}.kicad_sch"
        )
        normalized_path = path or "."
        file_path = os.path.join(normalized_path, base_name)

        # Refuse to clobber an existing schematic. create_schematic copies the
        # template over file_path unconditionally, so without this guard a name
        # collision silently wipes the user's sheet.
        if not bool(params.get("overwrite", False)) and os.path.exists(file_path):
            return {
                "success": False,
                "message": (
                    f"Schematic already exists: {file_path}. "
                    "Pass overwrite=true to replace it, or choose a different name."
                ),
                "errorCode": "SCHEMATIC_EXISTS",
                "hint": "Refusing to overwrite an existing schematic. Pick a new name or set overwrite=true.",
            }

        sch_path = path if path and path != "." else None
        schematic = SchematicManager.create_schematic(
            project_name, path=sch_path, metadata=metadata
        )
        success = SchematicManager.save_schematic(schematic, file_path)

        return {"success": success, "file_path": file_path}
    except Exception as e:
        logger.error(f"Error creating schematic: {str(e)}")
        return {"success": False, "message": str(e)}
