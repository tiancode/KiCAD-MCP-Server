"""Persistence tests for net classes, design rules and net-class membership.

These verify the fix for the bug where ``create_netclass``,
``set_design_rules`` and ``assign_net_to_class`` returned ``success: true`` but
never wrote anything to disk: in KiCad 9/10 net classes, membership patterns and
design-rule minimums live in the ``.kicad_pro`` project JSON, not the
``.kicad_pcb`` board object.

Two layers of coverage:

* **Pure helper tests** (no pcbnew) exercise the ``.kicad_pro`` read-modify-write
  directly against a *synthesized* KiCad-10-shaped project fixture — these always
  run.
* **Integration tests** (``@pytest.mark.integration``) drive the real command
  classes against a real ``pcbnew.BOARD`` and assert the change lands on disk.
  Because ``conftest`` stubs ``pcbnew`` in-process (and importing the real SWIG
  ``pcbnew`` into the pytest process would poison other tests via SWIG global
  state), each integration case runs in a fresh subprocess using
  ``sys.executable`` — under the project venv that interpreter carries the real
  ``pcbnew``.  They skip automatically when real pcbnew isn't importable
  (headless / CI without KiCad).

Both layers build everything under ``tmp_path`` — nothing outside the test tree
is ever read or written.  This replaces the previous version, which hard-skipped
everything unless a personal ``channel_distributor.kicad_pro`` from the original
developer's Linux box happened to be present.
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).parent.parent / "python"


# --------------------------------------------------------------------------
# Synthesized KiCad-10-shaped .kicad_pro fixture
# --------------------------------------------------------------------------
#
# A minimal-but-valid project: a Default class (track_width 0.25 so the
# formatting round-trip test's Default upsert is a genuine no-op), a Power
# class, empty membership maps, and a design_settings.rules block.  Written with
# the exact same routine ``save_kicad_pro`` uses so the on-disk bytes are
# canonical (2-space indent + trailing newline) — required by
# ``test_helper_preserves_formatting``.
SYNTH_PRO = {
    "board": {
        "design_settings": {
            "rules": {
                "min_clearance": 0.0,
                "min_track_width": 0.0,
                "min_via_diameter": 0.0,
            }
        }
    },
    "net_settings": {
        "classes": [
            {
                "bus_width": 12,
                "clearance": 0.2,
                "diff_pair_gap": 0.25,
                "diff_pair_via_gap": 0.25,
                "diff_pair_width": 0.2,
                "line_style": 0,
                "microvia_diameter": 0.3,
                "microvia_drill": 0.1,
                "name": "Default",
                "pcb_color": "rgba(0, 0, 0, 0.000)",
                "priority": 2147483647,
                "schematic_color": "rgba(0, 0, 0, 0.000)",
                "track_width": 0.25,
                "via_diameter": 0.6,
                "via_drill": 0.3,
                "wire_width": 6,
            },
            {
                "bus_width": 12,
                "clearance": 0.25,
                "diff_pair_gap": 0.25,
                "diff_pair_via_gap": 0.25,
                "diff_pair_width": 0.2,
                "line_style": 0,
                "microvia_diameter": 0.3,
                "microvia_drill": 0.1,
                "name": "Power",
                "pcb_color": "rgba(0, 0, 0, 0.000)",
                "priority": 0,
                "schematic_color": "rgba(0, 0, 0, 0.000)",
                "track_width": 0.5,
                "via_diameter": 0.8,
                "via_drill": 0.4,
                "wire_width": 6,
            },
        ],
        "netclass_assignments": None,
        "netclass_patterns": [],
    },
}


def _write_synth_pro(path: Path) -> None:
    """Write the synthesized project fixture with canonical KiCad-10 formatting.

    Uses the same serialization ``utils.kicad_pro.save_kicad_pro`` uses
    (indent=2 spaces, ``ensure_ascii=False``, trailing newline) so the bytes are
    byte-for-byte what a read-modify-write no-op would reproduce."""
    with path.open("w", encoding="utf-8") as f:
        json.dump(SYNTH_PRO, f, indent="  ", ensure_ascii=False)
        f.write("\n")


@pytest.fixture
def temp_project(tmp_path):
    """Write the synthesized .kicad_pro into tmp_path; return tmp_path."""
    _write_synth_pro(tmp_path / "channel_distributor.kicad_pro")
    return tmp_path


# --------------------------------------------------------------------------
# Pure helper tests (no pcbnew required)
# --------------------------------------------------------------------------


def test_helper_upsert_netclass_persists(temp_project):
    sys.path.insert(0, str(PYTHON_DIR))
    from utils import kicad_pro

    pro = temp_project / "channel_distributor.kicad_pro"
    data, indent = kicad_pro.load_kicad_pro(str(pro))
    ns = kicad_pro._net_settings(data)

    kicad_pro.upsert_netclass(ns, "HighSpeed", {"track_width": 0.35, "clearance": 0.3})
    kicad_pro.save_kicad_pro(str(pro), data, indent)

    # Re-read from disk to prove persistence.
    reread = json.loads(pro.read_text())
    by_name = {c["name"]: c for c in reread["net_settings"]["classes"]}
    assert "HighSpeed" in by_name
    assert by_name["HighSpeed"]["track_width"] == 0.35
    assert by_name["HighSpeed"]["clearance"] == 0.3
    # Template fields copied from Default must be present (valid class).
    assert "priority" in by_name["HighSpeed"]
    assert "via_diameter" in by_name["HighSpeed"]
    # Existing classes preserved.
    assert "Default" in by_name and "Power" in by_name


def test_helper_upsert_existing_class_updates_in_place(temp_project):
    sys.path.insert(0, str(PYTHON_DIR))
    from utils import kicad_pro

    pro = temp_project / "channel_distributor.kicad_pro"
    data, indent = kicad_pro.load_kicad_pro(str(pro))
    ns = kicad_pro._net_settings(data)
    before = len(ns["classes"])

    kicad_pro.upsert_netclass(ns, "Power", {"track_width": 0.75})
    kicad_pro.save_kicad_pro(str(pro), data, indent)

    reread = json.loads(pro.read_text())
    by_name = {c["name"]: c for c in reread["net_settings"]["classes"]}
    assert len(reread["net_settings"]["classes"]) == before  # no duplicate
    assert by_name["Power"]["track_width"] == 0.75


def test_helper_assign_and_pattern_persist(temp_project):
    sys.path.insert(0, str(PYTHON_DIR))
    from utils import kicad_pro

    pro = temp_project / "channel_distributor.kicad_pro"
    data, indent = kicad_pro.load_kicad_pro(str(pro))
    ns = kicad_pro._net_settings(data)

    kicad_pro.assign_net_to_class(ns, "/2_MCU/SWDIO", "Analog")
    added = kicad_pro.add_netclass_pattern(ns, "Power", "+5V_*")
    assert added is True
    # Idempotent.
    assert kicad_pro.add_netclass_pattern(ns, "Power", "+5V_*") is False
    kicad_pro.save_kicad_pro(str(pro), data, indent)

    reread = json.loads(pro.read_text())
    assert reread["net_settings"]["netclass_assignments"]["/2_MCU/SWDIO"] == "Analog"
    assert {"netclass": "Power", "pattern": "+5V_*"} in reread["net_settings"]["netclass_patterns"]


def test_helper_preserves_formatting(temp_project):
    """The rewrite must keep KiCad's indent + trailing newline (minimal diff)."""
    sys.path.insert(0, str(PYTHON_DIR))
    from utils import kicad_pro

    pro = temp_project / "channel_distributor.kicad_pro"
    original = pro.read_text()
    indent_unit = kicad_pro._detect_indent(original)
    # This KiCad 10-shaped file uses 2-space indent.
    assert indent_unit == "  "

    data, indent = kicad_pro.load_kicad_pro(str(pro))
    kicad_pro.upsert_netclass(data["net_settings"], "Default", {"track_width": 0.25})
    kicad_pro.save_kicad_pro(str(pro), data, indent)

    rewritten = pro.read_text()
    assert rewritten.endswith("}\n")
    assert "\t" not in rewritten  # stayed space-indented
    # Setting Default.track_width to its current value is a no-op -> identical file.
    assert rewritten == original


# --------------------------------------------------------------------------
# P1: create_netclass `nets` array persistence (stubbed board, no pcbnew)
# --------------------------------------------------------------------------
#
# create_netclass's inline `nets` loop used to call NETINFO_ITEM.SetClass(),
# a SWIG method that does not exist in KiCad 10 — so passing `nets` threw
# "'NETINFO_ITEM' object has no attribute 'SetClass'" and failed the whole
# call.  The fix persists the membership to the .kicad_pro (the same mechanism
# assign_net_to_class uses) and makes the in-memory SWIG mirror best-effort.
# These reproduce the KiCad-10 shape (a net object lacking SetClass, and one
# whose SetClass raises) without real pcbnew.


def _fake_board_with_nets(pcb_path, net_names, net_factory):
    """MagicMock board whose NetsByName() returns net objects from net_factory."""
    from unittest.mock import MagicMock

    class _NetsMap:
        def __init__(self):
            self._nets = {n: net_factory() for n in net_names}

        def has_key(self, name):  # SWIG map API
            return name in self._nets

        def __getitem__(self, name):
            return self._nets[name]

    board = MagicMock()
    board.GetFileName.return_value = str(pcb_path)
    board.GetNetInfo.return_value.NetsByName.return_value = _NetsMap()
    return board


def test_create_netclass_with_nets_persists_when_setclass_absent(temp_project):
    """P1: a net object with NO SetClass (KiCad 10) must not fail the call —
    the assignment persists to netclass_assignments instead."""
    sys.path.insert(0, str(PYTHON_DIR))
    from commands.routing import RoutingCommands

    pro = temp_project / "channel_distributor.kicad_pro"
    pcb = temp_project / "channel_distributor.kicad_pcb"

    class _NetNoSetClass:  # mimics KiCad-10 NETINFO_ITEM: no SetClass attribute
        pass

    board = _fake_board_with_nets(pcb, ["GND"], _NetNoSetClass)
    res = RoutingCommands(board).create_netclass(
        {"name": "RailClass", "traceWidth": 0.6, "nets": ["GND"]}
    )
    assert res["success"] is True, res
    assert res["persisted"] is True, res

    data = json.loads(pro.read_text())
    assert data["net_settings"]["netclass_assignments"]["GND"] == "RailClass"
    by = {c["name"]: c for c in data["net_settings"]["classes"]}
    assert by["RailClass"]["track_width"] == 0.6


def test_create_netclass_with_nets_persists_when_setclass_raises(temp_project):
    """P1: even a net whose SetClass *raises* must not fail the call."""
    sys.path.insert(0, str(PYTHON_DIR))
    from commands.routing import RoutingCommands

    pro = temp_project / "channel_distributor.kicad_pro"
    pcb = temp_project / "channel_distributor.kicad_pcb"

    class _NetRaises:
        def SetClass(self, _):
            raise AttributeError("'NETINFO_ITEM' object has no attribute 'SetClass'")

    board = _fake_board_with_nets(pcb, ["GND"], _NetRaises)
    res = RoutingCommands(board).create_netclass(
        {"name": "RailClass", "nets": ["GND"]}
    )
    assert res["success"] is True, res
    data = json.loads(pro.read_text())
    assert data["net_settings"]["netclass_assignments"]["GND"] == "RailClass"


# --------------------------------------------------------------------------
# Integration tests (real pcbnew + real command classes, via subprocess)
# --------------------------------------------------------------------------
#
# The driver builds a scratch board carrying one real named net ("GND", kept
# alive by a pad so it survives save/reload), writes the synthesized .kicad_pro
# as its sibling, then runs ONE persistence operation through the real command
# class and asserts the change landed in the project JSON.  Runs in a fresh
# interpreter so the real SWIG pcbnew never enters the pytest process.

_DRIVER = textwrap.dedent(
    '''
    import json, sys
    from pathlib import Path

    tmp = Path(sys.argv[1])
    python_dir = sys.argv[2]
    op = sys.argv[3]
    sys.path.insert(0, python_dir)

    import pcbnew

    pcb = tmp / "proj.kicad_pcb"
    pro = tmp / "proj.kicad_pro"          # already written by the test

    # Build a board with a real, named net kept alive by a pad, then save/reload
    # so the net is a genuine persisted board net (unconnected nets get pruned).
    board = pcbnew.BOARD()
    board.SetFileName(str(pcb))
    net = pcbnew.NETINFO_ITEM(board, "GND")
    board.Add(net)
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("R1")
    board.Add(fp)
    pad = pcbnew.PAD(fp)
    pad.SetNumber("1")
    pad.SetNet(net)
    fp.Add(pad)
    board.Save(str(pcb))
    board = pcbnew.LoadBoard(str(pcb))

    def a_net_name():
        info = board.GetNetInfo()
        for code in range(info.GetNetCount()):
            n = info.GetNetItem(code)
            if n and n.GetNetname():
                return n.GetNetname()
        raise AssertionError("board has no nets to test with")

    if op == "create_netclass":
        from commands.routing import RoutingCommands
        res = RoutingCommands(board).create_netclass({
            "name": "RF", "traceWidth": 0.4, "clearance": 0.3,
            "viaDiameter": 0.7, "viaDrill": 0.35, "patterns": ["*RF_*"],
        })
        assert res["success"] is True, res
        assert res["persisted"] is True, res
        data = json.loads(pro.read_text())
        by = {c["name"]: c for c in data["net_settings"]["classes"]}
        assert "RF" in by, by.keys()
        assert by["RF"]["track_width"] == 0.4, by["RF"]
        assert by["RF"]["clearance"] == 0.3, by["RF"]
        assert by["RF"]["via_diameter"] == 0.7, by["RF"]
        assert {"netclass": "RF", "pattern": "*RF_*"} in data["net_settings"]["netclass_patterns"]

    elif op == "create_netclass_with_nets":
        # P1: passing `nets` must NOT throw on real KiCad-10 pcbnew (whose
        # NETINFO_ITEM has no SetClass) and must persist netclass_assignments.
        from commands.routing import RoutingCommands
        name = a_net_name()
        res = RoutingCommands(board).create_netclass({
            "name": "Rail", "traceWidth": 0.5, "clearance": 0.25,
            "viaDiameter": 0.8, "viaDrill": 0.4, "nets": [name],
        })
        assert res["success"] is True, res
        assert res["persisted"] is True, res
        data = json.loads(pro.read_text())
        by = {c["name"]: c for c in data["net_settings"]["classes"]}
        assert "Rail" in by, by.keys()
        assert by["Rail"]["track_width"] == 0.5, by["Rail"]
        assert data["net_settings"]["netclass_assignments"][name] == "Rail", data["net_settings"]

    elif op == "assign_net":
        from commands.routing import RoutingCommands
        name = a_net_name()
        res = RoutingCommands(board).assign_net_to_class({"net": name, "netClass": "Power"})
        assert res["success"] is True, res
        assert res["persisted"] is True, res
        data = json.loads(pro.read_text())
        assert data["net_settings"]["netclass_assignments"][name] == "Power", data["net_settings"]

    elif op == "assign_pattern":
        from commands.routing import RoutingCommands
        res = RoutingCommands(board).assign_netclass_pattern(
            {"netClass": "Analog", "pattern": "*AIN?_*"})
        assert res["success"] is True, res
        assert res["persisted"] is True, res
        assert res["added"] is True, res
        data = json.loads(pro.read_text())
        assert {"netclass": "Analog", "pattern": "*AIN?_*"} in data["net_settings"]["netclass_patterns"]

    elif op == "design_rules":
        from commands.design_rules import DesignRuleCommands
        res = DesignRuleCommands(board).set_design_rules({
            "clearance": 0.18, "minTrackWidth": 0.12, "minViaDiameter": 0.5, "trackWidth": 0.3,
        })
        assert res["success"] is True, res
        assert res["persisted"] is True, res
        data = json.loads(pro.read_text())
        rules = data["board"]["design_settings"]["rules"]
        assert rules["min_clearance"] == 0.18, rules
        assert rules["min_track_width"] == 0.12, rules
        assert rules["min_via_diameter"] == 0.5, rules
        by = {c["name"]: c for c in data["net_settings"]["classes"]}
        assert by["Default"]["track_width"] == 0.3, by["Default"]

    else:
        raise SystemExit("unknown op: " + op)

    print("INTEGRATION-OK")
    '''
)


def _real_pcbnew_subprocess_available() -> bool:
    """True when ``sys.executable`` can import the real SWIG pcbnew."""
    try:
        probe = subprocess.run(
            [sys.executable, "-c", "import pcbnew; assert isinstance(pcbnew.BOARD, type)"],
            capture_output=True,
            timeout=120,
        )
    except Exception:
        return False
    return probe.returncode == 0


requires_real_pcbnew = pytest.mark.skipif(
    not _real_pcbnew_subprocess_available(),
    reason="real pcbnew not importable by sys.executable (headless / CI without KiCad)",
)


def _run_driver(tmp_path: Path, op: str) -> None:
    _write_synth_pro(tmp_path / "proj.kicad_pro")
    script = tmp_path / "driver.py"
    script.write_text(_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path), str(PYTHON_DIR), op],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "INTEGRATION-OK" in result.stdout, f"stdout={result.stdout}\nstderr={result.stderr}"


@pytest.mark.integration
@requires_real_pcbnew
def test_create_netclass_persists_to_disk(tmp_path):
    _run_driver(tmp_path, "create_netclass")


@pytest.mark.integration
@requires_real_pcbnew
def test_create_netclass_with_nets_persists_to_disk(tmp_path):
    _run_driver(tmp_path, "create_netclass_with_nets")


@pytest.mark.integration
@requires_real_pcbnew
def test_assign_net_to_class_persists_to_disk(tmp_path):
    _run_driver(tmp_path, "assign_net")


@pytest.mark.integration
@requires_real_pcbnew
def test_assign_netclass_pattern_persists_to_disk(tmp_path):
    _run_driver(tmp_path, "assign_pattern")


@pytest.mark.integration
@requires_real_pcbnew
def test_set_design_rules_persists_to_disk(tmp_path):
    _run_driver(tmp_path, "design_rules")
