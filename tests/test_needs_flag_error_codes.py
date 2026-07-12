"""
enrich_failure must stamp a truthful errorCode on every structured needs_*
refusal instead of the generic INTERNAL_ERROR.

Regression: add_gnd_stitching_vias' `needs_zone_fill` refusal (and, before it,
`needs_reconcile`) reached the MCP client tagged INTERNAL_ERROR, so an agent
couldn't tell a recoverable "fill the zone / reconcile first" state apart from a
real crash. Each flag now maps to a stable code + actionable hint, kept in one
place (utils.failure._NEEDS_FLAGS).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from utils.failure import enrich_failure  # noqa: E402


@pytest.mark.unit
def test_needs_zone_fill_gets_zone_fill_code():
    result = {
        "success": False,
        "needs_zone_fill": True,
        "message": "The 'in_zones' strategy needs filled GND copper",
        "gnd_net": "GND",
    }
    out = enrich_failure("add_gnd_stitching_vias", result)
    assert out["errorCode"] == "NEEDS_ZONE_FILL"
    # Hint names the concrete remedy tool call.
    assert "copper_pour(action=refill, force=true)" in out["hint"]
    # Payload preserved verbatim.
    assert out["needs_zone_fill"] is True
    assert out["gnd_net"] == "GND"
    assert out["message"] == result["message"]


@pytest.mark.unit
def test_needs_zone_fill_not_labeled_internal_error():
    """The exact mislabel this fix targets must not happen."""
    out = enrich_failure("add_gnd_stitching_vias", {"success": False, "needs_zone_fill": True})
    assert out["errorCode"] != "INTERNAL_ERROR"


@pytest.mark.unit
def test_needs_reconcile_gets_reconcile_code_with_direction():
    result = {
        "success": False,
        "needs_reconcile": True,
        "direction": "swig_to_ipc",
        "message": "SWIG wrote new content to disk ...",
    }
    out = enrich_failure("route_trace", result)
    assert out["errorCode"] == "NEEDS_RECONCILE"
    # Hint names the exact reconcile_backends call including the direction.
    assert 'reconcile_backends(direction="swig_to_ipc")' in out["hint"]
    assert out["direction"] == "swig_to_ipc"


@pytest.mark.unit
def test_needs_reconcile_without_direction_still_hints_reconcile():
    out = enrich_failure("route_trace", {"success": False, "needs_reconcile": True})
    assert out["errorCode"] == "NEEDS_RECONCILE"
    assert "reconcile_backends" in out["hint"]


@pytest.mark.unit
def test_needs_unit_placement_gets_unit_placement_code():
    out = enrich_failure(
        "connect_to_net",
        {"success": False, "needs_unit_placement": True, "message": "pin on unplaced unit"},
    )
    assert out["errorCode"] == "NEEDS_UNIT_PLACEMENT"
    assert "add_schematic_component" in out["hint"]


@pytest.mark.unit
def test_needs_manual_action_gets_manual_action_code():
    out = enrich_failure(
        "reconcile_backends",
        {"success": False, "needs_manual_action": True, "steps": ["do x", "do y"]},
    )
    assert out["errorCode"] == "MANUAL_ACTION_REQUIRED"
    assert "steps" in out["hint"]
    assert out["steps"] == ["do x", "do y"]


@pytest.mark.unit
def test_needs_pcb_editor_still_gets_pcb_editor_code():
    """The pre-existing branch keeps working under the table-driven mechanism."""
    out = enrich_failure(
        "add_track", {"success": False, "needs_pcb_editor": True, "message": "requires PCB editor"}
    )
    assert out["errorCode"] == "PCB_EDITOR_REQUIRED"
    assert "PCB editor" in out["hint"]


@pytest.mark.unit
def test_existing_error_code_is_not_overwritten():
    out = enrich_failure("foo", {"success": False, "needs_zone_fill": True, "errorCode": "CUSTOM"})
    assert out["errorCode"] == "CUSTOM"


@pytest.mark.unit
def test_existing_hint_is_preserved():
    out = enrich_failure("foo", {"success": False, "needs_zone_fill": True, "hint": "my own hint"})
    assert out["errorCode"] == "NEEDS_ZONE_FILL"
    assert out["hint"] == "my own hint"


@pytest.mark.unit
def test_success_payload_untouched():
    payload = {"success": True, "needs_zone_fill": True}
    assert enrich_failure("foo", payload) is payload


@pytest.mark.unit
def test_plain_failure_without_flag_falls_back_to_classifier():
    out = enrich_failure("foo", {"success": False, "message": "kaboom"})
    assert out["errorCode"] == "INTERNAL_ERROR"
