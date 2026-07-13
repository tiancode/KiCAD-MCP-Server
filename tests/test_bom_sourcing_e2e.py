"""End-to-end check for the sourcing-field fix on a REAL KiCad project.

The normal unit suite stubs ``pcbnew`` (see tests/conftest.py), so a genuine
end-to-end that needs the real SWIG bindings + kicad-cli cannot run in-process.
This test therefore spawns a *fresh* Python subprocess (no conftest stubs) that:

  1. copies the READ-ONLY gd32_radio fixture to a temp dir,
  2. runs the real ``sync_schematic_to_board`` (which now propagates the
     schematic's MPN / Manufacturer / "LCSC Part" fields onto the footprints),
  3. exports a BOM requesting those sourcing attributes,
  4. asserts the LCSC/MPN columns are populated.

Gated gracefully: skips when the fixture directory is absent (any machine but
the one that produced it) or when the real ``pcbnew`` cannot be imported
(driver exits 77).
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Absolute fixture path produced on this machine; absent elsewhere → skip.
FIXTURE_DIR = (
    "/private/tmp/claude-501/-Users-deht-Documents-KiCAD-MCP-Server/"
    "fccb9faf-12b2-4bc8-84a2-101eb20376d3/scratchpad/gd32_radio"
)

REPO = Path(__file__).resolve().parent.parent

# Runs in a clean interpreter with the real pcbnew.  Exit codes:
#   0  -> all assertions passed (prints E2E_OK)
#   77 -> environment unavailable (no real pcbnew) -> pytest skip
#   1  -> assertion/logic failure
_DRIVER = r"""
import csv, os, shutil, sys, tempfile
try:
    import pcbnew
except Exception:
    sys.exit(77)

SRC = os.environ["E2E_FIXTURE"]
from kicad_interface import KiCADInterface
from handlers.schematic_io import handle_sync_schematic_to_board
from commands.export import ExportCommands

work = tempfile.mkdtemp(prefix="gd32_e2e_")
dst = os.path.join(work, "gd32_radio")
os.makedirs(dst)
for name in ("gd32_radio.kicad_pcb", "gd32_radio.kicad_sch",
             "gd32_radio.kicad_pro", "gd32_radio.kicad_prl"):
    s = os.path.join(SRC, name)
    if os.path.exists(s):
        shutil.copy2(s, os.path.join(dst, name))
board_path = os.path.join(dst, "gd32_radio.kicad_pcb")
sch_path = os.path.join(dst, "gd32_radio.kicad_sch")

iface = KiCADInterface()
res = handle_sync_schematic_to_board(
    iface, {"boardPath": board_path, "schematicPath": sch_path}
)
assert res.get("success"), res
assert res.get("fields_footprints_updated", 0) > 0, "no footprints got sourcing fields"

board = pcbnew.LoadBoard(board_path)
u1 = None
for fp in board.GetFootprints():
    if fp.GetReference() == "U1":
        u1 = dict(fp.GetFieldsText())
assert u1 is not None, "U1 not on board"
assert u1.get("MPN") == "GD32F103VET6", u1.get("MPN")
assert u1.get("LCSC Part") == "C80215", u1.get("LCSC Part")

out = os.path.join(work, "bom.csv")
bom = ExportCommands(board).export_bom({
    "outputPath": out, "format": "CSV", "groupByValue": True,
    "attributes": ["LCSC", "MPN", "Manufacturer"],
})
assert bom.get("success"), bom
assert bom.get("attributesMissing") == [], bom.get("attributesMissing")
with open(out, newline="") as fh:
    rows = list(csv.DictReader(fh))
assert "LCSC" in rows[0] and "MPN" in rows[0], list(rows[0].keys())
assert any(r.get("LCSC") for r in rows), "no LCSC value in BOM"
assert any(r.get("MPN") for r in rows), "no MPN value in BOM"
shutil.rmtree(work, ignore_errors=True)
print("E2E_OK")
"""


@pytest.mark.integration
def test_sync_propagates_sourcing_fields_into_bom_real_kicad():
    if not os.path.isdir(FIXTURE_DIR):
        pytest.skip(f"real-KiCad fixture not present: {FIXTURE_DIR}")

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO / "python"), str(REPO), env.get("PYTHONPATH", "")]
    )
    env["E2E_FIXTURE"] = FIXTURE_DIR
    # Force the SWIG/file path for a deterministic run that doesn't depend on a
    # running KiCad IPC server.
    env["KICAD_BACKEND"] = "swig"

    proc = subprocess.run(
        [sys.executable, "-c", _DRIVER],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode == 77:
        pytest.skip("real pcbnew unavailable in subprocess")
    assert proc.returncode == 0, (
        f"e2e driver failed (rc={proc.returncode})\n"
        f"STDOUT:\n{proc.stdout[-2000:]}\nSTDERR:\n{proc.stderr[-2000:]}"
    )
    assert "E2E_OK" in proc.stdout, proc.stdout[-2000:]
