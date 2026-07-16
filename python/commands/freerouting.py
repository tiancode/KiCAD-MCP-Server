"""
Freerouting autoroute integration for KiCAD MCP Server.

Exports the board to Specctra DSN format, runs Freerouting CLI,
and imports the routed SES file back into the board.

Supports two execution modes:
  - Direct: java -jar freerouting.jar (requires Java 21+)
  - Docker: docker run eclipse-temurin:21-jre (requires Docker)
"""

import glob
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger("kicad_interface")

# Default Freerouting JAR location
DEFAULT_FREEROUTING_JAR = os.environ.get(
    "FREEROUTING_JAR",
    os.path.join(os.path.expanduser("~"), ".kicad-mcp", "freerouting.jar"),
)


def _resolve_freerouting_jar(requested: str) -> Optional[str]:
    """Return the actual .jar file to use, or None if nothing's available.

    GitHub releases ship versioned filenames (``freerouting-2.2.4.jar``)
    rather than the bare ``freerouting.jar`` the default path expects.
    When the exact path doesn't exist, look in the same directory for
    ``freerouting-*.jar`` and pick the lexicographically-latest match —
    which works as a version sort for the simple ``vX.Y.Z`` scheme
    upstream uses.  Returns the absolute path of whatever lands.

    Callers should treat this as the canonical "what JAR will autoroute
    actually run" answer; the user-facing ``check_freerouting`` surfaces
    both the requested path and the resolved one when they differ.
    """
    if os.path.isfile(requested):
        return requested
    parent = os.path.dirname(requested) or "."
    if not os.path.isdir(parent):
        return None
    candidates = sorted(
        glob.glob(os.path.join(parent, "freerouting-*.jar")),
        reverse=True,  # newest version first
    )
    return candidates[0] if candidates else None


DOCKER_IMAGE = "eclipse-temurin:21-jre"

# Default schedule of `-mp` (max passes) values used when ``attempts`` > 1.
# Cycles through a range that empirically produces enough variation between
# runs to surface a better result than any single fixed value. Ported from
# morningfire-pcb-automation/scripts/routing/freeroute_runner.py.
DEFAULT_PASS_SCHEDULE = [50, 60, 65, 70, 75, 80, 85, 90, 55, 95]


def _find_java() -> Optional[str]:
    """Find java executable on the system."""
    java = shutil.which("java")
    if java:
        return java
    for candidate in [
        "/usr/bin/java",
        "/usr/local/bin/java",
        os.path.expandvars("$JAVA_HOME/bin/java"),
    ]:
        if os.path.isfile(candidate):
            return candidate
    return None


def _find_docker() -> Optional[str]:
    """Find docker executable on the system."""
    return shutil.which("docker") or shutil.which("podman")


def _docker_available() -> bool:
    """Check if Docker/Podman is available and running."""
    docker = _find_docker()
    if not docker:
        return False
    try:
        proc = subprocess.run(
            [docker, "info"],
            capture_output=True,
            timeout=10,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        # Daemon not running (OSError on connect refusal), or hung past 10s
        # (TimeoutExpired).  Either way Docker is "not available" for our use.
        return False


def _java_version_ok(java_exe: str) -> bool:
    """Check if local Java is version 21+."""
    try:
        proc = subprocess.run(
            [java_exe, "-version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = proc.stderr or proc.stdout
        # Parse version like: openjdk version "17.0.18"
        for line in output.split("\n"):
            if "version" in line:
                ver = line.split('"')[1] if '"' in line else ""
                major = int(ver.split(".")[0])
                return major >= 21
    except Exception:
        # Probe function: any failure (missing binary, timeout, garbage
        # version string, …) means "we can't confirm Java 21+".  Caller
        # falls back to Docker.  Broad catch is intentional here — the
        # test suite explicitly exercises a generic-Exception side_effect
        # to assert this guard never throws.
        pass
    return False


def _build_freerouting_cmd(
    jar_path: str,
    dsn_path: str,
    ses_path: str,
    passes: int,
    use_docker: bool,
    single_thread: bool = False,
) -> List[str]:
    """Build the command to run Freerouting.

    ``single_thread`` forces ``-mt 1`` (single-threaded optimisation).
    Freerouting 2.x's multi-threaded optimiser is documented to produce
    clearance violations in some cases (the runtime even prints a warning);
    best-of-N callers should pass this so each attempt's score reflects a
    valid routed board, not an artefact of MT optimisation.
    """
    extra = ["-mt", "1"] if single_thread else []
    if use_docker:
        docker_exe = _find_docker()
        if docker_exe is None:
            raise RuntimeError("Docker/Podman executable not found")
        board_dir = os.path.dirname(dsn_path)
        dsn_name = os.path.basename(dsn_path)
        ses_name = os.path.basename(ses_path)
        jar_name = os.path.basename(jar_path)
        return [
            docker_exe,
            "run",
            "--rm",
            "-v",
            f"{jar_path}:/app/{jar_name}:ro",
            "-v",
            f"{board_dir}:/work",
            DOCKER_IMAGE,
            "java",
            "-jar",
            f"/app/{jar_name}",
            "-de",
            f"/work/{dsn_name}",
            "-do",
            f"/work/{ses_name}",
            "-mp",
            str(passes),
            *extra,
        ]
    else:
        java_exe = _find_java()
        if java_exe is None:
            raise RuntimeError("Java executable not found")
        return [
            java_exe,
            "-jar",
            jar_path,
            "-de",
            dsn_path,
            "-do",
            ses_path,
            "-mp",
            str(passes),
            *extra,
        ]


# ---------------------------------------------------------------------------
# Best-of-N scoring helpers (ported from morningfire-pcb-automation)
# ---------------------------------------------------------------------------
#
# Approach lifted from
#   https://github.com/NiNjA-CodE/morningfire-pcb-automation
#   scripts/routing/freeroute_runner.py::score_ses
#
# Single-shot Freerouting on dense boards routinely leaves 1–7 nets
# unrouted. Re-running with varied --max-passes values surfaces a better
# solution most of the time; the scoring function below picks the best
# SES across attempts.
# ---------------------------------------------------------------------------

_SES_NET_RE = re.compile(r"\(net\s+(\S+)\s*\n\s*\(wire")


def _score_ses(ses_text: str, target_nets: Iterable[str]) -> Dict[str, Any]:
    """Score a Specctra SES file by routing completeness.

    Score = (nets_routed * 1000) + segments + 50000_if_all_targets_routed

    The ``nets_routed * 1000`` term dominates segment count so an attempt
    that routes one more net always beats an attempt with marginally more
    segments. The target-net bonus is huge so any attempt that routes all
    critical nets wins, regardless of segment count.

    Returns: ``{"score": int, "nets": int, "segments": int, "vias": int,
                "targets_found": [...], "targets_missing": [...]}``
    """
    nets = set(_SES_NET_RE.findall(ses_text))
    # Strip wrapping quotes if Freerouting emits them.
    clean_nets = {n.strip('"') for n in nets}
    segments = len(re.findall(r"\(wire", ses_text))
    vias = len(re.findall(r"\(via ", ses_text))

    targets = set(target_nets) if target_nets else set()
    found = sorted(targets & clean_nets)
    missing = sorted(targets - clean_nets)

    score = len(clean_nets) * 1000 + segments
    if targets and not missing:
        score += 50_000

    return {
        "score": score,
        "nets": len(clean_nets),
        "segments": segments,
        "vias": vias,
        "targets_found": found,
        "targets_missing": missing,
    }


# ---------------------------------------------------------------------------
# Freerouting failure detection
# ---------------------------------------------------------------------------
#
# Freerouting 2.2.4 can hit a fatal error mid-run (e.g. the
# ``NullPointerException: "to_trace_entries" is null`` in
# ``ShapeSearchTree.merge_entries_in_front`` that fires on boards carrying
# pre-routed traces) yet still **exit 0 and write a SES file** — the SES is
# merely an echo of the input wiring with nothing new routed.  A clean exit
# code is therefore NOT proof of a successful route; the stdout/stderr stream
# has to be scanned for the fatal signatures below.  Reported as E2E finding
# B4.
# ---------------------------------------------------------------------------

_FATAL_FR_PATTERNS = [
    # The specific fatal log line Freerouting prints when a routing pass
    # throws — observed verbatim in the B4 crash.
    re.compile(r"ERROR\s+Error during routing passes", re.IGNORECASE),
    # Java stack-trace markers.  Kept specific (``java.lang.…Exception`` /
    # ``…Error``, ``Exception in thread``, an ``at pkg.Class.method(File:line)``
    # frame) so normal INFO/WARN routing chatter never trips the detector.
    re.compile(r"Exception in thread"),
    re.compile(r"java\.[\w.]*\.\w*(?:Exception|Error)\b"),
    re.compile(r"^\s*at\s+[\w.$]+\([\w.$]+:\d+\)"),
    re.compile(r"\bFATAL\b"),
]


def _detect_routing_failure(output: str) -> Optional[str]:
    """Return the most diagnostic fatal line in Freerouting output, or None.

    ``output`` is the combined stdout+stderr of one Freerouting invocation.
    Returns the offending line (the exception message, preferentially) so the
    caller can surface it to the user; returns ``None`` when the run looks
    clean.  Freerouting exiting 0 does NOT imply success — this scan is the
    authoritative signal (see the module note above).
    """
    if not output:
        return None
    matches: List[str] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        for pat in _FATAL_FR_PATTERNS:
            if pat.search(line):
                matches.append(line)
                break
    if not matches:
        return None
    # Prefer a line that actually names the exception — it's the most useful
    # thing to put in front of the user.
    for m in matches:
        if "Exception" in m or "Error:" in m:
            return m
    return matches[0]


# Net token inside a SES block: either a quoted "name with spaces" or a bare
# token up to the next whitespace / closing paren.
_SES_NET_TOKEN_RE = re.compile(r'\(net\s+("(?:[^"\\]|\\.)*"|[^\s)]+)')


def _ses_routed_nets(ses_text: str) -> set:
    """Net names that carry at least one wire or via in a SES file.

    Only these nets should have their existing board routing replaced before
    import — a net that appears in the SES with no wire/via must be left
    untouched, or we'd delete routing the import won't restore.  Used to give
    ``ImportSpecctraSES`` KiCad's native *replace* semantics instead of the
    *stack* behaviour that duplicated pre-routed traces in E2E finding B4.
    """
    if not ses_text:
        return set()
    matches = list(_SES_NET_TOKEN_RE.finditer(ses_text))
    nets: set = set()
    for i, m in enumerate(matches):
        name = m.group(1).strip('"')
        block_start = m.end()
        block_end = matches[i + 1].start() if i + 1 < len(matches) else len(ses_text)
        block = ses_text[block_start:block_end]
        if "(wire" in block or "(via " in block or "(via(" in block:
            nets.add(name)
    return nets


# NOTE (E2E finding B4, prevention item 3 — investigated + rejected):
# Rewriting pre-routed wires in the exported DSN from ``(type route)`` to
# ``(type protect)`` was tried as a way to sidestep the upstream Freerouting
# 2.2.4 crash (``to_trace_entries`` NPE). Against the real binary it did NOT
# work: on the fully pre-routed ESP32-C3 board the protect variant crashed
# just as often as the route variant (both ~5/5 runs), because Freerouting
# normalises protected traces too. The NPE is a multithread race in
# ShapeSearchTree.merge_entries_in_front that pre-routed traces trigger
# regardless of wire type. We therefore rely on honest-failure detection +
# SES replace-semantics instead of a DSN rewrite.


# ---------------------------------------------------------------------------
# DSN pre-routing / plane stripping (E2E finding B6)
# ---------------------------------------------------------------------------
#
# Freerouting 2.2.4 StackOverflows / NPEs in its DSN "Opening" phase on boards
# whose exported DSN carries a ``(wiring …)`` block of pre-routed traces and/or
# full-board ``(plane …)`` copper pours (the B6 crash: 2 planes + 48 wires ->
# java.lang.StackOverflowError at Simplex.to_IntOctagon). Removing those blocks
# from the DSN handed to Freerouting sidesteps the crash. This rewrites ONLY the
# ``.dsn`` fed to the router — never the ``.kicad_pcb`` — and is gated by the
# includePreRoutes (default False = strip the wiring, the actual crash trigger)
# and includePlanes (default True = keep the planes; stripping them turns the
# GND tree into a trace-routing job that times out) params.
# ---------------------------------------------------------------------------


def _skip_balanced_sexpr(text: str, start: int) -> int:
    """Return the index just past the ``)`` that closes the S-expression that
    begins at ``text[start]`` (which must be ``(``).

    Quoted strings (Specctra quote char ``"``) are skipped so parens inside net
    names like ``"unconnected-(J1-CC1-PadA5)"`` never unbalance the count.

    The header's ``(string_quote ")`` block is skipped wholesale: its lone
    literal ``"`` is the quote-char *definition*, not a string delimiter —
    naive toggling on it would leave the scanner stuck "inside" a string for
    the rest of the file (round-7 live-smoke finding).
    """
    depth = 0
    i = start
    n = len(text)
    in_quote = False
    while i < n:
        c = text[i]
        if in_quote:
            if c == '"':
                in_quote = False
        elif c == "(" and _is_string_quote_block(text, i):
            j = text.find(")", i)
            if j == -1:
                return n
            i = j + 1
            continue
        elif c == '"':
            in_quote = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


_STRING_QUOTE_TOKEN = "(string_quote"


def _is_string_quote_block(text: str, i: int) -> bool:
    """True when ``text[i:]`` opens the Specctra ``(string_quote …)`` block."""
    if not text.startswith(_STRING_QUOTE_TOKEN, i):
        return False
    nxt = text[i + len(_STRING_QUOTE_TOKEN)] if i + len(_STRING_QUOTE_TOKEN) < len(text) else ""
    return not (nxt.isalnum() or nxt in "_-")


def _remove_sexpr_blocks(text: str, keyword: str) -> "tuple[str, int]":
    """Remove every ``(keyword …)`` block from ``text`` (balanced, quote-aware).

    Returns ``(new_text, count_removed)``. A trailing run of spaces/tabs plus
    one newline after each removed block is consumed too so the file stays
    tidy. Only whole tokens match — with ``keyword='plane'`` a hypothetical
    ``(planet …)`` is left untouched. Parens inside quoted strings are ignored
    when both locating the token and balancing it.
    """
    opener = "(" + keyword
    ol = len(opener)
    out: List[str] = []
    i = 0
    n = len(text)
    removed = 0
    in_quote = False
    while i < n:
        c = text[i]
        if in_quote:
            out.append(c)
            if c == '"':
                in_quote = False
            i += 1
            continue
        if c == "(" and _is_string_quote_block(text, i):
            j = text.find(")", i)
            if j == -1:
                out.append(text[i:])
                i = n
                continue
            out.append(text[i : j + 1])
            i = j + 1
            continue
        if c == '"':
            in_quote = True
            out.append(c)
            i += 1
            continue
        if c == "(" and text.startswith(opener, i):
            nxt = text[i + ol] if i + ol < n else ""
            if not (nxt.isalnum() or nxt in "_-"):
                end = _skip_balanced_sexpr(text, i)
                removed += 1
                i = end
                while i < n and text[i] in " \t":
                    i += 1
                if i < n and text[i] == "\n":
                    i += 1
                continue
        out.append(c)
        i += 1
    return "".join(out), removed


def _strip_dsn_prerouting(
    dsn_text: str,
    include_pre_routes: bool = False,
    include_planes: bool = False,
) -> "tuple[str, Dict[str, Any]]":
    """Strip pre-routed wiring and/or copper planes from a Specctra DSN.

    ``include_pre_routes`` keeps the ``(wiring …)`` block of pre-routed traces;
    ``include_planes`` keeps the full-board ``(plane …)`` copper pours. Both
    default False (strip). Netclass ``(class …)`` blocks — including their
    Power/RF widths — are never touched, so stripping can't regress the
    netclass-aware DSN export (B7).

    Returns ``(new_text, {"wiring_removed": bool, "planes_removed": int})``.
    """
    text = dsn_text
    wiring_removed = 0
    planes_removed = 0
    if not include_pre_routes:
        text, wiring_removed = _remove_sexpr_blocks(text, "wiring")
    if not include_planes:
        text, planes_removed = _remove_sexpr_blocks(text, "plane")
    return text, {
        "wiring_removed": bool(wiring_removed),
        "planes_removed": planes_removed,
    }


class FreeroutingCommands:
    """Handles Freerouting autoroute operations."""

    def __init__(
        self,
        board: Any = None,
        signature_callback: Any = None,
        board_reload_callback: Any = None,
    ) -> None:
        self.board = board
        # Optional callback `fn(path)` invoked after this class saves the
        # board directly, so the parent KiCADInterface can keep its
        # in-memory disk signature in sync. Without it, _auto_save_board()
        # on the next mutation would see a stale hash and refuse.
        self._signature_callback = signature_callback
        # Optional callback `fn(path) -> bool` that asks the parent
        # KiCADInterface to replace its in-memory board with a fresh load of
        # ``path`` (rebinding every command handler). Autoroute uses it after
        # importing a SES into the currently-open project's file so later
        # reads serve the routed result.
        self._board_reload_callback = board_reload_callback

    def _save_and_record(self, board_path: str, board: Any = None) -> None:
        """Save ``board`` (default ``self.board``) and notify the parent (if any).

        Uses ``getattr`` so test fixtures that bypass ``__init__`` via
        ``__new__`` don't AttributeError — they simply skip the callback.
        """
        target = board if board is not None else self.board
        target.Save(board_path)
        cb = getattr(self, "_signature_callback", None)
        if cb is not None:
            try:
                cb(board_path)
            except Exception:
                logger.debug("Signature callback raised; ignoring", exc_info=True)

    def _board_routed_nets(self, board: Any = None) -> set:
        """Net names that currently have at least one track or via on the board.

        Used to tell "did the autoroute actually route anything new?" apart
        from "the SES is just an echo of the pre-existing routing" (the B4
        crash case). ``board`` defaults to ``self.board``.
        """
        board = board if board is not None else self.board
        nets: set = set()
        try:
            tracks = list(board.GetTracks())
        except Exception:
            return nets
        for t in tracks:
            try:
                name = t.GetNetname()
            except Exception:
                name = None
            if name:
                nets.add(name)
        return nets

    def _remove_tracks_on_nets(self, net_names: set, board: Any = None) -> int:
        """Delete every track/via whose net is in ``net_names``; return count.

        This is the "rip" half of KiCad's native Specctra *replace* semantics:
        before importing a SES we clear the existing routing on exactly the
        nets the SES will re-add, so the import replaces rather than stacks
        (which duplicated pre-routed traces — E2E finding B4). ``board``
        defaults to ``self.board``.

        Uses ``board.Delete`` (not ``Remove``) to match the rest of the code
        base: the KiCAD 10 SWIG bindings leak / corrupt the object table on
        ``Remove`` but free cleanly on ``Delete`` (see routing/_traces.py).
        """
        board = board if board is not None else self.board
        if not net_names:
            return 0
        removed = 0
        try:
            tracks = list(board.GetTracks())
        except Exception:
            return 0
        for t in tracks:
            try:
                name = t.GetNetname()
            except Exception:
                name = None
            if name in net_names:
                board.Delete(t)
                removed += 1
        return removed

    def _apply_ses(
        self,
        ses_path: str,
        board_path: Optional[str],
        board: Any = None,
        fire_signature: bool = True,
    ) -> Dict[str, Any]:
        """Import a SES with replace semantics, rebuild connectivity, then save.

        Removes existing tracks/vias on the nets the SES will re-route, runs
        ``ImportSpecctraSES``, rebuilds the board's connectivity (B8), and
        saves. ``board`` defaults to ``self.board``. When ``fire_signature`` is
        True the save goes through ``_save_and_record`` (the ``_on_swig_direct_
        save`` bookkeeping); routing callers pass False because they either
        reload the board afterward or wrote a file other than the open board.

        Returns ``{"ok": True, "removed_tracks": n, "replaced_nets": [...]}``
        on success, or ``{"ok": False, "error": {...response...}}`` on an
        import failure.
        """
        import pcbnew

        board = board if board is not None else self.board

        try:
            with open(ses_path, "r", encoding="utf-8", errors="replace") as fh:
                ses_text = fh.read()
        except OSError:
            ses_text = ""
        replace_nets = _ses_routed_nets(ses_text)
        removed = self._remove_tracks_on_nets(replace_nets, board)

        try:
            result = pcbnew.ImportSpecctraSES(board, ses_path)
            if result is not True and result != 0:
                return {
                    "ok": False,
                    "error": {
                        "success": False,
                        "message": "SES import failed",
                        "errorDetails": f"ImportSpecctraSES returned: {result}",
                    },
                }
        except Exception as e:
            # API boundary — pcbnew can raise C-level exceptions surfaced as
            # RuntimeError/generic Exception; return the caller-friendly shape.
            logger.exception(f"ImportSpecctraSES crashed: {e}")
            return {
                "ok": False,
                "error": {
                    "success": False,
                    "message": "SES import failed",
                    "errorDetails": str(e),
                },
            }

        # B8: ImportSpecctraSES leaves the connectivity graph stale, so a
        # run_drc immediately after import over-counts by one (a transient
        # marker that a later refresh clears — 280 vs kicad-cli's 279).
        # Rebuild it here so the saved file is already settled.
        try:
            if hasattr(board, "BuildListOfNets"):
                board.BuildListOfNets()
            if hasattr(board, "BuildConnectivity"):
                board.BuildConnectivity()
        except Exception:
            logger.debug(
                "Connectivity rebuild after SES import failed; ignoring",
                exc_info=True,
            )

        if board_path:
            try:
                if fire_signature:
                    self._save_and_record(board_path, board)
                else:
                    board.Save(board_path)
            except (OSError, RuntimeError) as e:
                # Non-fatal: the SES is imported; user can save manually.
                logger.warning(f"Board save after SES import failed: {e}")

        return {
            "ok": True,
            "removed_tracks": removed,
            "replaced_nets": sorted(replace_nets),
        }

    def _board_track_stats(self, board: Any = None) -> Dict[str, int]:
        """Return ``{"tracks": n, "vias": m}`` for ``board`` (default self.board)."""
        board = board if board is not None else self.board
        track_count = 0
        via_count = 0
        for t in board.GetTracks():
            if t.GetClass() == "PCB_VIA":
                via_count += 1
            else:
                track_count += 1
        return {"tracks": track_count, "vias": via_count}

    def _safe_fresh_load(self, path: str, pcbnew: Any) -> Any:
        """Load a fresh BOARD from ``path`` via ``pcbnew.LoadBoard``.

        Returns the loaded board, or None on failure (caller surfaces a real
        error rather than routing a stale in-memory board — E2E finding B5).
        """
        try:
            return pcbnew.LoadBoard(path)
        except Exception as e:
            logger.error(f"LoadBoard({path!r}) failed: {e}")
            return None

    def _prepare_target_board(self, params: Dict[str, Any], pcbnew: Any) -> Dict[str, Any]:
        """Resolve which board to operate on (E2E finding B5 fresh-load).

        ``boardPath`` (or, when omitted, the currently-open board's own file)
        is the file that actually gets routed/saved — never a different
        in-memory board. Semantics (orchestrator decision 4):

          * boardPath names the currently-open board's file (or is omitted):
            flush the in-memory edits to that file, then load it FRESH and
            operate on the copy. The caller reloads the parent afterward so
            later reads serve the routed result. (``external=False``)
          * boardPath names a different, existing file: load it FRESH and
            operate on that; the open project board is left untouched.
            (``external=True``)
          * boardPath names a nonexistent file: FILE_NOT_FOUND.

        A fresh LoadBoard also re-reads the sibling ``.kicad_pro`` netclasses,
        which is why it fixes the single-class DSN export (B7).

        Returns ``{"ok": True, "board", "board_path", "external"}`` or
        ``{"ok": False, "error": {...}}``.
        """
        requested = params.get("boardPath")
        loaded_path = self.board.GetFileName() if self.board else None
        board_path = requested or loaded_path

        if not board_path:
            return {
                "ok": False,
                "error": {
                    "success": False,
                    "message": "No board file path available",
                    "errorDetails": "Provide boardPath or open a project first",
                },
            }

        external = bool(requested) and (
            not loaded_path or os.path.abspath(requested) != os.path.abspath(loaded_path)
        )

        if external:
            if not os.path.isfile(board_path):
                return {
                    "ok": False,
                    "error": {
                        "success": False,
                        "errorCode": "FILE_NOT_FOUND",
                        "message": "Board file not found",
                        "errorDetails": f"No such file: {board_path}",
                    },
                }
            board = self._safe_fresh_load(board_path, pcbnew)
            if board is None:
                return {
                    "ok": False,
                    "error": {
                        "success": False,
                        "message": "Failed to load board",
                        "errorDetails": f"pcbnew.LoadBoard could not open {board_path}",
                    },
                }
            return {"ok": True, "board": board, "board_path": board_path, "external": True}

        # Same file as the open board (or boardPath omitted): flush in-memory
        # edits to disk first so the fresh load includes them, then reload.
        try:
            self._save_and_record(board_path)
        except (OSError, RuntimeError) as e:
            return {
                "ok": False,
                "error": {
                    "success": False,
                    "message": "Failed to save board before routing",
                    "errorDetails": str(e),
                },
            }
        board = self._safe_fresh_load(board_path, pcbnew)
        if board is None:
            return {
                "ok": False,
                "error": {
                    "success": False,
                    "message": "Failed to reload board",
                    "errorDetails": f"pcbnew.LoadBoard could not reopen {board_path}",
                },
            }
        return {"ok": True, "board": board, "board_path": board_path, "external": False}

    def _strip_dsn_file(
        self, dsn_path: str, include_pre_routes: bool, include_planes: bool
    ) -> Dict[str, Any]:
        """Rewrite ``dsn_path`` in place with pre-routing/planes stripped (B6).

        Never touches the ``.kicad_pcb`` — only the DSN handed to Freerouting.
        Returns the strip info dict (``{"wiring_removed", "planes_removed"}``).
        """
        if include_pre_routes and include_planes:
            return {"wiring_removed": False, "planes_removed": 0}
        try:
            with open(dsn_path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return {"wiring_removed": False, "planes_removed": 0}
        new_text, info = _strip_dsn_prerouting(text, include_pre_routes, include_planes)
        if new_text != text:
            try:
                with open(dsn_path, "w", encoding="utf-8") as fh:
                    fh.write(new_text)
            except OSError:
                logger.warning("Could not rewrite stripped DSN %s", dsn_path)
        return info

    def _finalize_board(self, board_path: str, external: bool) -> Dict[str, Any]:
        """Sync the parent after a routed board has been saved to disk.

        Same-file route: ask the parent to reload ``board_path`` (rebinding
        every handler) so later reads serve the routed result. External file:
        leave the open project board untouched and note that. Returns response
        fields to merge (``routed_board_path`` + optional ``note``).
        """
        fields: Dict[str, Any] = {"routed_board_path": board_path}
        if external:
            fields["note"] = (
                "The currently-open project board was not modified; the routed "
                f"result was saved to {board_path}."
            )
            return fields
        cb = getattr(self, "_board_reload_callback", None)
        if cb is not None:
            try:
                cb(board_path)
            except Exception:
                logger.warning("Board reload callback raised; ignoring", exc_info=True)
        return fields

    def _resolve_execution_mode(self, jar_path: str) -> Dict[str, Any]:
        """Determine how to run Freerouting: direct or docker.

        Returns dict with 'mode', 'use_docker', or 'error'.
        """
        java_exe = _find_java()
        if java_exe and _java_version_ok(java_exe):
            return {"mode": "direct", "use_docker": False}

        if _docker_available():
            return {"mode": "docker", "use_docker": True}

        if java_exe:
            return {
                "mode": "error",
                "error": (
                    f"Java found at {java_exe} but version < 21. "
                    "Freerouting 2.x requires Java 21+. "
                    "Install Java 21+ or Docker."
                ),
            }
        return {
            "mode": "error",
            "error": (
                "Neither Java 21+ nor Docker found. " "Install one of them to use Freerouting."
            ),
        }

    def autoroute(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Run Freerouting autorouter on the current board.

        Single-attempt flow (default):
            1. Export board to Specctra DSN
            2. Run Freerouting CLI on DSN -> SES (one pass with ``maxPasses``)
            3. Import SES back into the board
            4. Save the board

        Best-of-N flow (``attempts > 1``):
            1. Export DSN once
            2. Run Freerouting ``attempts`` times, varying ``--max-passes``
               per the ``passSchedule`` (defaults to a built-in schedule
               of 10 spread-out values).
            3. Score each SES by (nets_routed * 1000) + segments, plus a
               50,000-point bonus when every ``targetNets`` entry routed.
            4. Keep the highest-scoring SES; import that one into the board.

        Single-attempt behaviour is unchanged when ``attempts`` is omitted
        or set to 1, so existing callers do not need updates.

        The best-of-N scoring approach is ported from
        morningfire-pcb-automation
        (https://github.com/NiNjA-CodE/morningfire-pcb-automation,
        scripts/routing/freeroute_runner.py). On dense boards a single
        run regularly leaves 1–7 nets unrouted; cycling through a few
        ``-mp`` values typically gets the count to zero.

        Honest failure (E2E finding B4): Freerouting 2.2.4 can throw
        mid-run (the ``to_trace_entries`` NPE on boards with pre-routed
        traces), log ``ERROR Error during routing passes``, **exit 0**, and
        still write an echo SES. The stdout/stderr stream is scanned for the
        fatal signatures (``_detect_routing_failure``); a crashed pass never
        wins best-of-N, and a run that routed 0 new nets returns
        ``success: False`` with the exception line + a remediation hint
        instead of a fake ``success: True``. A partial crash (some new nets
        routed) imports what landed and returns ``routing_incomplete: True``
        + warnings.

        Replace semantics (B4): the SES import first clears existing
        tracks/vias on the nets the SES re-routes, so importing replaces
        rather than stacks (which duplicated pre-routed traces).

        Fresh-load target (E2E finding B5): ``boardPath`` (or, when omitted,
        the open board's own file) is loaded FRESH; the DSN is exported from
        and the SES imported into THAT board — never a stale in-memory board
        that would route/clobber the wrong file. A same-file route reloads the
        open project board afterward (so later reads serve the routed result);
        an external ``boardPath`` leaves the open board untouched and says so.
        The fresh load also re-reads the sibling ``.kicad_pro`` netclasses, so
        the DSN carries the real Power/RF widths (fixes B7). The routed file is
        returned as ``routed_board_path``.

        DSN stripping (E2E finding B6): the pre-routed ``(wiring …)`` block and
        full-board ``(plane …)`` copper — which crash Freerouting 2.2.4 in its
        DSN "Opening" phase — are stripped from the DSN handed to the router by
        default. ``includePreRoutes``/``includePlanes`` keep them. Stripping
        touches only the DSN, never the .kicad_pcb; note that a stripped GND
        plane is re-routed as ordinary traces rather than a poured pour.
        """
        try:
            import pcbnew
        except ImportError:
            return {
                "success": False,
                "message": "pcbnew not available",
                "errorDetails": "KiCAD Python API is required",
            }

        if not self.board:
            return {
                "success": False,
                "message": "No board is loaded",
                "errorDetails": "Load or create a board first",
            }

        requested_jar = params.get("freeroutingJar", DEFAULT_FREEROUTING_JAR)
        # Resolve versioned filenames (e.g. ``freerouting-2.2.4.jar``) so the
        # user doesn't have to rename the GitHub release download.
        jar_path = _resolve_freerouting_jar(requested_jar) or requested_jar
        timeout = params.get("timeout", 300)
        passes = params.get("maxPasses", 20)

        # B6 gates: strip the pre-routed ``(wiring …)`` block and/or full-board
        # ``(plane …)`` copper from the DSN handed to Freerouting (both default
        # to stripping). This only rewrites the DSN, never the .kicad_pcb.
        include_pre_routes = bool(params.get("includePreRoutes", False))
        # Planes are KEPT by default: live round-7 testing showed the crash
        # trigger is the pre-routed (wiring …) block alone — with planes kept
        # Freerouting 2.2.4 converges in seconds, while stripping the planes
        # turns the whole GND tree into an enormous trace-routing job that
        # times out. includePlanes=False strips them for the rare board where
        # the planes themselves break the DSN parse.
        include_planes = bool(params.get("includePlanes", True))

        # Best-of-N parameters
        attempts_raw = params.get("attempts", 1)
        try:
            attempts = int(attempts_raw) if attempts_raw is not None else 1
        except (TypeError, ValueError):
            return {
                "success": False,
                "message": "Invalid attempts value",
                "errorDetails": f"attempts must be a positive integer; got {attempts_raw!r}",
            }
        if attempts < 1:
            return {
                "success": False,
                "message": "Invalid attempts value",
                "errorDetails": "attempts must be >= 1",
            }
        target_nets = list(params.get("targetNets") or [])
        pass_schedule = list(params.get("passSchedule") or DEFAULT_PASS_SCHEDULE)
        if not pass_schedule:
            pass_schedule = [passes]

        # Validate Freerouting JAR (before any board side effects, so a missing
        # JAR never flushes/reloads the board).
        if not os.path.isfile(jar_path):
            return {
                "success": False,
                "message": "Freerouting JAR not found",
                "errorDetails": (
                    f"Expected at: {requested_jar}.  Also tried "
                    f"freerouting-*.jar in {os.path.dirname(requested_jar) or '.'}.  "
                    "Download from https://github.com/freerouting/freerouting/"
                    "releases or set FREEROUTING_JAR env var.  "
                    "Call check_freerouting for install instructions."
                ),
            }

        # Determine execution mode
        exec_mode = self._resolve_execution_mode(jar_path)
        if exec_mode["mode"] == "error":
            return {
                "success": False,
                "message": "No suitable Java runtime",
                "errorDetails": exec_mode["error"],
            }

        use_docker = exec_mode["use_docker"]

        # B5: resolve the board to route. boardPath (or, when omitted, the
        # currently-open board's own file) is loaded FRESH so the DSN reflects
        # the file named and the routed SES lands on that same file — never a
        # different in-memory board. Fresh LoadBoard also re-reads the sibling
        # .kicad_pro netclasses (fixes B7's single-class DSN).
        prep = self._prepare_target_board(params, pcbnew)
        if not prep["ok"]:
            return prep["error"]
        route_board = prep["board"]
        board_path = prep["board_path"]
        external = prep["external"]

        # Net names that already carry routing on the board being routed —
        # captured before we touch it so the failure path can tell "routed
        # something new" from "the SES is just an echo of the pre-existing
        # traces" (the B4 crash).
        pre_routed_nets = self._board_routed_nets(route_board)

        # Set up file paths
        board_dir = os.path.dirname(board_path)
        board_stem = Path(board_path).stem
        dsn_path = os.path.join(board_dir, f"{board_stem}.dsn")
        ses_path = os.path.join(board_dir, f"{board_stem}.ses")
        best_ses_path = os.path.join(board_dir, f"{board_stem}_best.ses")

        # Step 1: Export DSN from the freshly-loaded route board (once)
        logger.info(f"Exporting DSN to {dsn_path}")
        try:
            result = pcbnew.ExportSpecctraDSN(route_board, dsn_path)
            if result is not True and result != 0:
                return {
                    "success": False,
                    "message": "DSN export failed",
                    "errorDetails": (f"ExportSpecctraDSN returned: {result}"),
                }
        except Exception as e:
            # API boundary — pcbnew can raise C-level exceptions surfaced
            # as RuntimeError or generic Exception, plus OSError on the
            # file-write path.  Returning {success: False, ...} is the
            # caller-friendly shape; log the traceback so it's debuggable.
            logger.exception(f"ExportSpecctraDSN crashed: {e}")
            return {
                "success": False,
                "message": "DSN export failed",
                "errorDetails": str(e),
            }

        if not os.path.isfile(dsn_path):
            return {
                "success": False,
                "message": "DSN file was not created",
                "errorDetails": f"Expected at: {dsn_path}",
            }

        # B6: strip the pre-routed wiring / copper planes that crash
        # Freerouting 2.2.4 from the DSN fed to the router (never the board).
        strip_info = self._strip_dsn_file(dsn_path, include_pre_routes, include_planes)

        dsn_size = os.path.getsize(dsn_path)
        logger.info(f"DSN exported: {dsn_size} bytes")

        # Step 2: Run Freerouting (single or multiple attempts)
        mode_label = "docker" if use_docker else "direct"
        total_start = time.time()
        attempt_results: List[Dict[str, Any]] = []
        # Best CLEAN attempt (Freerouting reported no fatal error).
        best_score = -1
        best_attempt_idx = -1
        best_proc_stdout = ""
        # Best attempt that produced a SES but logged a FATAL error (the B4
        # NPE case: exit 0 + SES written, yet nothing meaningful routed). A
        # crashed pass must never win best-of-N over a clean one, so it's
        # tracked separately and only consulted when no clean attempt exists.
        failed_best_score = -1
        failed_best_idx = -1
        failed_best_stdout = ""
        failed_best_error = ""
        failed_ses_path = os.path.join(board_dir, f"{board_stem}_failed.ses")

        # If only one attempt, use the legacy maxPasses value (preserves
        # exact backward-compatible behaviour). Otherwise cycle through
        # passSchedule. Always run single-threaded when scoring multiple
        # attempts so the optimiser doesn't introduce clearance violations
        # that would distort the comparison.
        for idx in range(attempts):
            if attempts == 1:
                attempt_passes = passes
                single_thread = False
            else:
                attempt_passes = pass_schedule[idx % len(pass_schedule)]
                single_thread = True

            cmd = _build_freerouting_cmd(
                jar_path,
                dsn_path,
                ses_path,
                attempt_passes,
                use_docker,
                single_thread=single_thread,
            )
            logger.info(
                f"Freerouting attempt {idx + 1}/{attempts} "
                f"(mp={attempt_passes}, mode={mode_label})"
            )

            attempt_start = time.time()
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=board_dir,
                )
                attempt_elapsed = round(time.time() - attempt_start, 1)
            except subprocess.TimeoutExpired:
                return {
                    "success": False,
                    "message": f"Freerouting timed out after {timeout}s",
                    "errorDetails": "Increase timeout or reduce board complexity",
                    "attempts_completed": idx,
                }
            except (OSError, subprocess.SubprocessError) as e:
                # OSError: java/docker binary missing or unexecutable.
                # SubprocessError: other subprocess.run failures aside from
                # TimeoutExpired (which is handled above).
                logger.exception(f"Freerouting subprocess failed: {e}")
                return {
                    "success": False,
                    "message": "Failed to run Freerouting",
                    "errorDetails": str(e),
                    "attempts_completed": idx,
                }

            if proc.returncode != 0:
                # Don't abort the whole best-of-N just because one attempt
                # exits nonzero — record it and move on.
                attempt_results.append(
                    {
                        "attempt": idx + 1,
                        "max_passes": attempt_passes,
                        "elapsed_seconds": attempt_elapsed,
                        "ok": False,
                        "exit_code": proc.returncode,
                        "stderr": (proc.stderr or "")[:200],
                    }
                )
                if attempts == 1:
                    return {
                        "success": False,
                        "message": f"Freerouting exited with code {proc.returncode}",
                        "errorDetails": proc.stderr or proc.stdout,
                        "elapsed_seconds": attempt_elapsed,
                        "mode": mode_label,
                    }
                continue

            if not os.path.isfile(ses_path):
                attempt_results.append(
                    {
                        "attempt": idx + 1,
                        "max_passes": attempt_passes,
                        "elapsed_seconds": attempt_elapsed,
                        "ok": False,
                        "error": "no SES produced",
                    }
                )
                if attempts == 1:
                    return {
                        "success": False,
                        "message": "Freerouting did not produce SES output",
                        "errorDetails": (f"Expected at: {ses_path}. Stdout: {proc.stdout[:500]}"),
                        "elapsed_seconds": attempt_elapsed,
                    }
                continue

            # A clean exit code is NOT proof of success: Freerouting 2.2.4
            # can throw mid-run (the B4 NPE), log ``ERROR Error during routing
            # passes``, exit 0, and still write an echo SES. Scan the output
            # stream for the fatal signatures.
            routing_error = _detect_routing_failure(
                (proc.stdout or "") + "\n" + (proc.stderr or "")
            )

            # Score this attempt
            with open(ses_path, "r", encoding="utf-8", errors="replace") as fh:
                ses_text = fh.read()
            score_info = _score_ses(ses_text, target_nets)
            score = score_info["score"]
            attempt_rec: Dict[str, Any] = {
                "attempt": idx + 1,
                "max_passes": attempt_passes,
                "elapsed_seconds": attempt_elapsed,
                "ok": routing_error is None,
                **score_info,
            }
            if routing_error:
                attempt_rec["routing_error"] = routing_error
            attempt_results.append(attempt_rec)
            logger.info(
                f"  attempt {idx + 1}: score={score} "
                f"({score_info['nets']} nets, {score_info['segments']} segs, "
                f"{score_info['vias']} vias)"
                + (f" FAILED: {routing_error}" if routing_error else "")
            )

            if routing_error:
                # Keep the best failed SES aside as a fallback, but never let
                # it compete with a clean attempt for best-of-N.
                if score > failed_best_score:
                    failed_best_score = score
                    failed_best_idx = idx
                    failed_best_stdout = proc.stdout or ""
                    failed_best_error = routing_error
                    shutil.copy2(ses_path, failed_ses_path)
                continue

            if score > best_score:
                best_score = score
                best_attempt_idx = idx
                best_proc_stdout = proc.stdout or ""
                # Snapshot the SES that produced this score so later
                # attempts (which overwrite ses_path) don't clobber it.
                shutil.copy2(ses_path, best_ses_path)

        elapsed = round(time.time() - total_start, 1)

        hint = (
            "Freerouting 2.2.4 can crash (NullPointerException in "
            "ShapeSearchTree.merge_entries_in_front, 'to_trace_entries' is null) "
            "on boards that carry pre-routed traces. Delete the existing traces "
            "on the nets you want routed and re-run autoroute from a clean "
            "(unrouted) state."
        )

        # --- Case A: a clean attempt won -> normal success path -------------
        if best_attempt_idx != -1:
            # Restore the winning SES as the canonical output file
            if attempts > 1:
                shutil.copy2(best_ses_path, ses_path)
            ses_size = os.path.getsize(ses_path)
            logger.info(
                f"Best SES: attempt {best_attempt_idx + 1}, score={best_score}, "
                f"{ses_size} bytes (total {elapsed}s)"
            )

            # Step 3+4: Import the winning SES (replace semantics) into the
            # freshly-loaded route board and save it. fire_signature=False:
            # the same-file case reloads the parent (B5) below, and the
            # external-file case must not stamp the open board's signature.
            logger.info(f"Importing SES from {ses_path}")
            applied = self._apply_ses(ses_path, board_path, board=route_board, fire_signature=False)
            if not applied["ok"]:
                err = dict(applied["error"])
                err["elapsed_seconds"] = elapsed
                err["attempts"] = attempt_results
                return err

            with open(ses_path, "r", encoding="utf-8", errors="replace") as fh:
                routed_nets = _ses_routed_nets(fh.read())
            response: Dict[str, Any] = {
                "success": True,
                "message": f"Autoroute completed in {elapsed}s",
                "mode": mode_label,
                "dsn_path": dsn_path,
                "ses_path": ses_path,
                "elapsed_seconds": elapsed,
                "board_stats": self._board_track_stats(route_board),
                "nets_routed": len(routed_nets),
                "replaced_existing_tracks": applied["removed_tracks"],
                "freerouting_stdout": best_proc_stdout[:1000],
            }
            if strip_info["wiring_removed"] or strip_info["planes_removed"]:
                response["dsn_prerouting_stripped"] = strip_info
            # B5: reload the open project board (same-file) or note that the
            # open board was untouched (external file); add routed_board_path.
            response.update(self._finalize_board(board_path, external))
            if attempts > 1:
                response["attempts"] = attempt_results
                response["best_attempt"] = best_attempt_idx + 1
                response["best_score"] = best_score
                response["best_ses_path"] = best_ses_path
            return response

        # --- Case B: only failed attempts produced a SES --------------------
        # Every attempt that ran logged a fatal error (the B4 NPE: exit 0 +
        # echo SES). Decide from what the SES actually contains, not a hopeful
        # default.
        if failed_best_idx != -1:
            shutil.copy2(failed_ses_path, ses_path)
            with open(ses_path, "r", encoding="utf-8", errors="replace") as fh:
                ses_nets = _ses_routed_nets(fh.read())
            newly_routed = sorted(ses_nets - pre_routed_nets)

            if not newly_routed:
                # Total failure: the SES is just an echo of the pre-existing
                # routing (nothing new was routed). Do NOT import — leave the
                # board exactly as it was — and fail honestly with the hint.
                logger.error(f"Autoroute failed: 0 new nets routed ({failed_best_error})")
                return {
                    "success": False,
                    "message": "Freerouting failed: 0 nets routed",
                    "errorDetails": failed_best_error,
                    "hint": hint,
                    "mode": mode_label,
                    "dsn_path": dsn_path,
                    "ses_path": ses_path,
                    "elapsed_seconds": elapsed,
                    "freerouting_error": failed_best_error,
                    "freerouting_stdout": failed_best_stdout[:1000],
                    "pre_routed_nets": sorted(pre_routed_nets),
                    "attempts": attempt_results,
                }

            # Partial: some new nets got routed before/around the crash. Import
            # with replace semantics, but flag the run as incomplete.
            logger.warning(
                f"Autoroute partial: {len(newly_routed)} new net(s) routed "
                f"despite a fatal error ({failed_best_error})"
            )
            applied = self._apply_ses(ses_path, board_path, board=route_board, fire_signature=False)
            if not applied["ok"]:
                err = dict(applied["error"])
                err["elapsed_seconds"] = elapsed
                err["attempts"] = attempt_results
                err["freerouting_error"] = failed_best_error
                return err
            partial: Dict[str, Any] = {
                "success": True,
                "routing_incomplete": True,
                "message": (
                    f"Autoroute completed with errors in {elapsed}s: "
                    f"{len(newly_routed)} new net(s) routed, but Freerouting "
                    "reported a fatal error — routing is partial"
                ),
                "warnings": [
                    f"Freerouting reported a fatal error: {failed_best_error}",
                    hint,
                ],
                "mode": mode_label,
                "dsn_path": dsn_path,
                "ses_path": ses_path,
                "elapsed_seconds": elapsed,
                "board_stats": self._board_track_stats(route_board),
                "nets_routed": len(ses_nets),
                "newly_routed_nets": newly_routed,
                "replaced_existing_tracks": applied["removed_tracks"],
                "freerouting_error": failed_best_error,
                "freerouting_stdout": failed_best_stdout[:1000],
                "attempts": attempt_results,
            }
            if strip_info["wiring_removed"] or strip_info["planes_removed"]:
                partial["dsn_prerouting_stripped"] = strip_info
            partial.update(self._finalize_board(board_path, external))
            return partial

        # --- Case C: no attempt produced a SES at all -----------------------
        return {
            "success": False,
            "message": "All Freerouting attempts failed",
            "errorDetails": "No attempt produced a usable SES file",
            "elapsed_seconds": elapsed,
            "attempts": attempt_results,
        }

    def export_dsn(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Export a board to Specctra DSN format only.

        B5: when ``boardPath`` names a file OTHER than the currently-open
        board, that file is loaded FRESH and exported — the DSN reflects the
        file named, not the in-memory board. A nonexistent ``boardPath`` is a
        hard FILE_NOT_FOUND. When ``boardPath`` is omitted (or is the open
        board's own file) the in-memory board is exported as before.
        """
        try:
            import pcbnew
        except ImportError:
            return {
                "success": False,
                "message": "pcbnew not available",
                "errorDetails": "KiCAD Python API is required",
            }

        if not self.board:
            return {
                "success": False,
                "message": "No board is loaded",
                "errorDetails": "Load or create a board first",
            }

        requested = params.get("boardPath")
        loaded_path = self.board.GetFileName()
        board_path = requested or loaded_path
        output_path = params.get("outputPath")

        if not output_path:
            if board_path:
                output_path = os.path.splitext(board_path)[0] + ".dsn"
            else:
                return {
                    "success": False,
                    "message": "No output path",
                    "errorDetails": ("Provide outputPath or have a board open"),
                }

        # B5: export the file named by boardPath, not the in-memory board, when
        # they differ. Export is read-only, so no flush/reload is needed.
        src_board = self.board
        if requested and (
            not loaded_path or os.path.abspath(requested) != os.path.abspath(loaded_path)
        ):
            if not os.path.isfile(requested):
                return {
                    "success": False,
                    "errorCode": "FILE_NOT_FOUND",
                    "message": "Board file not found",
                    "errorDetails": f"No such file: {requested}",
                }
            src_board = self._safe_fresh_load(requested, pcbnew)
            if src_board is None:
                return {
                    "success": False,
                    "message": "Failed to load board",
                    "errorDetails": f"pcbnew.LoadBoard could not open {requested}",
                }

        try:
            result = pcbnew.ExportSpecctraDSN(src_board, output_path)
            if result is not True and result != 0:
                return {
                    "success": False,
                    "message": "DSN export failed",
                    "errorDetails": (f"ExportSpecctraDSN returned: {result}"),
                }
        except Exception as e:
            logger.exception(f"ExportSpecctraDSN crashed: {e}")
            return {
                "success": False,
                "message": "DSN export failed",
                "errorDetails": str(e),
            }

        file_size = os.path.getsize(output_path) if os.path.isfile(output_path) else 0
        return {
            "success": True,
            "message": f"Exported DSN to {output_path}",
            "path": output_path,
            "size_bytes": file_size,
        }

    def import_ses(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Import a Specctra SES file into a board (with replace semantics).

        Existing tracks/vias on the nets the SES re-routes are cleared before
        the import so routing is *replaced*, not stacked — the same fix as
        autoroute (E2E finding B4): ``ImportSpecctraSES`` alone duplicated
        pre-routed traces.

        B5 fresh-load: the SES is applied to the file named by ``boardPath``
        (or, when omitted, the open board's own file), never a different
        in-memory board. Same-file imports reload the open project board after
        saving; an external ``boardPath`` leaves the open board untouched.
        """
        try:
            import pcbnew  # noqa: F401  (import guarded for the caller's env)
        except ImportError:
            return {
                "success": False,
                "message": "pcbnew not available",
                "errorDetails": "KiCAD Python API is required",
            }

        if not self.board:
            return {
                "success": False,
                "message": "No board is loaded",
                "errorDetails": "Load or create a board first",
            }

        ses_path = params.get("sesPath")
        if not ses_path:
            return {
                "success": False,
                "message": "Missing sesPath parameter",
                "errorDetails": ("Provide the path to the .ses file"),
            }

        if not os.path.isfile(ses_path):
            return {
                "success": False,
                "message": "SES file not found",
                "errorDetails": f"File not found: {ses_path}",
            }

        prep = self._prepare_target_board(params, pcbnew)
        if not prep["ok"]:
            return prep["error"]
        target_board = prep["board"]
        board_path = prep["board_path"]
        external = prep["external"]

        applied = self._apply_ses(ses_path, board_path, board=target_board, fire_signature=False)
        if not applied["ok"]:
            return applied["error"]

        response = {
            "success": True,
            "message": f"Imported SES from {ses_path}",
            "board_stats": self._board_track_stats(target_board),
            "replaced_existing_tracks": applied["removed_tracks"],
        }
        response.update(self._finalize_board(board_path, external))
        return response

    def check_freerouting(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Check if Freerouting and Java/Docker are available.

        When something's missing the response carries a structured
        ``install`` section with the exact commands the user needs to
        run — the TS adapter prints them as a copy-pasteable block.
        Previously the response just said ``jar_found: false`` and left
        the user to discover the install URL on their own.

        Versioned filenames (``freerouting-2.2.4.jar``) in the same
        directory as the requested path are auto-discovered so callers
        don't have to rename the GitHub release download.  The
        ``freerouting.jar_path`` field reports the actual file that
        would be invoked; ``freerouting.requested_path`` reports the
        original lookup target when they differ.
        """
        requested_jar = params.get("freeroutingJar", DEFAULT_FREEROUTING_JAR)
        resolved_jar = _resolve_freerouting_jar(requested_jar)
        jar_path = resolved_jar or requested_jar

        # Check local Java
        java_exe = _find_java()
        java_version = None
        java_21_ok = False
        if java_exe:
            try:
                proc = subprocess.run(
                    [java_exe, "-version"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                java_version = (proc.stderr or proc.stdout).strip().split("\n")[0]
                java_21_ok = _java_version_ok(java_exe)
            except (subprocess.TimeoutExpired, OSError):
                # best-effort: probe failure leaves java_version=None /
                # java_21_ok=False, which the caller surfaces in the
                # diagnostic payload.
                pass

        # Check Docker/Podman
        docker_exe = _find_docker()
        has_docker = _docker_available()

        jar_exists = os.path.isfile(jar_path)
        ready = jar_exists and (java_21_ok or has_docker)

        mode = "none"
        if java_21_ok:
            mode = "direct"
        elif has_docker:
            mode = "docker"

        install_steps: List[Dict[str, Any]] = []
        if not jar_exists:
            target_dir = os.path.dirname(jar_path) or "."
            install_steps.append(
                {
                    "missing": "freerouting.jar",
                    "target_path": jar_path,
                    "summary": (
                        "Download the Freerouting JAR from the GitHub release "
                        "page and save it as the path above.  Any release ≥ "
                        "1.9 works; the latest version is recommended."
                    ),
                    "download_page": "https://github.com/freerouting/freerouting/releases/latest",
                    "release_index": "https://github.com/freerouting/freerouting/releases",
                    "shell_unix": [
                        f"mkdir -p {target_dir!s}",
                        "# Pick the freerouting-*-linux-x64.jar (or *.jar without "
                        "platform suffix) from the latest release:",
                        "#   https://github.com/freerouting/freerouting/releases/latest",
                        f"# curl -L -o {jar_path!s} \\",
                        "#   <copy the JAR asset URL from the release page>",
                    ],
                    "shell_windows": [
                        f"mkdir {target_dir!s}",
                        "# Download the JAR from "
                        "https://github.com/freerouting/freerouting/releases/latest",
                        f"# and save it to {jar_path!s}",
                    ],
                    "override_with_env": "FREEROUTING_JAR=/path/to/freerouting.jar",
                }
            )
        if not java_21_ok and not has_docker:
            install_steps.append(
                {
                    "missing": "java>=21 or docker/podman",
                    "summary": (
                        "Freerouting needs either Java 21+ on PATH OR a "
                        "running Docker/Podman daemon (the MCP will pull "
                        f"{DOCKER_IMAGE} and run the JAR inside it).  "
                        "Either path works; Java is simpler."
                    ),
                    "java_install": (
                        "Linux: ``sudo apt install openjdk-21-jre`` (Debian/"
                        "Ubuntu) or ``sudo pacman -S jre-openjdk`` (Arch).  "
                        "macOS: ``brew install openjdk@21`` then "
                        "``sudo ln -sfn $(brew --prefix)/opt/openjdk@21/"
                        "libexec/openjdk.jdk /Library/Java/JavaVirtualMachines"
                        "/openjdk-21.jdk``.  "
                        "Windows: install from https://adoptium.net/temurin/releases/?version=21"
                    ),
                    "docker_alt": (
                        "Or start Docker Desktop / install podman; the MCP "
                        f"will use the ``{DOCKER_IMAGE}`` image automatically."
                    ),
                }
            )

        response: Dict[str, Any] = {
            "success": True,
            "message": "Freerouting dependency check",
            "java": {
                "found": java_exe is not None,
                "path": java_exe,
                "version": java_version,
                "java_21_ok": java_21_ok,
            },
            "docker": {
                "available": has_docker,
                "path": docker_exe,
                "image": DOCKER_IMAGE,
            },
            "freerouting": {
                "jar_found": jar_exists,
                "jar_path": jar_path,
                # When the auto-discover landed on a versioned filename,
                # surface the original lookup target so the user can see
                # what was matched and where the file actually lives.
                "requested_path": (
                    requested_jar if resolved_jar and resolved_jar != requested_jar else None
                ),
            },
            "execution_mode": mode,
            "ready": ready,
        }
        if install_steps:
            response["install"] = {
                "needed": True,
                "steps": install_steps,
                "after_install": (
                    "Re-run check_freerouting to verify, then call "
                    "autoroute(...) to use the Freerouting CLI."
                ),
            }
        return response
