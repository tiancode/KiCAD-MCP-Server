"""Regression tests: run_erc must not loop on phantom lib_symbol_mismatch.

After run_erc started merging the project-local sym-lib-table, kicad-cli's
headless LIB_SYMBOL::Compare began flagging project-scoped custom symbols with
``lib_symbol_mismatch`` even when the schematic's embedded lib_symbols entry is
byte-identical to the on-disk .kicad_sym. refresh_schematic_lib_symbols is then
a no-op, so the old "recommend refresh → re-run" advice was an infinite loop.

handle_run_erc now compares each flagged symbol's embedded definition against
disk: identical → tag likely_false_positive (and never recommend refresh);
genuinely drifted → keep the refresh recommendation.
"""

import json
import sys
import unittest.mock as mock
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

_SYMBOL_BODY = (
    '(symbol "{name}" (pin_names (offset 0)) (in_bom yes) (on_board yes)\n'
    '  (property "Reference" "{ref}" (at 0 0 0))\n'
    '  (property "Value" "WIDGET" (at 0 0 0))\n'
    '  (symbol "WIDGET_0_1" (rectangle (start -2 2) (end 2 -2)))\n'
    ")"
)


def _make_project(tmp_path: Path, embedded_ref: str = "U", disk_ref: str = "U") -> Path:
    """Project with mylib.kicad_sym (symbol WIDGET) + a schematic embedding
    mylib:WIDGET. embedded_ref != disk_ref makes the embedded copy differ."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "mylib.kicad_sym").write_text(
        "(kicad_symbol_lib (version 20211014) (generator test)\n"
        + _SYMBOL_BODY.format(name="WIDGET", ref=disk_ref)
        + "\n)\n",
        encoding="utf-8",
    )
    (proj / "sym-lib-table").write_text(
        "(sym_lib_table\n  (version 7)\n"
        '  (lib (name "mylib")(type "KiCad")(uri "${KIPRJMOD}/mylib.kicad_sym")(options ""))\n)\n',
        encoding="utf-8",
    )
    (proj / "demo.kicad_sch").write_text(
        "(kicad_sch (version 20211123) (generator test)\n"
        "  (lib_symbols\n"
        + _SYMBOL_BODY.format(name="mylib:WIDGET", ref=embedded_ref)
        + "\n  )\n)\n",
        encoding="utf-8",
    )
    return proj


def test_embedded_matching_disk_detected(tmp_path):
    from handlers.schematic_io import _embedded_symbols_matching_disk

    proj = _make_project(tmp_path)  # embedded == disk
    assert _embedded_symbols_matching_disk(str(proj / "demo.kicad_sch"), proj) == {"WIDGET"}


def test_embedded_differing_from_disk_not_matched(tmp_path):
    from handlers.schematic_io import _embedded_symbols_matching_disk

    proj = _make_project(tmp_path, embedded_ref="U", disk_ref="X")  # differ
    assert _embedded_symbols_matching_disk(str(proj / "demo.kicad_sch"), proj) == set()


def test_bare_name_collision_not_treated_as_match(tmp_path):
    """Two libs both define 'WIDGET'; one's embedded copy matches disk, the
    other's differs. Because the violation message only quotes the symbol name
    (not the library), an ambiguous name must NOT be reported as a false
    positive — otherwise libB's genuine mismatch would be silently hidden."""
    from handlers.schematic_io import _embedded_symbols_matching_disk

    def _sym(name, ref):
        return (
            f'(symbol "{name}" (in_bom yes) (on_board yes)\n'
            f'  (property "Reference" "{ref}" (at 0 0 0))\n'
            '  (symbol "WIDGET_0_1" (rectangle (start -1 1) (end 1 -1))))'
        )

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "libA.kicad_sym").write_text(
        f"(kicad_symbol_lib (version 1) (generator t)\n{_sym('WIDGET', 'U')}\n)\n", "utf-8"
    )
    (proj / "libB.kicad_sym").write_text(
        f"(kicad_symbol_lib (version 1) (generator t)\n{_sym('WIDGET', 'X')}\n)\n", "utf-8"
    )
    (proj / "sym-lib-table").write_text(
        "(sym_lib_table (version 7)\n"
        '  (lib (name "libA")(type "KiCad")(uri "${KIPRJMOD}/libA.kicad_sym")(options ""))\n'
        '  (lib (name "libB")(type "KiCad")(uri "${KIPRJMOD}/libB.kicad_sym")(options ""))\n)\n',
        "utf-8",
    )
    # libA:WIDGET embedded matches libA disk (ref U); libB:WIDGET embedded
    # differs from libB disk (embedded ref U, disk ref X).
    (proj / "demo.kicad_sch").write_text(
        "(kicad_sch (version 1) (generator t)\n  (lib_symbols\n"
        f"{_sym('libA:WIDGET', 'U')}\n{_sym('libB:WIDGET', 'U')}\n  )\n)\n",
        "utf-8",
    )
    # 'WIDGET' is ambiguous (matches libA, differs libB) → excluded from FP set.
    assert _embedded_symbols_matching_disk(str(proj / "demo.kicad_sch"), proj) == set()


def _run_erc_with_mismatch(monkeypatch, sch_path, descriptions, auto_refresh):
    """Drive handle_run_erc with a faked kicad-cli emitting the given
    lib_symbol_mismatch descriptions."""
    from handlers.schematic_io import handle_run_erc
    from kicad_interface import KiCADInterface

    def _fake_run(cmd, **kw):
        out = cmd[cmd.index("--output") + 1]
        viols = [
            {
                "type": "lib_symbol_mismatch",
                "severity": "warning",
                "description": d,
                "items": [{"pos": {"x": 1.0, "y": 1.0}}],
            }
            for d in descriptions
        ]
        Path(out).write_text(json.dumps({"sheets": [{"violations": viols}]}), encoding="utf-8")
        return mock.MagicMock(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("subprocess.run", _fake_run)

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.design_rule_commands = mock.MagicMock()
    iface.design_rule_commands._find_kicad_cli = mock.MagicMock(return_value="/fake/kicad-cli")
    return handle_run_erc(
        iface, {"schematicPath": str(sch_path), "autoRefreshLibSymbols": auto_refresh}
    )


def test_byte_identical_mismatch_tagged_fp_no_refresh_rec(monkeypatch, tmp_path):
    """The user's case: embedded == disk, kicad-cli flags it anyway → tag false
    positive, exclude from real_errors, and DO NOT recommend refresh (no loop)."""
    proj = _make_project(tmp_path)  # embedded == disk
    out = _run_erc_with_mismatch(
        monkeypatch,
        proj / "demo.kicad_sch",
        ["Symbol 'WIDGET' has differences from the library"] * 3,
        auto_refresh=False,
    )
    assert out["success"] is True
    assert len(out["violations"]) == 3
    assert all(v.get("likely_false_positive") for v in out["violations"])
    assert out["summary"]["lib_symbol_mismatch_false_positives"] == 3
    assert out["summary"]["real_errors"] == 0
    assert [r["kind"] for r in out["summary"]["recommendations"]] == []


def test_genuine_mismatch_keeps_refresh_rec(monkeypatch, tmp_path):
    """Embedded differs from disk → genuine drift → keep the refresh
    recommendation (when the user opted out of the pre-ERC auto-refresh)."""
    proj = _make_project(tmp_path, embedded_ref="U", disk_ref="X")  # differ
    out = _run_erc_with_mismatch(
        monkeypatch,
        proj / "demo.kicad_sch",
        ["Symbol 'WIDGET' has differences from the library"],
        auto_refresh=False,
    )
    assert not out["violations"][0].get("likely_false_positive")
    assert out["summary"]["lib_symbol_mismatch_false_positives"] == 0
    assert "refresh_lib_symbols" in [r["kind"] for r in out["summary"]["recommendations"]]


def test_mixed_fp_and_genuine(monkeypatch, tmp_path):
    """A byte-identical mismatch (FP) and a genuine one in the same run: only the
    genuine one is counted and drives the refresh recommendation; the FP is
    tagged and excluded."""
    proj = _make_project(tmp_path)  # WIDGET embedded == disk
    out = _run_erc_with_mismatch(
        monkeypatch,
        proj / "demo.kicad_sch",
        [
            "Symbol 'WIDGET' has differences from the library",  # FP (matches disk)
            "Symbol 'GHOST' has differences from the library",  # genuine (unknown sym)
        ],
        auto_refresh=False,
    )
    by_fp = {
        v["message"].split("'")[1]: v.get("likely_false_positive", False) for v in out["violations"]
    }
    assert by_fp == {"WIDGET": True, "GHOST": False}
    assert out["summary"]["lib_symbol_mismatch_false_positives"] == 1
    refresh = next(
        (r for r in out["summary"]["recommendations"] if r["kind"] == "refresh_lib_symbols"), None
    )
    assert refresh is not None and refresh["count"] == 1
