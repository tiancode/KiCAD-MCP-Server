"""Persistence tests for net classes, design rules and net-class membership.

These verify the fix for the bug where ``create_netclass``,
``set_design_rules`` and ``assign_net_to_class`` returned ``success: true`` but
never wrote anything to disk: in KiCad 9/10 net classes, membership patterns and
design-rule minimums live in the ``.kicad_pro`` project JSON, not the
``.kicad_pcb`` board object.

Two layers of coverage:

* **Pure helper tests** (no pcbnew) exercise the ``.kicad_pro`` read-modify-write
  directly against a temp copy of the real project — these always run.
* **Integration tests** (``@pytest.mark.integration``) build the real command
  classes against a real ``pcbnew.BOARD`` loaded from a temp copy and assert the
  change lands on disk.  Skipped automatically when real pcbnew is unavailable.

The fixtures always operate on a *copy* under ``tmp_path`` — the user's real
project is never touched.
"""

import json
import shutil
from pathlib import Path

import pytest

# Real-project source (KiCad 10).  Skip everything if it isn't present so the
# suite stays green on other machines / CI.
REAL_PROJECT = Path("/home/tianshuai/Documents/channel_dist_kicad")
REAL_PRO = REAL_PROJECT / "channel_distributor.kicad_pro"
REAL_PCB = REAL_PROJECT / "channel_distributor.kicad_pcb"

pytestmark = pytest.mark.skipif(
    not REAL_PRO.exists(),
    reason="real channel_distributor.kicad_pro not present on this machine",
)


def _real_pcbnew_available() -> bool:
    """True only when a genuine pcbnew (not the conftest MagicMock) is importable."""
    try:
        import pcbnew  # noqa: F401
    except Exception:
        return False
    # conftest stubs pcbnew with a MagicMock; the real module exposes BOARD.
    return isinstance(getattr(pcbnew, "BOARD", None), type)


@pytest.fixture
def temp_project(tmp_path):
    """Copy the real .kicad_pro + .kicad_pcb into tmp_path; return their paths."""
    pro = tmp_path / "channel_distributor.kicad_pro"
    shutil.copy(REAL_PRO, pro)
    if REAL_PCB.exists():
        shutil.copy(REAL_PCB, tmp_path / "channel_distributor.kicad_pcb")
    return tmp_path


# --------------------------------------------------------------------------
# Pure helper tests (no pcbnew required)
# --------------------------------------------------------------------------


def test_helper_upsert_netclass_persists(temp_project):
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
    from utils import kicad_pro

    pro = temp_project / "channel_distributor.kicad_pro"
    original = pro.read_text()
    indent_unit = kicad_pro._detect_indent(original)
    # This real KiCad 10 file uses 2-space indent.
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
# Integration tests (real pcbnew + real command classes)
# --------------------------------------------------------------------------

requires_pcbnew = pytest.mark.skipif(
    not _real_pcbnew_available(),
    reason="real pcbnew not importable (headless / stubbed)",
)


@pytest.fixture
def loaded_board(temp_project):
    import pcbnew

    pcb = temp_project / "channel_distributor.kicad_pcb"
    if not pcb.exists():
        pytest.skip("no .kicad_pcb copy available")
    board = pcbnew.LoadBoard(str(pcb))
    return board, temp_project


@pytest.mark.integration
@requires_pcbnew
def test_create_netclass_persists_to_disk(loaded_board):
    from commands.routing import RoutingCommands

    board, proj = loaded_board
    cmds = RoutingCommands(board)
    result = cmds.create_netclass(
        {
            "name": "RF",
            "traceWidth": 0.4,
            "clearance": 0.3,
            "viaDiameter": 0.7,
            "viaDrill": 0.35,
            "patterns": ["*RF_*"],
        }
    )
    assert result["success"] is True
    assert result["persisted"] is True

    pro = proj / "channel_distributor.kicad_pro"
    data = json.loads(pro.read_text())
    by_name = {c["name"]: c for c in data["net_settings"]["classes"]}
    assert "RF" in by_name
    assert by_name["RF"]["track_width"] == 0.4
    assert by_name["RF"]["clearance"] == 0.3
    assert by_name["RF"]["via_diameter"] == 0.7
    assert {"netclass": "RF", "pattern": "*RF_*"} in data["net_settings"]["netclass_patterns"]


@pytest.mark.integration
@requires_pcbnew
def test_assign_net_to_class_persists_to_disk(loaded_board):
    from commands.routing import RoutingCommands

    board, proj = loaded_board
    cmds = RoutingCommands(board)

    # Pick a real net from the board to assign.
    netinfo = board.GetNetInfo()
    net_name = None
    for code in range(netinfo.GetNetCount()):
        n = netinfo.GetNetItem(code)
        if n and n.GetNetname():
            net_name = n.GetNetname()
            break
    assert net_name, "board has no nets to test with"

    result = cmds.assign_net_to_class({"net": net_name, "netClass": "Power"})
    assert result["success"] is True
    assert result["persisted"] is True

    pro = proj / "channel_distributor.kicad_pro"
    data = json.loads(pro.read_text())
    assert data["net_settings"]["netclass_assignments"][net_name] == "Power"


@pytest.mark.integration
@requires_pcbnew
def test_assign_netclass_pattern_persists_to_disk(loaded_board):
    from commands.routing import RoutingCommands

    board, proj = loaded_board
    cmds = RoutingCommands(board)

    result = cmds.assign_netclass_pattern({"netClass": "Analog", "pattern": "*AIN?_*"})
    assert result["success"] is True
    assert result["persisted"] is True
    assert result["added"] is True

    pro = proj / "channel_distributor.kicad_pro"
    data = json.loads(pro.read_text())
    assert {"netclass": "Analog", "pattern": "*AIN?_*"} in data["net_settings"]["netclass_patterns"]


@pytest.mark.integration
@requires_pcbnew
def test_set_design_rules_persists_to_disk(loaded_board):
    from commands.design_rules import DesignRuleCommands

    board, proj = loaded_board
    cmds = DesignRuleCommands(board)
    result = cmds.set_design_rules(
        {
            "clearance": 0.18,
            "minTrackWidth": 0.12,
            "minViaDiameter": 0.5,
            "trackWidth": 0.3,
        }
    )
    assert result["success"] is True
    assert result["persisted"] is True

    pro = proj / "channel_distributor.kicad_pro"
    data = json.loads(pro.read_text())
    rules = data["board"]["design_settings"]["rules"]
    assert rules["min_clearance"] == 0.18
    assert rules["min_track_width"] == 0.12
    assert rules["min_via_diameter"] == 0.5
    # Default class track width mirrored.
    by_name = {c["name"]: c for c in data["net_settings"]["classes"]}
    assert by_name["Default"]["track_width"] == 0.3
