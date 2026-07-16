"""Re-implemented component tools (E2E round 7): add_component_annotation,
group_components, replace_component.

These three tools were once registered on the TS surface with no Python backend
(every call returned "Unknown command") and were removed; they are now
implemented for real on the SWIG path
(commands/component/_annotate_group_replace.py) and re-registered.

Behaviour is exercised against a REAL pcbnew 10 board with stock footprints
loaded from KiCad's SharedSupport libraries.  Because tests/conftest.py installs
a MagicMock pcbnew stub in-process, the real board work runs in a subprocess
(sys.executable == the venv python, which has the real module).  The whole file
skips cleanly when the SharedSupport footprints or a real pcbnew are absent.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
PYTHON_DIR = REPO / "python"
FP_DIR = Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints")

_REQUIRED_FPS = [
    FP_DIR / "Resistor_SMD.pretty" / "R_0603_1608Metric.kicad_mod",
    FP_DIR / "Resistor_SMD.pretty" / "R_0805_2012Metric.kicad_mod",
    FP_DIR / "Package_DIP.pretty" / "DIP-8_W7.62mm.kicad_mod",
]


def _real_pcbnew_available() -> bool:
    try:
        probe = subprocess.run(
            [sys.executable, "-c", "import pcbnew; pcbnew.BOARD()"],
            capture_output=True,
            timeout=120,
        )
    except Exception:
        return False
    return probe.returncode == 0


_HAVE_FPS = all(p.exists() for p in _REQUIRED_FPS)

pytestmark = pytest.mark.skipif(
    not (_HAVE_FPS and _real_pcbnew_available()),
    reason="requires real pcbnew and KiCad SharedSupport stock footprints",
)


# ---------------------------------------------------------------------------
# Subprocess driver — runs every scenario against a real board once and prints
# a single JSON blob the tests below assert against.
# ---------------------------------------------------------------------------
_DRIVER = r"""
import sys, json
PYTHON_DIR, FP = sys.argv[1], sys.argv[2]
sys.path.insert(0, PYTHON_DIR)
import pcbnew
from commands.library import LibraryManager
from commands.component import ComponentCommands

def nc():
    b = pcbnew.BOARD()
    lm = LibraryManager()
    lm.libraries = {
        "Resistor_SMD": FP + "/Resistor_SMD.pretty",
        "Package_DIP": FP + "/Package_DIP.pretty",
    }
    return ComponentCommands(board=b, library_manager=lm), b

def load(b, lib, name, ref, x_mm, flip=False, rot=None):
    m = pcbnew.FootprintLoad(FP + "/" + lib + ".pretty", name)
    m.SetReference(ref)
    m.SetPosition(pcbnew.VECTOR2I(int(x_mm * 1e6), 0))
    b.Add(m)
    if flip:
        m.Flip(m.GetPosition(), False)
    if rot is not None:
        m.SetOrientation(pcbnew.EDA_ANGLE(rot, pcbnew.DEGREES_T))
    return m

def assign_net(b, fp, num, name):
    n = pcbnew.NETINFO_ITEM(b, name)
    b.Add(n)
    for p in fp.Pads():
        if p.GetNumber() == num:
            p.SetNet(n)

def pos(fp):
    p = fp.GetPosition()
    return [round(p.x / 1e6, 4), round(p.y / 1e6, 4)]

def layer(b, fp):
    return b.GetLayerName(fp.GetLayer())

def texts(b):
    out = []
    for d in b.GetDrawings():
        if d.GetClass() == "PCB_TEXT":
            out.append({"text": d.GetText(), "layer": b.GetLayerName(d.GetLayer()),
                        "pos": [round(d.GetPosition().x / 1e6, 4), round(d.GetPosition().y / 1e6, 4)]})
    return out

def groups(b):
    return [{"name": g.GetName(),
             "members": sorted(it.GetReference() for it in g.GetItems() if hasattr(it, "GetReference"))}
            for g in b.Groups()]

def padnets(fp):
    return [{"num": p.GetNumber(), "net": p.GetNetname()} for p in fp.Pads()]

R = {}

# annotation with explicit offset (component at x=10mm)
c, b = nc(); load(b, "Resistor_SMD", "R_0603_1608Metric", "R1", 10)
R["annot_offset"] = {
    "resp": c.add_component_annotation({"reference": "R1", "text": "CHK",
                                        "offset": {"x": 1, "y": 2, "unit": "mm"}}),
    "texts": texts(b),
    "comp_pos": pos(b.FindFootprintByReference("R1")),
}

# default offset + legacy layer name
c, b = nc(); load(b, "Resistor_SMD", "R_0603_1608Metric", "R1", 10)
R["annot_default"] = {"resp": c.add_component_annotation({"reference": "R1", "text": "D", "layer": "F.SilkS"}),
                      "texts": texts(b)}

# unknown ref -> refuse, add nothing
c, b = nc(); load(b, "Resistor_SMD", "R_0603_1608Metric", "R1", 10)
R["annot_unknown"] = {"resp": c.add_component_annotation({"reference": "NOPE", "text": "x"}),
                      "text_count": len(texts(b))}

# invalid layer -> VALIDATION
c, b = nc(); load(b, "Resistor_SMD", "R_0603_1608Metric", "R1", 10)
R["annot_badlayer"] = {"resp": c.add_component_annotation({"reference": "R1", "text": "x", "layer": "Nope.Layer"})}

# group ok
c, b = nc(); load(b, "Resistor_SMD", "R_0603_1608Metric", "R1", 10); load(b, "Resistor_SMD", "R_0603_1608Metric", "R2", 20)
R["group_ok"] = {"resp": c.group_components({"references": ["R1", "R2"], "groupName": "grp"}), "groups": groups(b)}

# group unknown ref -> refuse, no partial group
c, b = nc(); load(b, "Resistor_SMD", "R_0603_1608Metric", "R1", 10); load(b, "Resistor_SMD", "R_0603_1608Metric", "R2", 20)
R["group_unknown"] = {"resp": c.group_components({"references": ["R1", "NOPE"], "groupName": "grp"}), "groups": groups(b)}

# regroup: member moves, source keeps its other member
c, b = nc()
for i, x in (("R1", 10), ("R2", 20), ("R3", 30)):
    load(b, "Resistor_SMD", "R_0603_1608Metric", i, x)
c.group_components({"references": ["R1", "R2"], "groupName": "A"})
R["group_regroup"] = {"resp": c.group_components({"references": ["R1", "R3"], "groupName": "B"}), "groups": groups(b)}

# regroup that empties the source group -> source removed
c, b = nc(); load(b, "Resistor_SMD", "R_0603_1608Metric", "R1", 10)
c.group_components({"references": ["R1"], "groupName": "A"})
R["group_empty_removal"] = {"resp": c.group_components({"references": ["R1"], "groupName": "B"}), "groups": groups(b)}

# replace: matching pads keep nets; position/rotation/layer preserved
c, b = nc(); r1 = load(b, "Resistor_SMD", "R_0603_1608Metric", "R1", 10, rot=45)
assign_net(b, r1, "1", "NetA"); assign_net(b, r1, "2", "NetB")
pre = {"pos": pos(r1), "rot": r1.GetOrientation().AsDegrees(), "layer": layer(b, r1)}
resp = c.replace_component({"reference": "R1", "newFootprint": "Resistor_SMD:R_0805_2012Metric"})
nr = b.FindFootprintByReference("R1")
R["replace_ok"] = {"resp": resp, "pre": pre,
                   "new_fpid": nr.GetFPIDAsString(), "pos": pos(nr),
                   "rot": nr.GetOrientation().AsDegrees(), "layer": layer(b, nr),
                   "padnets": padnets(nr)}

# replace: flipped DIP-8 -> R_0603; side + rotation preserved, unmatched reported
c, b = nc(); u = load(b, "Package_DIP", "DIP-8_W7.62mm", "U1", 30, flip=True, rot=90)
assign_net(b, u, "1", "N1"); assign_net(b, u, "3", "N3")
resp = c.replace_component({"reference": "U1", "newFootprint": "Resistor_SMD:R_0603_1608Metric"})
nu = b.FindFootprintByReference("U1")
R["replace_unmatched"] = {"resp": resp, "layer": layer(b, nu),
                          "rot": nu.GetOrientation().AsDegrees(), "padnets": padnets(nu)}

# replace: newValue applied
c, b = nc(); r = load(b, "Resistor_SMD", "R_0603_1608Metric", "R1", 10); r.SetValue("10k")
R["replace_value"] = {"resp": c.replace_component({"reference": "R1",
                      "newFootprint": "Resistor_SMD:R_0805_2012Metric", "newValue": "22k"})}

# replace: unknown ref / footprint -> truthful refusals, old part untouched
c, b = nc(); load(b, "Resistor_SMD", "R_0603_1608Metric", "R1", 10)
R["replace_unknown_ref"] = {"resp": c.replace_component({"reference": "ZZ",
                            "newFootprint": "Resistor_SMD:R_0603_1608Metric"})}
R["replace_unknown_fp"] = {"resp": c.replace_component({"reference": "R1", "newFootprint": "NoLib:NoFp"}),
                           "still_fpid": b.FindFootprintByReference("R1").GetFPIDAsString()}

print("RESULTS:" + json.dumps(R))
"""


@pytest.fixture(scope="module")
def results():
    proc = subprocess.run(
        [sys.executable, "-c", _DRIVER, str(PYTHON_DIR), str(FP_DIR)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    marker = "RESULTS:"
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith(marker)), None)
    assert (
        line is not None
    ), f"driver produced no results\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return json.loads(line[len(marker) :])


# ----------------------------- add_component_annotation --------------------- #
@pytest.mark.integration
def test_annotation_lands_on_layer_near_component(results):
    r = results["annot_offset"]
    resp = r["resp"]
    assert resp["success"] is True
    assert resp["annotation"]["layer"] == "F.Silkscreen"
    # offset {1,2} mm from the component at (10, 0) -> (11, 2)
    cx, cy = r["comp_pos"]
    assert resp["annotation"]["position"]["x"] == pytest.approx(cx + 1, abs=1e-3)
    assert resp["annotation"]["position"]["y"] == pytest.approx(cy + 2, abs=1e-3)
    # a real PCB_TEXT landed on the board at that spot / layer
    assert len(r["texts"]) == 1
    t = r["texts"][0]
    assert t["text"] == "CHK"
    assert t["layer"] == "F.Silkscreen"
    assert t["pos"] == [pytest.approx(cx + 1, abs=1e-3), pytest.approx(cy + 2, abs=1e-3)]


@pytest.mark.integration
def test_annotation_default_offset_and_legacy_layer(results):
    r = results["annot_default"]
    resp = r["resp"]
    assert resp["success"] is True
    # legacy short name resolves to the canonical KiCad 10 layer
    assert resp["annotation"]["layer"] == "F.SilkS"
    assert r["texts"][0]["layer"] == "F.Silkscreen"
    # no offset -> text sits at the component origin (10, 0)
    assert resp["annotation"]["position"]["x"] == pytest.approx(10, abs=1e-3)
    assert resp["annotation"]["position"]["y"] == pytest.approx(0, abs=1e-3)


@pytest.mark.integration
def test_annotation_unknown_ref_refuses(results):
    r = results["annot_unknown"]
    assert r["resp"]["success"] is False
    assert r["resp"]["errorCode"] == "COMPONENT_NOT_FOUND"
    assert r["text_count"] == 0  # nothing added


@pytest.mark.integration
def test_annotation_invalid_layer_refuses(results):
    resp = results["annot_badlayer"]["resp"]
    assert resp["success"] is False
    assert resp["errorCode"] == "VALIDATION"


# --------------------------------- group_components ------------------------- #
@pytest.mark.integration
def test_group_exists_with_name_and_members(results):
    r = results["group_ok"]
    assert r["resp"]["success"] is True
    assert r["resp"]["group"]["name"] == "grp"
    assert r["resp"]["group"]["memberCount"] == 2
    assert r["groups"] == [{"name": "grp", "members": ["R1", "R2"]}]


@pytest.mark.integration
def test_group_unknown_ref_refuses_without_partial(results):
    r = results["group_unknown"]
    assert r["resp"]["success"] is False
    assert r["resp"]["errorCode"] == "COMPONENT_NOT_FOUND"
    assert r["resp"]["missing"] == ["NOPE"]
    assert r["groups"] == []  # no partial group created


@pytest.mark.integration
def test_group_regroup_moves_member_deterministically(results):
    r = results["group_regroup"]
    assert r["resp"]["success"] is True
    assert r["resp"]["reassigned"] == [{"reference": "R1", "fromGroup": "A"}]
    by_name = {g["name"]: g["members"] for g in r["groups"]}
    assert by_name["A"] == ["R2"]  # source keeps its remaining member
    assert by_name["B"] == ["R1", "R3"]


@pytest.mark.integration
def test_group_regroup_removes_emptied_source(results):
    r = results["group_empty_removal"]
    assert r["resp"]["success"] is True
    assert r["resp"]["removedEmptyGroups"] == ["A"]
    assert r["groups"] == [{"name": "B", "members": ["R1"]}]


# ------------------------------- replace_component -------------------------- #
@pytest.mark.integration
def test_replace_swaps_footprint_preserving_geometry_and_nets(results):
    r = results["replace_ok"]
    assert r["resp"]["success"] is True
    # The re-stamped FPID must keep the library nickname (round-7 live-smoke
    # finding: FootprintLoad alone yields a bare, unresolvable item name).
    assert r["new_fpid"] == "Resistor_SMD:R_0805_2012Metric"
    # reference / position / rotation / layer preserved
    assert r["pos"] == r["pre"]["pos"]
    assert r["rot"] == pytest.approx(r["pre"]["rot"])
    assert r["layer"] == r["pre"]["layer"] == "F.Cu"
    # matching pad nets carried across
    nets = {p["num"]: p["net"] for p in r["padnets"]}
    assert nets == {"1": "NetA", "2": "NetB"}
    assert sorted(r["resp"]["padMatch"]["matched"]) == ["1", "2"]
    assert r["resp"]["padMatch"]["droppedNets"] == []


@pytest.mark.integration
def test_replace_preserves_side_rotation_and_reports_unmatched(results):
    r = results["replace_unmatched"]
    assert r["resp"]["success"] is True
    assert r["layer"] == "B.Cu"  # flipped side preserved
    assert r["rot"] == pytest.approx(90.0)
    pm = r["resp"]["padMatch"]
    assert pm["matched"] == ["1"]  # only pad 1 exists on both
    assert "2" in pm["unmatchedNewPads"]  # new pad 2 got no net
    dropped = {d["pad"]: d["net"] for d in pm["droppedNets"]}
    assert dropped == {"3": "N3"}  # DIP pad 3's net could not be carried over
    nets = {p["num"]: p["net"] for p in r["padnets"]}
    assert nets["1"] == "N1"


@pytest.mark.integration
def test_replace_applies_new_value(results):
    resp = results["replace_value"]["resp"]
    assert resp["success"] is True
    assert resp["component"]["value"] == "22k"


@pytest.mark.integration
def test_replace_unknown_ref_refuses(results):
    resp = results["replace_unknown_ref"]["resp"]
    assert resp["success"] is False
    assert resp["errorCode"] == "COMPONENT_NOT_FOUND"


@pytest.mark.integration
def test_replace_unknown_footprint_refuses_and_leaves_part(results):
    r = results["replace_unknown_fp"]
    assert r["resp"]["success"] is False
    assert r["resp"]["errorCode"] == "FOOTPRINT_NOT_FOUND"
    # old part untouched after the failed swap (FootprintLoad stores the bare
    # footprint name, no library nickname)
    assert r["still_fpid"] == "R_0603_1608Metric"
