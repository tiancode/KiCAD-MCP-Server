"""S8 regression: rotate_schematic_component angle validation + normalization.

KiCad schematic symbols only support orthogonal rotations (0/90/180/270). The
handler must reject a non-multiple-of-90 (e.g. 45°) rather than persist an
invalid schematic, and normalize negatives / values ≥360 (e.g. -90 → 270).
"""

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from handlers.schematic_component._placement import (  # noqa: E402
    handle_rotate_schematic_component,
)

_SCH = textwrap.dedent(
    """\
    (kicad_sch (version 20250114) (generator "test")
      (paper "A4")
      (lib_symbols
        (symbol "Device:R"
          (pin passive line (at 0 1.016 270) (length 1.27)
            (name "~" (effects (font (size 1.27 1.27))))
            (number "1" (effects (font (size 1.27 1.27)))))
          (pin passive line (at 0 -1.016 90) (length 1.27)
            (name "~" (effects (font (size 1.27 1.27))))
            (number "2" (effects (font (size 1.27 1.27)))))
        )
      )
      (symbol (lib_id "Device:R") (at 100 100 0)
        (property "Reference" "R1" (at 100 100 0))
        (property "Value" "10k" (at 100 100 0))
      )
    )
    """
)


def _sch(tmp_path):
    p = tmp_path / "rot.kicad_sch"
    p.write_text(_SCH, encoding="utf-8")
    return p


@pytest.mark.unit
class TestRotateAngleValidation:
    def test_45_degrees_rejected(self, tmp_path):
        res = handle_rotate_schematic_component(
            None, {"schematicPath": str(_sch(tmp_path)), "reference": "R1", "angle": 45}
        )
        assert res["success"] is False
        assert res["errorCode"] == "INVALID_ROTATION"
        # The message must name the valid values.
        assert "0, 90, 180, or 270" in res["message"] or "90" in res["message"]

    def test_non_numeric_rejected(self, tmp_path):
        res = handle_rotate_schematic_component(
            None, {"schematicPath": str(_sch(tmp_path)), "reference": "R1", "angle": "sideways"}
        )
        assert res["success"] is False
        assert res["errorCode"] == "INVALID_ROTATION"

    def test_negative_90_normalized_to_270(self, tmp_path):
        sch = _sch(tmp_path)
        res = handle_rotate_schematic_component(
            None, {"schematicPath": str(sch), "reference": "R1", "angle": -90}
        )
        assert res["success"] is True
        assert res["angle"] == 270
        assert res["requestedAngle"] == -90
        # And the file stores the normalized angle, not -90.
        assert "270" in sch.read_text(encoding="utf-8")

    def test_450_normalized_to_90(self, tmp_path):
        res = handle_rotate_schematic_component(
            None, {"schematicPath": str(_sch(tmp_path)), "reference": "R1", "angle": 450}
        )
        assert res["success"] is True
        assert res["angle"] == 90
        assert res["requestedAngle"] == 450

    def test_valid_90_no_normalization_field(self, tmp_path):
        res = handle_rotate_schematic_component(
            None, {"schematicPath": str(_sch(tmp_path)), "reference": "R1", "angle": 90}
        )
        assert res["success"] is True
        assert res["angle"] == 90
        assert "requestedAngle" not in res
