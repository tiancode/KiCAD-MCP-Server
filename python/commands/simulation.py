"""ngspice batch-simulation runner.

Exports a SPICE netlist from a KiCad 10 schematic via ``kicad-cli sch export
netlist --format spice``, wraps it in an ngspice ``.control`` deck, runs
``ngspice -b`` and parses the results into plain JSON-friendly dicts.

Analyses supported: ``op``, ``tran``, ``dc``, ``ac``.

Data formats returned by :func:`run_simulation`:

* ``op``   — ``data`` is a flat ``{name: float}`` mapping of node voltages
  and branch currents parsed from ngspice's ``print all`` output (branch
  currents appear as e.g. ``v1#branch``).
* ``tran`` / ``dc`` — ``data`` is ``{"x": [...], "signals": {sig: [...]}}``
  parsed from a ``wrdata`` file (ngspice writes ``x y`` column pairs per
  signal on each row).
* ``ac``   — ngspice's ``wrdata`` emits complex vectors as ``x real imag``
  column triples, so each signal is returned as
  ``{"real": [...], "imag": [...]}`` (and ``data["complex"]`` is ``True``).
  Magnitude/phase conversion is intentionally left to the caller.

Everything is testable without ngspice/kicad-cli installed: the ``run``
parameter replaces ``subprocess.run`` and the parsers/deck builder are pure
module-level functions.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("commands.simulation")

VALID_ANALYSES = ("op", "tran", "dc", "ac")

_NGSPICE_INSTALL_HINT = (
    "Install ngspice and make sure it is on PATH: 'sudo apt install ngspice' "
    "(Debian/Ubuntu), 'brew install ngspice' (macOS) or 'winget install ngspice' (Windows)."
)
_EXPORT_FAIL_HINT = (
    "Check that every symbol in the schematic resolves (project sym-lib-table or embedded "
    "lib symbols) and that components carry SPICE model fields; opening the schematic in "
    "KiCad 10 and running Inspect > Simulator once usually surfaces the offending symbol."
)


def _resolve_kicad_cli() -> Optional[str]:
    """Locate kicad-cli, delegating to the repo-wide single source of truth."""
    try:
        # Same resolution used by DesignRuleCommands._find_kicad_cli et al.
        from utils.kicad_cli import find_kicad_cli

        return find_kicad_cli()
    except ImportError:  # pragma: no cover - only when utils package is unavailable
        # Fallback mirrors utils/kicad_cli.py: PATH lookup only.
        return shutil.which("kicad-cli.exe") or shutil.which("kicad-cli")


def _excerpt(text: str, limit: int = 400) -> str:
    """Return the tail of ``text`` (where CLI tools put the actual error), trimmed."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]


def _require(params: Dict[str, Any], keys: List[str], analysis: str) -> None:
    """Raise ValueError naming any keys missing from ``params``."""
    missing = [k for k in keys if params.get(k) is None]
    if missing:
        raise ValueError(
            f"Missing required parameter(s) for '{analysis}' analysis: {', '.join(missing)}"
        )


def _analysis_command(analysis: str, params: Dict[str, Any]) -> str:
    """Build the ngspice dot-command line for ``analysis``.

    Raises ValueError with a message naming missing/invalid parameters.
    """
    if analysis == "op":
        return "op"
    if analysis == "tran":
        _require(params, ["tstep", "tstop"], "tran")
        cmd = f"tran {params['tstep']} {params['tstop']}"
        if params.get("tstart") is not None:
            cmd += f" {params['tstart']}"
        return cmd
    if analysis == "dc":
        _require(params, ["source", "start", "stop", "step"], "dc")
        return f"dc {params['source']} {params['start']} {params['stop']} {params['step']}"
    if analysis == "ac":
        _require(params, ["points", "fstart", "fstop"], "ac")
        variation = str(params.get("variation", "dec")).lower()
        if variation not in ("dec", "oct", "lin"):
            raise ValueError(
                f"Invalid ac 'variation' {variation!r}; expected one of: dec, oct, lin"
            )
        return f"ac {variation} {params['points']} {params['fstart']} {params['fstop']}"
    raise ValueError(f"Unknown analysis '{analysis}'; expected one of: {', '.join(VALID_ANALYSES)}")


def build_control_deck(
    netlist_text: str,
    analysis: str,
    params: Optional[Dict[str, Any]],
    signals: Optional[List[str]],
    data_path: str,
) -> str:
    """Turn an exported SPICE netlist into a batch-mode ngspice deck.

    Strips any existing ``.end`` line, appends a ``.control`` block running
    the requested analysis (``print all`` for op, ``wrdata`` to ``data_path``
    for tran/dc/ac), a ``quit``, then ``.endc`` and a single trailing ``.end``.

    Raises ValueError for unknown analyses, missing parameters, or missing
    signals on non-op analyses.
    """
    analysis = (analysis or "").strip().lower()
    command = _analysis_command(analysis, params or {})
    if analysis != "op" and not signals:
        raise ValueError(f"signals is required for '{analysis}' analysis")

    lines = [line for line in netlist_text.splitlines() if line.strip().lower() != ".end"]
    while lines and not lines[-1].strip():
        lines.pop()

    lines += ["", ".control", command]
    if analysis == "op":
        lines.append("print all")
    else:
        lines.append(f"wrdata {data_path} {' '.join(signals or [])}")
    lines += ["quit", ".endc", ".end"]
    return "\n".join(lines) + "\n"


def parse_op_output(stdout: str) -> Dict[str, float]:
    """Parse ngspice ``print all`` stdout into ``{name: value}``.

    Recognises lines of the form ``name = value`` (e.g. ``out = 2.5e+00``,
    ``v1#branch = -2.5e-03``); everything else (banner, headers) is ignored.
    """
    results: Dict[str, float] = {}
    for line in stdout.splitlines():
        match = re.match(r"^\s*([^\s=]+)\s*=\s*([-+]?[0-9][0-9.eE+-]*)\s*$", line)
        if not match:
            continue
        try:
            results[match.group(1)] = float(match.group(2))
        except ValueError:
            continue
    return results


def parse_wrdata_file(text: str, signals: List[str]) -> Dict[str, Any]:
    """Parse an ngspice ``wrdata`` output file.

    ngspice writes one row per point with an ``x value`` column pair per
    signal (``x y1 x y2 ...``); for complex (ac) vectors each signal is an
    ``x real imag`` triple. Returns ``{"x": [...], "signals": {...},
    "complex": bool}`` where each signal maps to a list of floats, or to
    ``{"real": [...], "imag": [...]}`` for complex data.

    Raises ValueError if the column count matches neither layout.
    """
    n_signals = len(signals)
    rows: List[List[float]] = []
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        try:
            rows.append([float(f) for f in fields])
        except ValueError:
            continue  # header/comment line
    if not rows or n_signals == 0:
        return {"x": [], "signals": {sig: [] for sig in signals}, "complex": False}

    ncols = len(rows[0])
    rows = [r for r in rows if len(r) == ncols]
    if ncols == 3 * n_signals:
        is_complex = True
    elif ncols == 2 * n_signals:
        is_complex = False
    else:
        raise ValueError(f"Unexpected wrdata layout: {ncols} column(s) for {n_signals} signal(s)")

    width = 3 if is_complex else 2
    parsed_signals: Dict[str, Any] = {}
    for i, sig in enumerate(signals):
        base = i * width
        if is_complex:
            parsed_signals[sig] = {
                "real": [r[base + 1] for r in rows],
                "imag": [r[base + 2] for r in rows],
            }
        else:
            parsed_signals[sig] = [r[base + 1] for r in rows]
    return {"x": [r[0] for r in rows], "signals": parsed_signals, "complex": is_complex}


def downsample(rows: List[Any], max_points: int) -> List[Any]:
    """Evenly downsample ``rows`` to at most ``max_points`` items.

    First and last elements are always preserved. Index selection depends
    only on ``len(rows)`` and ``max_points``, so applying this to several
    equal-length arrays keeps them aligned.
    """
    n = len(rows)
    max_points = max(2, max_points)
    if n <= max_points:
        return list(rows)
    step = (n - 1) / (max_points - 1)
    return [rows[round(i * step)] for i in range(max_points)]


def _normalize_op_name(sig: str) -> str:
    """Map a requested signal like ``v(out)``/``i(V1)`` to ngspice op-output naming."""
    s = sig.strip().lower()
    match = re.fullmatch(r"v\((.+)\)", s)
    if match:
        return match.group(1)
    match = re.fullmatch(r"i\((.+)\)", s)
    if match:
        return match.group(1) + "#branch"
    return s


def _stderr_warnings(stderr: str, cap: int = 20) -> List[str]:
    """Collect non-empty stderr lines (capped) as warnings."""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    return lines[:cap]


def run_simulation(
    schematic_path: str,
    *,
    analysis: str,
    params: Optional[Dict[str, Any]] = None,
    signals: Optional[List[str]] = None,
    max_points: int = 2000,
    timeout: float = 120.0,
    kicad_cli: Optional[str] = None,
    ngspice: Optional[str] = None,
    run: Optional[Callable[..., Any]] = None,
    workdir: Optional[str] = None,
) -> Dict[str, Any]:
    """Run an ngspice batch simulation of a KiCad schematic.

    Exports the schematic to a SPICE netlist with kicad-cli (KiCad 10),
    builds a ``.control`` deck for the requested analysis, runs
    ``ngspice -b`` and parses the results (see module docstring for the
    per-analysis ``data`` shapes).

    Args:
        schematic_path: Path to the ``.kicad_sch`` file.
        analysis: One of ``op``, ``tran``, ``dc``, ``ac``.
        params: Analysis parameters —
            tran: ``{"tstep": "1u", "tstop": "10m", "tstart": optional}``;
            dc: ``{"source": "V1", "start": 0, "stop": 5, "step": 0.1}``;
            ac: ``{"variation": "dec", "points": 10, "fstart": 1, "fstop": "1Meg"}``.
        signals: Vectors to record, e.g. ``["v(out)", "i(V1)"]``. Required
            for tran/dc/ac; for op the full ``print all`` output is returned
            (filtered to ``signals`` when given and matchable).
        max_points: Downsample tran/dc/ac data to at most this many rows.
        timeout: Per-subprocess timeout in seconds.
        kicad_cli / ngspice: Binary path overrides (test injection).
        run: Replacement for ``subprocess.run`` (test injection).
        workdir: Directory for the netlist/deck/data files (a fresh temp
            directory by default).

    Returns:
        On success: ``{"success": True, "analysis", "engine": "ngspice",
        "netlistPath", "data", "pointsReturned", "truncated", "warnings"}``.
        On failure: ``{"success": False, "message": ..., "hint": optional}``.
        Never raises.
    """
    try:
        runner: Callable[..., Any] = run if run is not None else subprocess.run
        params = params or {}

        if not schematic_path:
            return {"success": False, "message": "schematic_path is required"}
        if not os.path.exists(schematic_path):
            return {"success": False, "message": f"Schematic not found: {schematic_path}"}

        analysis = (analysis or "").strip().lower()
        try:
            _analysis_command(analysis, params)  # early validation
        except ValueError as exc:
            return {"success": False, "message": str(exc)}
        if analysis != "op" and not signals:
            return {
                "success": False,
                "message": f"signals is required for '{analysis}' analysis",
                "hint": 'Pass the vectors to record, e.g. ["v(out)", "i(V1)"].',
            }

        cli = kicad_cli or _resolve_kicad_cli()
        if not cli:
            return {
                "success": False,
                "message": "kicad-cli not found",
                "hint": "Install KiCad 10 or pass kicad_cli explicitly; kicad-cli must be "
                "on PATH or in a standard install location.",
            }

        if workdir is None:
            workdir = tempfile.mkdtemp(prefix="kicad_sim_")
        else:
            os.makedirs(workdir, exist_ok=True)

        # -- 1. Export SPICE netlist ------------------------------------
        netlist_path = os.path.join(workdir, "circuit.cir")
        export_cmd = [
            cli,
            "sch",
            "export",
            "netlist",
            "--format",
            "spice",
            "-o",
            netlist_path,
            schematic_path,
        ]
        logger.info(f"Exporting SPICE netlist: {' '.join(export_cmd)}")
        try:
            export_proc = runner(export_cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            return {
                "success": False,
                "message": f"kicad-cli not found: {cli}",
                "hint": "Install KiCad 10 (kicad-cli ships with it).",
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "message": f"kicad-cli netlist export timed out after {timeout} seconds",
            }
        export_rc = getattr(export_proc, "returncode", 1)
        if export_rc != 0 or not os.path.exists(netlist_path):
            stderr = _excerpt(getattr(export_proc, "stderr", "") or "")
            detail = f" (exit {export_rc})" if export_rc != 0 else " (no netlist written)"
            return {
                "success": False,
                "message": f"kicad-cli spice netlist export failed{detail}: "
                f"{stderr or 'no stderr output'}",
                "hint": _EXPORT_FAIL_HINT,
            }

        # -- 2. Build the control deck -----------------------------------
        with open(netlist_path, "r", encoding="utf-8") as fh:
            netlist_text = fh.read()
        data_path = os.path.join(workdir, "data.txt")
        deck = build_control_deck(netlist_text, analysis, params, signals, data_path)
        deck_path = os.path.join(workdir, "deck.cir")
        with open(deck_path, "w", encoding="utf-8") as fh:
            fh.write(deck)

        # -- 3. Run ngspice in batch mode ---------------------------------
        ngspice_bin = ngspice or "ngspice"
        sim_cmd = [ngspice_bin, "-b", deck_path]
        logger.info(f"Running: {' '.join(sim_cmd)}")
        try:
            sim_proc = runner(sim_cmd, capture_output=True, text=True, timeout=timeout, cwd=workdir)
        except FileNotFoundError:
            return {
                "success": False,
                "message": "ngspice not found",
                "hint": _NGSPICE_INSTALL_HINT,
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "message": f"ngspice timed out after {timeout} seconds",
            }

        sim_stdout = getattr(sim_proc, "stdout", "") or ""
        sim_stderr = getattr(sim_proc, "stderr", "") or ""
        warnings = _stderr_warnings(sim_stderr)
        sim_rc = getattr(sim_proc, "returncode", 1)
        if sim_rc != 0:
            return {
                "success": False,
                "message": f"ngspice failed (exit {sim_rc}): "
                f"{_excerpt(sim_stderr) or _excerpt(sim_stdout) or 'no output'}",
                "netlistPath": netlist_path,
                "warnings": warnings,
            }

        # -- 4. Parse results ---------------------------------------------
        result: Dict[str, Any] = {
            "success": True,
            "analysis": analysis,
            "engine": "ngspice",
            "netlistPath": netlist_path,
            "warnings": warnings,
        }
        if analysis == "op":
            values = parse_op_output(sim_stdout)
            if signals:
                wanted = {_normalize_op_name(s) for s in signals}
                filtered = {k: v for k, v in values.items() if k.lower() in wanted}
                if filtered:
                    values = filtered
                else:
                    warnings.append(
                        "Requested signals did not match any operating-point results; "
                        "returning all values."
                    )
            if not values:
                warnings.append("No operating-point values parsed from ngspice output.")
            result["data"] = values
            result["pointsReturned"] = len(values)
            result["truncated"] = False
            return result

        if not os.path.exists(data_path):
            return {
                "success": False,
                "message": "ngspice produced no data file: "
                f"{_excerpt(sim_stderr) or _excerpt(sim_stdout) or 'no output'}",
                "netlistPath": netlist_path,
                "warnings": warnings,
            }
        with open(data_path, "r", encoding="utf-8") as fh:
            parsed = parse_wrdata_file(fh.read(), list(signals or []))
        if not parsed["x"]:
            return {
                "success": False,
                "message": "ngspice data file contained no data rows",
                "netlistPath": netlist_path,
                "warnings": warnings,
            }

        total = len(parsed["x"])
        signal_data: Dict[str, Any] = {}
        for name, vals in parsed["signals"].items():
            if isinstance(vals, dict):  # complex (ac) vector
                signal_data[name] = {
                    "real": downsample(vals["real"], max_points),
                    "imag": downsample(vals["imag"], max_points),
                }
            else:
                signal_data[name] = downsample(vals, max_points)
        x_values = downsample(parsed["x"], max_points)
        data: Dict[str, Any] = {"x": x_values, "signals": signal_data}
        if parsed["complex"]:
            data["complex"] = True
        result["data"] = data
        result["pointsReturned"] = len(x_values)
        result["truncated"] = total > len(x_values)
        return result

    except Exception as e:  # API boundary; bucket: catch + return
        logger.error(f"Error running simulation: {e}")
        return {"success": False, "message": f"Simulation failed: {e}"}
