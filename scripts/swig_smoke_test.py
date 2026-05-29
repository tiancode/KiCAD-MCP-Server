"""
SWIG-based PCB smoke test driven by the MCP server's own command layer.

Runs the full create_project → board sizing → mounting holes →
footprint placement → routing → save chain in-process, using real
pcbnew, and inspects the resulting .kicad_pcb.  The pytest suite covers
the same code paths with MagicMock'd pcbnew; this script is the
end-to-end equivalent for catching regressions that only surface against
a real KiCAD install (SWIG dehydration, library URI resolution, layer
enumeration drift across KiCAD versions, …).

Run on a KiCAD 10 Flatpak install:

    flatpak run --command=python3 \\
        --filesystem=/tmp:rw \\
        --filesystem=<repo-root>:ro \\
        --filesystem=/var/lib/flatpak:ro \\
        org.kicad.KiCad scripts/swig_smoke_test.py

Run on a native KiCAD install (pcbnew on host PYTHONPATH):

    python3 scripts/swig_smoke_test.py

The board lands at $MCP_TEST_DIR (default /tmp/mcp-pcb-test/).
"""

import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

# Stub host-only deps that Flatpak Python doesn't have (PIL, cairosvg,
# colorlog, skip).  These only matter for tools we're NOT exercising
# here (board rendering, schematic write).  pcbnew is real.
for _m in ("PIL", "PIL.Image", "cairosvg", "colorlog"):
    sys.modules[_m] = MagicMock(name=_m)
_skip_mod = types.ModuleType("skip")


class _FakeSchematic:
    def __init__(self, path):
        self.path = path
        self.symbol = []


_skip_mod.Schematic = _FakeSchematic
sys.modules["skip"] = _skip_mod

# Anchor at scripts/.. so the script runs from any checkout location.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "python"))

# Force SWIG (we have real pcbnew via flatpak run --command=python3)
os.environ["KICAD_BACKEND"] = "swig"

PROJECT_DIR = Path(os.environ.get("MCP_TEST_DIR", "/tmp/mcp-pcb-test"))
PROJECT_DIR.mkdir(parents=True, exist_ok=True)
PROJECT_PATH = PROJECT_DIR / "mcp_smoke_test.kicad_pro"
BOARD_PATH = PROJECT_DIR / "mcp_smoke_test.kicad_pcb"


def dump(label, result):
    print(f"\n=== {label} ===")
    if isinstance(result, dict):
        compact = {k: v for k, v in result.items() if not k.startswith("_")}
        print(json.dumps(compact, indent=2, default=str)[:600])
    else:
        print(result)


def main():
    # We can't bypass commands/__init__.py at this point because it cascades
    # into board/view.py → PIL — already stubbed above, so it's safe to
    # import the package normally now.
    import pcbnew
    from commands.board import BoardCommands
    from commands.component import ComponentCommands
    from commands.design_rules import DesignRuleCommands
    from commands.library import LibraryManager as FootprintLibraryManager
    from commands.project import ProjectCommands
    from commands.routing import RoutingCommands

    # Step 0 — Clean any prior run so we start from a known state.
    for f in PROJECT_DIR.glob("mcp_smoke_test.*"):
        f.unlink()

    # Step 1 — Create the project (SWIG path), then open it for subsequent
    # operations.  create_project lays down .kicad_pro, .kicad_pcb, .kicad_sch.
    project = ProjectCommands()
    dump(
        "create_project",
        project.create_project({"name": "mcp_smoke_test", "path": str(PROJECT_DIR)}),
    )
    res = project.open_project({"filename": str(PROJECT_PATH)})
    dump("open_project", res)
    board = project.board

    # Step 2 — Board size + outline
    board_cmds = BoardCommands(board=board)
    dump("set_board_size", board_cmds.set_board_size({"width": 80, "height": 60, "unit": "mm"}))
    dump(
        "add_board_outline",
        board_cmds.add_board_outline(
            {"shape": "rectangle", "width": 80, "height": 60, "unit": "mm"}
        ),
    )
    dump("get_board_info", board_cmds.get_board_info({}))
    dump("get_layer_list", board_cmds.get_layer_list({}))

    # Step 3 — Mounting holes (4 corners)
    for x, y in [(5, 5), (75, 5), (5, 55), (75, 55)]:
        res = board_cmds.add_mounting_hole(
            {"position": {"x": x, "y": y, "unit": "mm"}, "diameter": 3.2, "drill": 3.2}
        )
    dump("add_mounting_hole x4 (last result)", res)

    # Step 4 — Place a couple of footprints
    fp_lib = FootprintLibraryManager()
    comp_cmds = ComponentCommands(board, fp_lib)
    dump(
        "place_component R1",
        comp_cmds.place_component(
            {
                "componentId": "Resistor_SMD:R_0603_1608Metric",
                "position": {"x": 20, "y": 20, "unit": "mm"},
                "reference": "R1",
                "value": "10k",
                "rotation": 0,
                "layer": "F.Cu",
            }
        ),
    )
    dump(
        "place_component R2",
        comp_cmds.place_component(
            {
                "componentId": "Resistor_SMD:R_0603_1608Metric",
                "position": {"x": 30, "y": 20, "unit": "mm"},
                "reference": "R2",
                "value": "1k",
                "rotation": 0,
                "layer": "F.Cu",
            }
        ),
    )
    dump("get_component_list", comp_cmds.get_component_list({}))
    dump("find_component R1", comp_cmds.find_component({"reference": "R1"}))

    # Step 5 — Net + trace + via
    routing = RoutingCommands(board)
    dump("add_net (SIGNAL_1)", routing.add_net({"name": "SIGNAL_1"}))
    dump(
        "route_trace",
        routing.route_trace(
            {
                "start": {"x": 20, "y": 20, "unit": "mm"},
                "end": {"x": 30, "y": 20, "unit": "mm"},
                "layer": "F.Cu",
                "width": 0.25,
                "net": "SIGNAL_1",
            }
        ),
    )
    dump(
        "add_via",
        routing.add_via(
            {
                "position": {"x": 25, "y": 25, "unit": "mm"},
                "diameter": 0.8,
                "drill": 0.4,
                "net": "SIGNAL_1",
                "type": "through",
            }
        ),
    )
    dump("get_nets_list", routing.get_nets_list({}))

    # Step 6 — Design rules
    drc = DesignRuleCommands(board)
    dump("get_design_rules", drc.get_design_rules({}))

    # Step 7 — Save & summarize
    pcbnew.SaveBoard(str(BOARD_PATH), board)
    print(f"\n✓ Saved board to {BOARD_PATH} ({BOARD_PATH.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
