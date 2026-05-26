"""
Full MCP-functionality integration test: builds a real KiCAD project from
scratch (5 V → 3.3 V AMS1117 LDO regulator with indicator LED) by driving
the same `KiCADInterface.handle_command(name, params)` entry point the
MCP server uses.

Run on a KiCAD 10 Flatpak install:

    flatpak run --command=python3 \\
        --filesystem=/tmp:rw \\
        --filesystem=$(pwd):ro \\
        --filesystem=/var/lib/flatpak:ro \\
        org.kicad.KiCad scripts/full_mcp_integration_test.py

Or on a native install (pcbnew importable from host Python):

    python3 scripts/full_mcp_integration_test.py

Output lands at $MCP_INTEGRATION_DIR (default /tmp/mcp-integration/).
The test prints PASS/FAIL per step and exits non-zero on any failure.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from typing import Any, Callable, Dict
from unittest.mock import MagicMock

# Mock host-only deps that bundled Pythons may not ship.
for _m in ("colorlog",):
    sys.modules.setdefault(_m, MagicMock(name=_m))
for _m in ("PIL", "PIL.Image", "cairosvg"):
    try:
        __import__(_m)
    except ImportError:
        sys.modules[_m] = MagicMock(name=_m)

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "python"))
os.environ.setdefault("KICAD_BACKEND", "swig")

OUT_DIR = Path(os.environ.get("MCP_INTEGRATION_DIR", "/tmp/mcp-integration"))
OUT_DIR.mkdir(parents=True, exist_ok=True)
PROJECT_NAME = "ldo_regulator_3v3"
PROJECT_PRO = OUT_DIR / f"{PROJECT_NAME}.kicad_pro"
PROJECT_PCB = OUT_DIR / f"{PROJECT_NAME}.kicad_pcb"
PROJECT_SCH = OUT_DIR / f"{PROJECT_NAME}.kicad_sch"


RESULTS: list[tuple[str, bool, str]] = []


def section(title: str) -> None:
    print(f"\n{'═' * 78}\n {title}\n{'═' * 78}")


def call(iface: Any, command: str, params: Dict[str, Any], label: str | None = None) -> Any:
    """Invoke the MCP command and record the outcome."""
    lbl = label or f"{command} {json.dumps(params, default=str)[:60]}"
    try:
        result = iface.handle_command(command, params)
    except Exception as e:  # noqa: BLE001 — integration harness
        msg = f"{type(e).__name__}: {e}"
        RESULTS.append((lbl, False, msg))
        print(f"  ✗ {lbl:60s} — RAISED {msg[:60]}")
        return None

    ok = bool(result.get("success", False)) if isinstance(result, dict) else True
    msg = (result.get("message") if isinstance(result, dict) else None) or "ok"
    RESULTS.append((lbl, ok, msg))
    marker = "✓" if ok else "✗"
    print(f"  {marker} {lbl:60s} — {msg[:60]}")
    return result


SCHEMATIC_COMPONENTS = [
    # (ref, lib, symbol, value, footprint, x, y, rotation)
    (
        "J1",
        "Connector",
        "Conn_01x02_Pin",
        "5V_IN",
        "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
        30,
        50,
        180,
    ),
    ("C1", "Device", "C", "10uF", "Capacitor_SMD:C_0603_1608Metric", 60, 50, 0),
    (
        "U1",
        "Regulator_Linear",
        "AMS1117-3.3",
        "AMS1117-3.3",
        "Package_TO_SOT_SMD:SOT-223-3_TabPin2",
        100,
        50,
        0,
    ),
    ("C2", "Device", "C", "10uF", "Capacitor_SMD:C_0603_1608Metric", 140, 50, 0),
    ("R1", "Device", "R", "1k", "Resistor_SMD:R_0603_1608Metric", 170, 50, 90),
    ("D1", "Device", "LED", "RED", "LED_SMD:LED_0603_1608Metric", 170, 80, 0),
    (
        "J2",
        "Connector",
        "Conn_01x02_Pin",
        "3V3_OUT",
        "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
        200,
        50,
        0,
    ),
]


PCB_PLACEMENTS = [
    # ref, x, y, rotation
    ("J1", 6, 20, 0),
    ("C1", 16, 15, 0),
    ("U1", 28, 20, 0),
    ("C2", 40, 15, 0),
    ("R1", 46, 20, 90),
    ("D1", 46, 27, 0),
    ("J2", 54, 20, 180),
]


def main() -> int:
    print(f"Output directory: {OUT_DIR}\n")

    # Clean slate
    for f in OUT_DIR.glob(f"{PROJECT_NAME}.*"):
        f.unlink()

    section("Boot KiCADInterface (the MCP-server's dispatcher)")
    import kicad_interface

    iface = kicad_interface.KiCADInterface()
    print(f"  backend: {'IPC' if iface.use_ipc else 'SWIG'}")
    print(f"  command_routes: {len(iface.command_routes)} commands registered")
    print(f"  _HANDLER_MAP   : {len(type(iface)._HANDLER_MAP)} handler-module entries")

    # ────────────────────────────────────────────────────────────────────
    section("Phase 1 — Project + board skeleton")
    call(iface, "create_project", {"name": PROJECT_NAME, "path": str(OUT_DIR)})
    call(iface, "open_project", {"filename": str(PROJECT_PRO)})
    call(iface, "set_board_size", {"width": 60, "height": 40, "unit": "mm"})
    call(
        iface, "add_board_outline", {"shape": "rectangle", "width": 60, "height": 40, "unit": "mm"}
    )
    for ref, (x, y) in [("MH1", (3, 3)), ("MH2", (57, 3)), ("MH3", (3, 37)), ("MH4", (57, 37))]:
        call(
            iface,
            "add_mounting_hole",
            {"position": {"x": x, "y": y, "unit": "mm"}, "diameter": 2.5, "drill": 2.5},
            label=f"add_mounting_hole {ref}",
        )
    call(iface, "get_board_info", {})
    call(iface, "get_layer_list", {})

    # ────────────────────────────────────────────────────────────────────
    section("Phase 2 — Schematic content")
    for ref, lib, sym, value, fp, x, y, rot in SCHEMATIC_COMPONENTS:
        call(
            iface,
            "add_schematic_component",
            {
                "schematicPath": str(PROJECT_SCH),
                "component": {
                    "type": sym,
                    "library": lib,
                    "reference": ref,
                    "value": value,
                    "footprint": fp,  # required for sync_schematic_to_board
                    "x": x,
                    "y": y,
                    "rotation": rot,
                },
            },
            label=f"add_schematic_component {ref} ({lib}:{sym})",
        )

    # Wire each pin to its named net via connect_to_net.  This is the
    # MCP-server's recommended pin-to-net pattern: it creates a wire stub
    # at the pin, places a net label, and adds the wire to the schematic.
    # The net assignments line up with KiCAD's ERC + sync flow.
    PIN_NETS = [
        # (ref, pin, net)
        ("J1", "1", "5V_IN"),
        ("C1", "1", "5V_IN"),
        ("U1", "3", "5V_IN"),
        ("U1", "2", "3V3_OUT"),
        ("C2", "1", "3V3_OUT"),
        ("R1", "1", "3V3_OUT"),
        ("J2", "1", "3V3_OUT"),
        ("R1", "2", "LED_A"),
        ("D1", "1", "LED_A"),
        ("J1", "2", "GND"),
        ("C1", "2", "GND"),
        ("U1", "1", "GND"),
        ("C2", "2", "GND"),
        ("D1", "2", "GND"),
        ("J2", "2", "GND"),
    ]
    for ref, pin, net in PIN_NETS:
        call(
            iface,
            "connect_to_net",
            {
                "schematicPath": str(PROJECT_SCH),
                "componentRef": ref,
                "pinName": pin,
                "netName": net,
            },
            label=f"{ref}.{pin} → {net}",
        )

    # Inventory + analysis on the schematic
    call(iface, "list_schematic_components", {"schematicPath": str(PROJECT_SCH)})
    call(iface, "list_schematic_nets", {"schematicPath": str(PROJECT_SCH)})
    call(iface, "list_schematic_wires", {"schematicPath": str(PROJECT_SCH)})
    call(iface, "list_schematic_labels", {"schematicPath": str(PROJECT_SCH)})
    call(iface, "find_orphaned_wires", {"schematicPath": str(PROJECT_SCH)})
    call(iface, "find_overlapping_elements", {"schematicPath": str(PROJECT_SCH)})

    # ────────────────────────────────────────────────────────────────────
    section("Phase 3 — ERC + netlist")
    call(iface, "run_erc", {"schematicPath": str(PROJECT_SCH)})
    call(iface, "generate_netlist", {"schematicPath": str(PROJECT_SCH)})

    # ────────────────────────────────────────────────────────────────────
    section("Phase 4 — Sync schematic → board")
    call(
        iface,
        "sync_schematic_to_board",
        {"schematicPath": str(PROJECT_SCH), "boardPath": str(PROJECT_PCB)},
    )

    # ────────────────────────────────────────────────────────────────────
    section("Phase 5 — PCB placement + routing")
    call(iface, "get_component_list", {})
    for ref, x, y, rot in PCB_PLACEMENTS:
        call(
            iface,
            "move_component",
            {"reference": ref, "position": {"x": x, "y": y, "unit": "mm"}, "rotation": rot},
            label=f"move_component {ref} → ({x},{y},{rot}°)",
        )

    call(iface, "get_nets_list", {})

    # Add ground pour
    call(
        iface,
        "add_copper_pour",
        {
            "net": "GND",
            "layer": "B.Cu",
            "polygon": [
                {"x": 1, "y": 1, "unit": "mm"},
                {"x": 59, "y": 1, "unit": "mm"},
                {"x": 59, "y": 39, "unit": "mm"},
                {"x": 1, "y": 39, "unit": "mm"},
            ],
            "clearance": 0.25,
        },
    )

    # Couple of power traces
    call(
        iface,
        "route_trace",
        {
            "start": {"x": 6, "y": 20, "unit": "mm"},
            "end": {"x": 28, "y": 20, "unit": "mm"},
            "width": 0.5,
            "layer": "F.Cu",
            "net": "/5V_IN",
        },
        label="route 5V_IN (J1 → U1)",
    )
    call(
        iface,
        "route_trace",
        {
            "start": {"x": 28, "y": 20, "unit": "mm"},
            "end": {"x": 54, "y": 20, "unit": "mm"},
            "width": 0.5,
            "layer": "F.Cu",
            "net": "/3V3_OUT",
        },
        label="route 3V3_OUT (U1 → J2)",
    )
    call(iface, "refill_zones", {})

    # ────────────────────────────────────────────────────────────────────
    section("Phase 6 — DRC + exports")
    call(iface, "get_design_rules", {})
    call(iface, "run_drc", {})

    call(
        iface,
        "export_gerber",
        {"outputDir": str(OUT_DIR / "gerbers"), "layers": ["F.Cu", "B.Cu", "Edge.Cuts"]},
    )
    call(iface, "export_bom", {"outputPath": str(OUT_DIR / "bom.csv"), "format": "CSV"})

    # ────────────────────────────────────────────────────────────────────
    section("Summary")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f"  Steps: {len(RESULTS)}   passed={passed}   failed={failed}")
    if failed:
        print("\n  Failures:")
        for label, ok, msg in RESULTS:
            if not ok:
                print(f"    ✗ {label:60s} {msg[:50]}")

    if PROJECT_PCB.exists():
        print(f"\n  Final board: {PROJECT_PCB} ({PROJECT_PCB.stat().st_size} bytes)")
    if PROJECT_SCH.exists():
        print(f"  Final schematic: {PROJECT_SCH} ({PROJECT_SCH.stat().st_size} bytes)")

    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
