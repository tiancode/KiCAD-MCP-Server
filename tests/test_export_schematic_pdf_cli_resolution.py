"""export_schematic_pdf must resolve kicad-cli via the shared bundle resolver.

Regression for E2E round-6 finding S11: the PDF export handler hardcoded
``["kicad-cli", ...]`` and relied on PATH, so it failed on macOS ("kicad-cli
not found in PATH") even though the binary was installed inside
``KiCad.app/Contents/MacOS`` and every other tool (run_erc, get_schematic_view,
export_netlist) found it via ``utils.kicad_cli.find_kicad_cli``.

Unit tests pin that the subprocess argv[0] is the *resolved* path (not the bare
string "kicad-cli"). The final test does a REAL kicad-cli export of the E2E
schematic, skip-gated on the same graceful discovery the rest of the suite uses.
"""

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from handlers.schematic_io import handle_export_schematic_pdf  # noqa: E402
from utils.kicad_cli import find_kicad_cli  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fixture: KiCADInterface instance (no __init__, avoids pcbnew/IPC)
# ---------------------------------------------------------------------------


def _make_iface() -> Any:
    with patch("kicad_interface.USE_IPC_BACKEND", False):
        from kicad_interface import KiCADInterface

        iface = KiCADInterface.__new__(KiCADInterface)
    return iface


@pytest.fixture()
def iface():
    return _make_iface()


# ---------------------------------------------------------------------------
# Unit: resolver is used and its result is argv[0]
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExportPdfResolvesCli:
    _RESOLVED = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"

    def _run(self, iface, tmp_path, extra_params=None):
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)")
        out = tmp_path / "out.pdf"
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return MagicMock(returncode=0, stderr="")

        params = {"schematicPath": str(sch), "outputPath": str(out)}
        if extra_params:
            params.update(extra_params)

        # Patch find_kicad_cli at its single source of truth so the whole
        # resolver chain (_find_kicad_cli_static -> find_kicad_cli) is exercised
        # rather than short-circuited — the bug was that this chain was bypassed.
        with (
            patch("subprocess.run", side_effect=fake_run),
            patch("utils.kicad_cli.find_kicad_cli", lambda: self._RESOLVED),
        ):
            result = handle_export_schematic_pdf(iface, params)
        return result, captured, sch, out

    def test_argv0_is_resolved_path_not_bare_string(self, iface, tmp_path):
        result, captured, sch, out = self._run(iface, tmp_path)
        assert result["success"] is True, result
        cmd = captured["cmd"]
        # The load-bearing assertion: argv[0] is the resolver's absolute path,
        # NOT the PATH-relative literal "kicad-cli".
        assert cmd[0] == self._RESOLVED
        assert cmd[0] != "kicad-cli"
        assert cmd[1:4] == ["sch", "export", "pdf"]
        assert str(sch) in cmd
        assert str(out) in cmd
        assert result["file"]["path"] == str(out)

    def test_black_and_white_flag_before_input(self, iface, tmp_path):
        _result, captured, sch, _out = self._run(
            iface, tmp_path, {"blackAndWhite": True}
        )
        cmd = captured["cmd"]
        assert "--black-and-white" in cmd
        # Flag is inserted before the trailing input path.
        assert cmd.index("--black-and-white") == cmd.index(str(sch)) - 1

    def test_cli_failure_message_propagated(self, iface, tmp_path):
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)")
        with (
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=1, stderr="boom"),
            ),
            patch("utils.kicad_cli.find_kicad_cli", lambda: self._RESOLVED),
        ):
            result = handle_export_schematic_pdf(
                iface,
                {"schematicPath": str(sch), "outputPath": str(tmp_path / "o.pdf")},
            )
        assert result["success"] is False
        assert "boom" in result["message"]


# ---------------------------------------------------------------------------
# Unit: validation & discovery-failure paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExportPdfValidation:
    def test_missing_schematic_path(self, iface, tmp_path):
        result = handle_export_schematic_pdf(
            iface, {"outputPath": str(tmp_path / "o.pdf")}
        )
        assert result["success"] is False
        assert "chematic" in result["message"]

    def test_missing_output_path(self, iface, tmp_path):
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)")
        result = handle_export_schematic_pdf(iface, {"schematicPath": str(sch)})
        assert result["success"] is False
        assert "utput" in result["message"]

    def test_schematic_not_found(self, iface, tmp_path):
        result = handle_export_schematic_pdf(
            iface,
            {
                "schematicPath": "/nope/x.kicad_sch",
                "outputPath": str(tmp_path / "o.pdf"),
            },
        )
        assert result["success"] is False
        assert "not found" in result["message"].lower()

    def test_cli_not_found_gives_clear_error(self, iface, tmp_path):
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)")
        with patch("utils.kicad_cli.find_kicad_cli", lambda: None):
            result = handle_export_schematic_pdf(
                iface,
                {"schematicPath": str(sch), "outputPath": str(tmp_path / "o.pdf")},
            )
        assert result["success"] is False
        assert "kicad-cli" in result["message"]
        # Error must no longer claim PATH-only; it points at the bundle location.
        assert "not found" in result["message"].lower()


# ---------------------------------------------------------------------------
# Real end-to-end export with the actual kicad-cli (skip-gated on discovery).
# Mirrors test_pin_world_xy_eeschema_truth.py: find_kicad_cli() is None -> skip.
# ---------------------------------------------------------------------------

_KICAD_CLI = find_kicad_cli()
_E2E_SCH = Path(
    "/private/tmp/claude-501/-Users-deht-Documents-KiCAD-MCP-Server/"
    "fccb9faf-12b2-4bc8-84a2-101eb20376d3/scratchpad/gd32_radio/gd32_radio.kicad_sch"
)


@pytest.mark.integration
@pytest.mark.skipif(_KICAD_CLI is None, reason="kicad-cli not found")
@pytest.mark.skipif(not _E2E_SCH.exists(), reason="E2E gd32_radio schematic not present")
def test_real_pdf_export_produces_nonempty_file(tmp_path):
    """Copy the READ-ONLY E2E schematic out, export a real PDF via the handler."""
    sch = tmp_path / "gd32_radio.kicad_sch"
    shutil.copy(_E2E_SCH, sch)  # never touch the original
    out = tmp_path / "gd32_radio.pdf"

    iface = _make_iface()
    result = handle_export_schematic_pdf(
        iface, {"schematicPath": str(sch), "outputPath": str(out)}
    )

    assert result["success"] is True, result
    assert result["file"]["path"] == str(out)
    assert out.exists(), "handler reported success but no PDF was written"
    assert out.stat().st_size > 0, "produced PDF is empty"
    # Sanity: it really is a PDF.
    assert out.read_bytes()[:5] == b"%PDF-"
