"""Regression tests for PWR_FLAG net-name leakage.

PWR_FLAG is KiCad's ERC marker — placed on a wire to silence the
"pin not driven" warning on power inputs.  It's a schematic concept,
not a real net.  Two bugs let the literal string "PWR_FLAG" appear
as a net name in the MCP's output:

  1. ``_parse_virtual_connections`` registered the #FLG pin's
     ``Value`` ("PWR_FLAG") into ``point_to_label`` — net-name
     resolvers then surfaced "PWR_FLAG" as the wire's net.
  2. ``_build_hierarchical_pad_net_map`` treated #FLG symbols just
     like #PWR symbols and used their Value as the net name —
     ``sync_schematic_to_board`` then added a synthetic "PWR_FLAG"
     net to the board's NetInfo.

Both are fixed and locked in here.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


# ---------------------------------------------------------------------------
# 1. _parse_virtual_connections uses a sentinel for #FLG, not the literal
# ---------------------------------------------------------------------------
def _fake_pwr_symbol(reference: str, value: str):
    """kicad-skip-shaped symbol stand-in."""
    return SimpleNamespace(
        property=SimpleNamespace(
            Reference=SimpleNamespace(value=reference),
            Value=SimpleNamespace(value=value),
        )
    )


def test_parse_virtual_connections_uses_sentinel_for_pwr_flag(monkeypatch, tmp_path):
    """The pin of a #FLG symbol must land in point_to_label so the
    orphan-wire detector still sees it as an anchor — but NOT with the
    literal "PWR_FLAG" name (that leaks to net-name resolvers)."""
    from commands import wire_connectivity
    from commands.wire_connectivity import (
        PWRFLAG_LABEL_SENTINEL,
        _parse_virtual_connections,
        is_pwrflag_label,
    )

    sch_path = tmp_path / "demo.kicad_sch"
    sch_path.write_text("(kicad_sch)\n", encoding="utf-8")

    # Schematic: one PWR port (GND), one PWR_FLAG, both at distinct points.
    schematic = SimpleNamespace(
        symbol=[
            _fake_pwr_symbol("#PWR01", "GND"),
            _fake_pwr_symbol("#FLG01", "PWR_FLAG"),
        ]
    )

    # Stub PinLocator: both symbols have pin "1" at known positions.
    def fake_pins(self_locator, path, ref):
        if ref == "#PWR01":
            return {"1": (50.0, 60.0)}
        if ref == "#FLG01":
            return {"1": (50.0, 80.0)}
        return {}

    monkeypatch.setattr(wire_connectivity.PinLocator, "get_all_symbol_pins", fake_pins)
    # _parse_labels_sexp would read the file; stub it empty so this test
    # only exercises the power-symbol branch. _parse_virtual_connections resolves
    # these from the _parsing submodule after the package split.
    monkeypatch.setattr(wire_connectivity._parsing, "_parse_labels_sexp", lambda sexp: ({}, {}))
    monkeypatch.setattr(wire_connectivity._parsing, "_load_sexp", lambda path: [])

    point_to_label, label_to_points = _parse_virtual_connections(schematic, str(sch_path))

    # PWR port → real net name registered both ways.
    pwr_iu = (int(50.0 * 10000), int(60.0 * 10000))
    assert point_to_label.get(pwr_iu) == "GND"
    assert pwr_iu in label_to_points.get("GND", [])

    # PWR_FLAG → anchor IS registered (for orphan-wire detection) but
    # with the sentinel; the literal "PWR_FLAG" must NOT appear.
    flag_iu = (int(50.0 * 10000), int(80.0 * 10000))
    assert flag_iu in point_to_label
    assert is_pwrflag_label(point_to_label[flag_iu])
    assert point_to_label[flag_iu] != "PWR_FLAG"
    # Not added to label_to_points so it can't bridge unrelated power
    # rails via the BFS label-jump.
    assert "PWR_FLAG" not in label_to_points
    assert PWRFLAG_LABEL_SENTINEL not in label_to_points


def test_get_net_at_point_does_not_surface_pwr_flag(monkeypatch, tmp_path):
    """Querying the pin position of a PWR_FLAG must NOT return
    "PWR_FLAG" as the net.  Either fall through to a real label on the
    same wire or return None."""
    from commands import wire_connectivity
    from commands.wire_connectivity import get_net_at_point

    sch_path = tmp_path / "demo.kicad_sch"
    sch_path.write_text("(kicad_sch)\n", encoding="utf-8")

    # Single PWR_FLAG, no other labels.  Net at its pin → None (no real net).
    schematic = SimpleNamespace(
        symbol=[_fake_pwr_symbol("#FLG01", "PWR_FLAG")],
        wire=[],
    )

    monkeypatch.setattr(
        wire_connectivity.PinLocator,
        "get_all_symbol_pins",
        lambda self, path, ref: {"1": (50.0, 80.0)} if ref == "#FLG01" else {},
    )
    # _parse_virtual_connections resolves _load_sexp/_parse_labels_sexp from
    # _parsing; get_net_at_point resolves _parse_wires from _queries.
    monkeypatch.setattr(wire_connectivity._parsing, "_parse_labels_sexp", lambda sexp: ({}, {}))
    monkeypatch.setattr(wire_connectivity._parsing, "_load_sexp", lambda path: [])
    monkeypatch.setattr(wire_connectivity._queries, "_parse_wires", lambda sch: [])

    result = get_net_at_point(schematic, str(sch_path), 50.0, 80.0)

    assert result["net_name"] != "PWR_FLAG"
    assert result["net_name"] is None


# ---------------------------------------------------------------------------
# 2. _build_hierarchical_pad_net_map skips #FLG symbols entirely
# ---------------------------------------------------------------------------
def test_sync_to_board_does_not_create_pwr_flag_net(monkeypatch, tmp_path):
    """The hierarchical pad-net mapper used to treat #FLG just like
    #PWR and propagate "PWR_FLAG" via the wire-BFS as a net name.
    The all_net_names set then carried it, which is what
    sync_schematic_to_board uses to feed pcbnew.NETINFO_ITEM.  Skip
    #FLG so no synthetic "PWR_FLAG" net appears on the board."""
    import kicad_interface
    from kicad_interface import KiCADInterface

    # Build a fake schematic with one PWR (GND) + one PWR_FLAG, both
    # whose pin-1 positions snap to the same wire-graph component so
    # the BFS would have a chance to spread "PWR_FLAG" if we didn't
    # filter at the source.
    flag_sym = _fake_pwr_symbol("#FLG01", "PWR_FLAG")
    pwr_sym = _fake_pwr_symbol("#PWR01", "GND")
    fake_schematic = SimpleNamespace(
        symbol=[flag_sym, pwr_sym],
        wire=[],
        label=[],
        global_label=[],
        hierarchical_label=[],
        sheet=[],
    )

    # Stub the Schematic loader so we don't need a real .kicad_sch file.
    monkeypatch.setattr(kicad_interface, "Schematic", lambda path: fake_schematic, raising=False)

    # Stub pin locator: both symbols have pin "1" at the same location
    # so a real wire-BFS would land both labels on one net.
    class _Locator:
        def get_all_symbol_pins(self, sch_path, ref):
            if ref == "#PWR01":
                return {"1": (50.0, 60.0)}
            if ref == "#FLG01":
                return {"1": (50.0, 60.0)}
            return {}

    monkeypatch.setattr(kicad_interface, "PinLocator", _Locator, raising=False)

    iface = KiCADInterface.__new__(KiCADInterface)
    iface._current_project_path = tmp_path
    sch_path = tmp_path / "demo.kicad_sch"
    sch_path.write_text("(kicad_sch)\n", encoding="utf-8")

    _, all_net_names = iface._build_hierarchical_pad_net_map(str(sch_path))

    assert "PWR_FLAG" not in all_net_names, (
        "PWR_FLAG must not be reported as a real net — it's a schematic ERC "
        "marker, not a net the board can use.  The actual rail (GND here) "
        "should be the only net surfaced for this corner of the schematic."
    )
