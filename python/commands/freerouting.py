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

    def _save_and_record(self, board_path: str) -> None:
        """Save the board and notify the parent interface (if any).

        Uses ``getattr`` so test fixtures that bypass ``__init__`` via
        ``__new__`` don't AttributeError — they simply skip the callback.
        """
        self.board.Save(board_path)
        cb = getattr(self, "_signature_callback", None)
        if cb is not None:
            try:
                cb(board_path)
            except Exception:
                logger.debug("Signature callback raised; ignoring", exc_info=True)

    def _board_routed_nets(self) -> set:
        """Net names that currently have at least one track or via on the board.

        Used to tell "did the autoroute actually route anything new?" apart
        from "the SES is just an echo of the pre-existing routing" (the B4
        crash case).
        """
        nets: set = set()
        try:
            tracks = list(self.board.GetTracks())
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

    def _remove_tracks_on_nets(self, net_names: set) -> int:
        """Delete every track/via whose net is in ``net_names``; return count.

        This is the "rip" half of KiCad's native Specctra *replace* semantics:
        before importing a SES we clear the existing routing on exactly the
        nets the SES will re-add, so the import replaces rather than stacks
        (which duplicated pre-routed traces — E2E finding B4).

        Uses ``board.Delete`` (not ``Remove``) to match the rest of the code
        base: the KiCAD 10 SWIG bindings leak / corrupt the object table on
        ``Remove`` but free cleanly on ``Delete`` (see routing/_traces.py).
        """
        if not net_names:
            return 0
        removed = 0
        try:
            tracks = list(self.board.GetTracks())
        except Exception:
            return 0
        for t in tracks:
            try:
                name = t.GetNetname()
            except Exception:
                name = None
            if name in net_names:
                self.board.Delete(t)
                removed += 1
        return removed

    def _apply_ses(self, ses_path: str, board_path: Optional[str]) -> Dict[str, Any]:
        """Import a SES with replace semantics, then save the board.

        Removes existing tracks/vias on the nets the SES will re-route, runs
        ``ImportSpecctraSES``, and saves via ``_save_and_record`` (preserving
        the ``_on_swig_direct_save`` landed-write bookkeeping).

        Returns ``{"ok": True, "removed_tracks": n, "replaced_nets": [...]}``
        on success, or ``{"ok": False, "error": {...response...}}`` on an
        import failure.
        """
        import pcbnew

        try:
            with open(ses_path, "r", encoding="utf-8", errors="replace") as fh:
                ses_text = fh.read()
        except OSError:
            ses_text = ""
        replace_nets = _ses_routed_nets(ses_text)
        removed = self._remove_tracks_on_nets(replace_nets)

        try:
            result = pcbnew.ImportSpecctraSES(self.board, ses_path)
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

        if board_path:
            try:
                self._save_and_record(board_path)
            except (OSError, RuntimeError) as e:
                # Non-fatal: the SES is imported; user can save manually.
                logger.warning(f"Board save after SES import failed: {e}")

        return {
            "ok": True,
            "removed_tracks": removed,
            "replaced_nets": sorted(replace_nets),
        }

    def _board_track_stats(self) -> Dict[str, int]:
        """Return ``{"tracks": n, "vias": m}`` for the current board."""
        track_count = 0
        via_count = 0
        for t in self.board.GetTracks():
            if t.GetClass() == "PCB_VIA":
                via_count += 1
            else:
                track_count += 1
        return {"tracks": track_count, "vias": via_count}

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

        board_path = params.get("boardPath")
        if not board_path:
            board_path = self.board.GetFileName()

        if not board_path:
            return {
                "success": False,
                "message": "No board file path available",
                "errorDetails": ("Provide boardPath or open a project first"),
            }

        requested_jar = params.get("freeroutingJar", DEFAULT_FREEROUTING_JAR)
        # Resolve versioned filenames (e.g. ``freerouting-2.2.4.jar``) so the
        # user doesn't have to rename the GitHub release download.
        jar_path = _resolve_freerouting_jar(requested_jar) or requested_jar
        timeout = params.get("timeout", 300)
        passes = params.get("maxPasses", 20)

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

        # Net names that already carry routing — captured before we touch the
        # board so the failure path can tell "routed something new" from "the
        # SES is just an echo of the pre-existing traces" (the B4 crash).
        pre_routed_nets = self._board_routed_nets()

        # Validate Freerouting JAR
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

        # Set up file paths
        board_dir = os.path.dirname(board_path)
        board_stem = Path(board_path).stem
        dsn_path = os.path.join(board_dir, f"{board_stem}.dsn")
        ses_path = os.path.join(board_dir, f"{board_stem}.ses")
        best_ses_path = os.path.join(board_dir, f"{board_stem}_best.ses")

        # Step 1: Export DSN (once, regardless of attempt count)
        logger.info(f"Exporting DSN to {dsn_path}")
        try:
            result = pcbnew.ExportSpecctraDSN(self.board, dsn_path)
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

            # Step 3+4: Import the winning SES (replace semantics) and save.
            logger.info(f"Importing SES from {ses_path}")
            applied = self._apply_ses(ses_path, board_path)
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
                "board_stats": self._board_track_stats(),
                "nets_routed": len(routed_nets),
                "replaced_existing_tracks": applied["removed_tracks"],
                "freerouting_stdout": best_proc_stdout[:1000],
            }
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
            applied = self._apply_ses(ses_path, board_path)
            if not applied["ok"]:
                err = dict(applied["error"])
                err["elapsed_seconds"] = elapsed
                err["attempts"] = attempt_results
                err["freerouting_error"] = failed_best_error
                return err
            return {
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
                "board_stats": self._board_track_stats(),
                "nets_routed": len(ses_nets),
                "newly_routed_nets": newly_routed,
                "replaced_existing_tracks": applied["removed_tracks"],
                "freerouting_error": failed_best_error,
                "freerouting_stdout": failed_best_stdout[:1000],
                "attempts": attempt_results,
            }

        # --- Case C: no attempt produced a SES at all -----------------------
        return {
            "success": False,
            "message": "All Freerouting attempts failed",
            "errorDetails": "No attempt produced a usable SES file",
            "elapsed_seconds": elapsed,
            "attempts": attempt_results,
        }

    def export_dsn(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Export the board to Specctra DSN format only."""
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

        board_path = params.get("boardPath") or self.board.GetFileName()
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

        try:
            result = pcbnew.ExportSpecctraDSN(self.board, output_path)
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
        """Import a Specctra SES file into the board (with replace semantics).

        Existing tracks/vias on the nets the SES re-routes are cleared before
        the import so routing is *replaced*, not stacked — the same fix as
        autoroute (E2E finding B4): ``ImportSpecctraSES`` alone duplicated
        pre-routed traces.
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

        board_path = params.get("boardPath") or self.board.GetFileName()
        applied = self._apply_ses(ses_path, board_path)
        if not applied["ok"]:
            return applied["error"]

        return {
            "success": True,
            "message": f"Imported SES from {ses_path}",
            "board_stats": self._board_track_stats(),
            "replaced_existing_tracks": applied["removed_tracks"],
        }

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
