"""Tests for the ngspice batch-simulation runner (commands.simulation).

All tests run without kicad-cli or ngspice installed: subprocess.run is
replaced via the ``run`` injection point and the parsers/deck builder are
exercised as pure functions.
"""

import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.simulation import (  # noqa: E402
    build_control_deck,
    downsample,
    parse_op_output,
    parse_wrdata_file,
    run_simulation,
)

NETLIST = """.title KiCad schematic
V1 in 0 5
R1 in out 1k
R2 out 0 1k
.end
"""

OP_STDOUT = """
Note: Compatibility modes selected: kicad

No. of Data Rows : 1
in = 5.000000e+00
out = 2.500000e+00
mid_node.3 = 1.234500e-01
v1#branch = -2.50000e-03

ngspice-42 done
"""


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_fake_run(
    op_stdout: str = "",
    data_text: Optional[str] = None,
    kicad_rc: int = 0,
    kicad_stderr: str = "",
    ngspice_missing: bool = False,
    ngspice_stderr: str = "",
):
    """Build a subprocess.run stand-in faking kicad-cli and ngspice."""
    calls: List[List[str]] = []

    def fake_run(cmd: List[str], **kwargs: Any) -> _FakeProc:
        calls.append(list(cmd))
        if "kicad-cli" in Path(cmd[0]).name:
            if kicad_rc == 0:
                out = cmd[cmd.index("-o") + 1]
                Path(out).write_text(NETLIST, encoding="utf-8")
            return _FakeProc(kicad_rc, "", kicad_stderr)
        # ngspice invocation
        if ngspice_missing:
            raise FileNotFoundError("ngspice")
        deck = Path(cmd[cmd.index("-b") + 1]).read_text(encoding="utf-8")
        match = re.search(r"^wrdata (\S+)", deck, re.MULTILINE)
        if match and data_text is not None:
            Path(match.group(1)).write_text(data_text, encoding="utf-8")
        return _FakeProc(0, op_stdout, ngspice_stderr)

    fake_run.calls = calls  # type: ignore[attr-defined]
    return fake_run


def _schematic(tmp_path: Path) -> str:
    sch = tmp_path / "test.kicad_sch"
    sch.write_text("(kicad_sch (version 20250114))", encoding="utf-8")
    return str(sch)


# ---------------------------------------------------------------------------
# build_control_deck
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildControlDeck:
    def _lines(self, deck: str) -> List[str]:
        return deck.splitlines()

    def _assert_structure(self, lines: List[str], analysis_line: str) -> None:
        """Common assertions: order, quit present, exactly one .end at the end."""
        i_control = lines.index(".control")
        i_analysis = lines.index(analysis_line)
        i_quit = lines.index("quit")
        i_endc = lines.index(".endc")
        assert i_control < i_analysis < i_quit < i_endc
        assert lines.count(".end") == 1
        assert lines[-1] == ".end"
        assert i_endc < lines.index(".end")

    def test_op_deck(self):
        deck = build_control_deck(NETLIST, "op", None, None, "/w/data.txt")
        lines = self._lines(deck)
        self._assert_structure(lines, "op")
        assert "print all" in lines
        assert not any(line.startswith("wrdata") for line in lines)

    def test_tran_deck(self):
        deck = build_control_deck(
            NETLIST, "tran", {"tstep": "1u", "tstop": "10m"}, ["v(out)"], "/w/data.txt"
        )
        lines = self._lines(deck)
        self._assert_structure(lines, "tran 1u 10m")
        assert "wrdata /w/data.txt v(out)" in lines

    def test_tran_deck_with_tstart(self):
        deck = build_control_deck(
            NETLIST,
            "tran",
            {"tstep": "1u", "tstop": "10m", "tstart": "2m"},
            ["v(out)"],
            "/w/data.txt",
        )
        assert "tran 1u 10m 2m" in self._lines(deck)

    def test_dc_deck(self):
        deck = build_control_deck(
            NETLIST,
            "dc",
            {"source": "V1", "start": 0, "stop": 5, "step": 0.1},
            ["v(out)", "i(V1)"],
            "/w/data.txt",
        )
        lines = self._lines(deck)
        self._assert_structure(lines, "dc V1 0 5 0.1")
        assert "wrdata /w/data.txt v(out) i(V1)" in lines

    def test_ac_deck(self):
        deck = build_control_deck(
            NETLIST,
            "ac",
            {"variation": "dec", "points": 10, "fstart": 1, "fstop": "1Meg"},
            ["v(out)"],
            "/w/data.txt",
        )
        lines = self._lines(deck)
        self._assert_structure(lines, "ac dec 10 1 1Meg")
        assert "wrdata /w/data.txt v(out)" in lines

    def test_ac_variation_defaults_to_dec(self):
        deck = build_control_deck(
            NETLIST, "ac", {"points": 20, "fstart": 10, "fstop": "100k"}, ["v(out)"], "/d"
        )
        assert "ac dec 20 10 100k" in deck.splitlines()

    def test_existing_end_is_stripped(self):
        assert ".end" in NETLIST  # netlist under test really has one
        deck = build_control_deck(NETLIST, "op", None, None, "/w/data.txt")
        lines = deck.splitlines()
        assert lines.count(".end") == 1
        # netlist body survives
        assert "R1 in out 1k" in lines

    def test_missing_params_raise(self):
        with pytest.raises(ValueError, match="tstop"):
            build_control_deck(NETLIST, "tran", {"tstep": "1u"}, ["v(out)"], "/d")

    def test_non_op_requires_signals(self):
        with pytest.raises(ValueError, match="signals"):
            build_control_deck(NETLIST, "tran", {"tstep": "1u", "tstop": "1m"}, [], "/d")

    def test_unknown_analysis_raises(self):
        with pytest.raises(ValueError, match="noise"):
            build_control_deck(NETLIST, "noise", {}, ["v(out)"], "/d")


# ---------------------------------------------------------------------------
# parse_op_output
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseOpOutput:
    def test_realistic_output(self):
        values = parse_op_output(OP_STDOUT)
        assert values == {
            "in": 5.0,
            "out": 2.5,
            "mid_node.3": pytest.approx(0.12345),
            "v1#branch": pytest.approx(-2.5e-3),
        }

    def test_branch_current_e_notation(self):
        values = parse_op_output("v1#branch = -1.23456e-06\n")
        assert values["v1#branch"] == pytest.approx(-1.23456e-6)

    def test_ignores_non_value_lines(self):
        stdout = "Doing analysis at TEMP = 27.000000\nNote: something = broken=text\n"
        values = parse_op_output(stdout)
        # "TEMP = 27" is not in name=value shape once prefixed; only clean pairs parse
        assert "Note:" not in values
        assert all(isinstance(v, float) for v in values.values())

    def test_empty(self):
        assert parse_op_output("") == {}


# ---------------------------------------------------------------------------
# parse_wrdata_file
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseWrdataFile:
    def test_two_signals_real(self):
        text = (
            " 0.000000e+00  1.000000e+00  0.000000e+00  5.000000e+00\n"
            " 1.000000e-03  9.000000e-01  1.000000e-03  4.900000e+00\n"
            " 2.000000e-03  8.000000e-01  2.000000e-03  4.800000e+00\n"
        )
        parsed = parse_wrdata_file(text, ["v(out)", "v(in)"])
        assert parsed["x"] == [0.0, 1e-3, 2e-3]
        assert parsed["signals"]["v(out)"] == [1.0, 0.9, 0.8]
        assert parsed["signals"]["v(in)"] == [5.0, 4.9, 4.8]
        assert parsed["complex"] is False

    def test_complex_ac_layout(self):
        # x real imag per signal (ngspice wrdata on complex vectors)
        text = " 1.0e+00  0.5  -0.5\n 1.0e+01  0.4  -0.6\n"
        parsed = parse_wrdata_file(text, ["v(out)"])
        assert parsed["complex"] is True
        assert parsed["x"] == [1.0, 10.0]
        assert parsed["signals"]["v(out)"] == {"real": [0.5, 0.4], "imag": [-0.5, -0.6]}

    def test_skips_blank_and_header_lines(self):
        text = "time v(out)\n\n 0.0 1.0\n 1.0 2.0\n"
        parsed = parse_wrdata_file(text, ["v(out)"])
        assert parsed["x"] == [0.0, 1.0]
        assert parsed["signals"]["v(out)"] == [1.0, 2.0]

    def test_bad_column_count_raises(self):
        with pytest.raises(ValueError, match="column"):
            parse_wrdata_file(" 0.0 1.0 2.0\n", ["v(a)", "v(b)"])

    def test_empty_file(self):
        parsed = parse_wrdata_file("", ["v(out)"])
        assert parsed == {"x": [], "signals": {"v(out)": []}, "complex": False}


# ---------------------------------------------------------------------------
# downsample
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDownsample:
    def test_large_input_endpoints_preserved(self):
        rows = list(range(10000))
        out = downsample(rows, 100)
        assert len(out) == 100
        assert out[0] == 0
        assert out[-1] == 9999
        assert out == sorted(out)  # monotonic selection

    def test_small_input_untouched(self):
        rows = [1.0, 2.0, 3.0]
        assert downsample(rows, 2000) == rows

    def test_exact_size_untouched(self):
        rows = list(range(100))
        assert downsample(rows, 100) == rows

    def test_alignment_across_arrays(self):
        # Same length + max_points must select the same indices.
        x = list(range(5000))
        y = [v * 2 for v in x]
        xd = downsample(x, 50)
        yd = downsample(y, 50)
        assert [v * 2 for v in xd] == yd


# ---------------------------------------------------------------------------
# run_simulation end-to-end (injected subprocess.run)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRunSimulation:
    def test_tran_end_to_end(self, tmp_path):
        sch = _schematic(tmp_path)
        data_text = "".join(f" {i * 1e-6:e}  {float(i):e}\n" for i in range(5))
        fake = _make_fake_run(data_text=data_text)
        result = run_simulation(
            sch,
            analysis="tran",
            params={"tstep": "1u", "tstop": "10m"},
            signals=["v(out)"],
            kicad_cli="kicad-cli",
            ngspice="ngspice",
            run=fake,
            workdir=str(tmp_path / "work"),
        )
        assert result["success"] is True
        assert result["analysis"] == "tran"
        assert result["engine"] == "ngspice"
        assert result["pointsReturned"] == 5
        assert result["truncated"] is False
        assert result["data"]["x"] == pytest.approx([0.0, 1e-6, 2e-6, 3e-6, 4e-6])
        assert result["data"]["signals"]["v(out)"] == pytest.approx([0.0, 1.0, 2.0, 3.0, 4.0])
        assert Path(result["netlistPath"]).read_text(encoding="utf-8") == NETLIST
        # First call is the kicad-cli export, second is ngspice -b
        assert fake.calls[0][:5] == ["kicad-cli", "sch", "export", "netlist", "--format"]
        assert "spice" in fake.calls[0]
        assert fake.calls[1][0] == "ngspice"
        assert fake.calls[1][1] == "-b"

    def test_op_end_to_end(self, tmp_path):
        sch = _schematic(tmp_path)
        fake = _make_fake_run(op_stdout=OP_STDOUT)
        result = run_simulation(
            sch,
            analysis="op",
            kicad_cli="kicad-cli",
            ngspice="ngspice",
            run=fake,
            workdir=str(tmp_path / "work"),
        )
        assert result["success"] is True
        assert result["data"]["out"] == pytest.approx(2.5)
        assert result["data"]["v1#branch"] == pytest.approx(-2.5e-3)
        assert result["pointsReturned"] == len(result["data"])
        assert result["truncated"] is False

    def test_op_signal_filtering(self, tmp_path):
        sch = _schematic(tmp_path)
        fake = _make_fake_run(op_stdout=OP_STDOUT)
        result = run_simulation(
            sch,
            analysis="op",
            signals=["v(out)", "i(V1)"],
            kicad_cli="kicad-cli",
            ngspice="ngspice",
            run=fake,
            workdir=str(tmp_path / "work"),
        )
        assert result["success"] is True
        assert set(result["data"]) == {"out", "v1#branch"}

    def test_downsampling_and_truncated_flag(self, tmp_path):
        sch = _schematic(tmp_path)
        data_text = "".join(f" {i * 1e-6:e}  {float(i):e}\n" for i in range(50))
        fake = _make_fake_run(data_text=data_text)
        result = run_simulation(
            sch,
            analysis="tran",
            params={"tstep": "1u", "tstop": "50u"},
            signals=["v(out)"],
            max_points=10,
            kicad_cli="kicad-cli",
            ngspice="ngspice",
            run=fake,
            workdir=str(tmp_path / "work"),
        )
        assert result["success"] is True
        assert result["pointsReturned"] == 10
        assert result["truncated"] is True
        assert result["data"]["x"][0] == pytest.approx(0.0)
        assert result["data"]["x"][-1] == pytest.approx(49e-6)
        assert len(result["data"]["signals"]["v(out)"]) == 10

    def test_ac_complex_data(self, tmp_path):
        sch = _schematic(tmp_path)
        data_text = " 1.0e+00  0.5  -0.5\n 1.0e+01  0.4  -0.6\n"
        fake = _make_fake_run(data_text=data_text)
        result = run_simulation(
            sch,
            analysis="ac",
            params={"variation": "dec", "points": 10, "fstart": 1, "fstop": "1Meg"},
            signals=["v(out)"],
            kicad_cli="kicad-cli",
            ngspice="ngspice",
            run=fake,
            workdir=str(tmp_path / "work"),
        )
        assert result["success"] is True
        assert result["data"]["complex"] is True
        assert result["data"]["signals"]["v(out)"]["real"] == pytest.approx([0.5, 0.4])
        assert result["data"]["signals"]["v(out)"]["imag"] == pytest.approx([-0.5, -0.6])

    def test_ngspice_stderr_collected_as_warnings(self, tmp_path):
        sch = _schematic(tmp_path)
        fake = _make_fake_run(
            op_stdout=OP_STDOUT, ngspice_stderr="Warning: vout: no DC path to ground\n"
        )
        result = run_simulation(
            sch,
            analysis="op",
            kicad_cli="kicad-cli",
            ngspice="ngspice",
            run=fake,
            workdir=str(tmp_path / "work"),
        )
        assert result["success"] is True
        assert result["warnings"] == ["Warning: vout: no DC path to ground"]

    def test_ngspice_missing(self, tmp_path):
        sch = _schematic(tmp_path)
        fake = _make_fake_run(ngspice_missing=True)
        result = run_simulation(
            sch,
            analysis="op",
            kicad_cli="kicad-cli",
            ngspice="ngspice",
            run=fake,
            workdir=str(tmp_path / "work"),
        )
        assert result["success"] is False
        assert result["message"] == "ngspice not found"
        assert "apt install ngspice" in result["hint"]
        assert "brew install ngspice" in result["hint"]

    def test_kicad_cli_export_failure(self, tmp_path):
        sch = _schematic(tmp_path)
        fake = _make_fake_run(kicad_rc=1, kicad_stderr="Error: unable to load symbol library")
        result = run_simulation(
            sch,
            analysis="op",
            kicad_cli="kicad-cli",
            ngspice="ngspice",
            run=fake,
            workdir=str(tmp_path / "work"),
        )
        assert result["success"] is False
        assert "unable to load symbol library" in result["message"]
        assert "hint" in result
        # ngspice must never have been invoked
        assert all("kicad-cli" in Path(c[0]).name for c in fake.calls)

    def test_tran_missing_tstop(self, tmp_path):
        sch = _schematic(tmp_path)
        fake = _make_fake_run()
        result = run_simulation(
            sch,
            analysis="tran",
            params={"tstep": "1u"},
            signals=["v(out)"],
            kicad_cli="kicad-cli",
            run=fake,
        )
        assert result["success"] is False
        assert "tstop" in result["message"]
        assert fake.calls == []  # validated before any subprocess ran

    def test_dc_missing_source(self, tmp_path):
        sch = _schematic(tmp_path)
        result = run_simulation(
            sch,
            analysis="dc",
            params={"start": 0, "stop": 5, "step": 0.1},
            signals=["v(out)"],
            kicad_cli="kicad-cli",
            run=_make_fake_run(),
        )
        assert result["success"] is False
        assert "source" in result["message"]

    def test_signals_required_for_tran(self, tmp_path):
        sch = _schematic(tmp_path)
        result = run_simulation(
            sch,
            analysis="tran",
            params={"tstep": "1u", "tstop": "1m"},
            kicad_cli="kicad-cli",
            run=_make_fake_run(),
        )
        assert result["success"] is False
        assert "signals" in result["message"]

    def test_unknown_analysis(self, tmp_path):
        sch = _schematic(tmp_path)
        result = run_simulation(sch, analysis="noise", run=_make_fake_run())
        assert result["success"] is False
        assert "noise" in result["message"]

    def test_missing_schematic(self):
        result = run_simulation("/nonexistent/x.kicad_sch", analysis="op", run=_make_fake_run())
        assert result["success"] is False
        assert "not found" in result["message"].lower()

    def test_never_raises(self, tmp_path):
        sch = _schematic(tmp_path)

        def exploding_run(cmd: List[str], **kwargs: Any) -> _FakeProc:
            raise RuntimeError("boom")

        result = run_simulation(sch, analysis="op", kicad_cli="kicad-cli", run=exploding_run)
        assert result["success"] is False
        assert "boom" in result["message"]
