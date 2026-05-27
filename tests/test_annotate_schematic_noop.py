"""Regression test for annotate_schematic's no-op clarity.

Before: agents that called ``add_schematic_component(reference="U1")``
(concrete ref at creation) then ``annotate_schematic`` got
``"All components already annotated"`` — technically correct, but the
tool description didn't say so, and the agent couldn't easily detect
the no-op state programmatically.

After: the response carries ``noop: true`` and the message points at
the actual cause ("no '?' placeholders").
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _fake_symbol(reference: str):
    """Build a kicad-skip-shaped symbol stand-in."""
    sym = SimpleNamespace()
    sym.property = SimpleNamespace(
        Reference=SimpleNamespace(value=reference),
    )
    sym.uuid = SimpleNamespace(value=f"uuid-{reference}")
    sym.setAllReferences = MagicMock()
    return sym


def test_annotate_returns_noop_when_every_symbol_has_concrete_ref(monkeypatch, tmp_path):
    """All symbols carry concrete refs (R1, U1) — nothing to annotate.
    Response must say noop:true and the message must point at the cause."""
    from handlers import schematic_component
    from handlers.schematic_component import handle_annotate_schematic

    sch_path = tmp_path / "demo.kicad_sch"
    sch_path.write_text("(kicad_sch)\n", encoding="utf-8")

    fake_schematic = SimpleNamespace(
        symbol=[_fake_symbol("R1"), _fake_symbol("U1"), _fake_symbol("C1")]
    )
    monkeypatch.setattr(
        schematic_component.SchematicManager,
        "load_schematic",
        staticmethod(lambda path: fake_schematic),
    )
    save_called = MagicMock()
    monkeypatch.setattr(
        schematic_component.SchematicManager, "save_schematic", staticmethod(save_called)
    )

    iface = MagicMock()
    result = handle_annotate_schematic(iface, {"schematicPath": str(sch_path)})

    assert result["success"] is True
    assert result["noop"] is True
    assert result["annotated"] == []
    # Message must mention the cause and the `?` placeholder convention.
    assert "?" in result["message"]
    # We didn't touch any symbol → no save.
    save_called.assert_not_called()


def test_annotate_runs_normally_when_some_symbols_have_placeholder(monkeypatch, tmp_path):
    """A mix of concrete and placeholder refs — the placeholders get
    annotated, the concrete ones stay put, and noop must NOT be set."""
    from handlers import schematic_component
    from handlers.schematic_component import handle_annotate_schematic

    sch_path = tmp_path / "demo.kicad_sch"
    sch_path.write_text("(kicad_sch)\n", encoding="utf-8")

    r1 = _fake_symbol("R1")  # already annotated
    r_placeholder = _fake_symbol("R?")  # needs annotation → R2 (R1 is taken)
    u_placeholder = _fake_symbol("U?")  # needs annotation → U1
    fake_schematic = SimpleNamespace(symbol=[r1, r_placeholder, u_placeholder])

    monkeypatch.setattr(
        schematic_component.SchematicManager,
        "load_schematic",
        staticmethod(lambda path: fake_schematic),
    )
    saves = []
    monkeypatch.setattr(
        schematic_component.SchematicManager,
        "save_schematic",
        staticmethod(lambda sch, p: saves.append(p)),
    )

    iface = MagicMock()
    result = handle_annotate_schematic(iface, {"schematicPath": str(sch_path)})

    assert result["success"] is True
    assert result.get("noop") is not True
    new_refs = {row["newReference"] for row in result["annotated"]}
    assert new_refs == {"R2", "U1"}
    # The annotation must have been written back.
    assert saves == [str(sch_path)]
