"""
KiCAD Process Management Utilities

Detects if KiCAD is running and provides auto-launch functionality.
"""

import ctypes
import logging
import os
import platform
import shutil
import signal
import stat
import subprocess
import time
from ctypes import wintypes
from pathlib import Path
from typing import List, Optional, Set

logger = logging.getLogger(__name__)


class KiCADProcessManager:
    """Manages KiCAD process detection and launching"""

    # PIDs of KiCad GUI processes THIS server launched (class-level so the set
    # survives across handler calls for the server's lifetime).  ``launch()``
    # records here; ``terminate_launched`` only ever signals a PID from this
    # set — never an externally started KiCad — and only when it is *currently*
    # a running GUI binary, so a reused PID can't be killed by mistake (D6).
    _launched_pids: "Set[int]" = set()

    @staticmethod
    def _windows_list_processes() -> List[dict]:
        """List running processes on Windows using Toolhelp API."""
        processes: List[dict] = []
        try:
            TH32CS_SNAPPROCESS = 0x00000002
            try:
                ulong_ptr = wintypes.ULONG_PTR  # type: ignore[attr-defined]
            except AttributeError:
                ulong_ptr = (
                    ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
                )

            class PROCESSENTRY32W(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ulong_ptr),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * wintypes.MAX_PATH),
                ]

            CreateToolhelp32Snapshot = ctypes.windll.kernel32.CreateToolhelp32Snapshot
            Process32FirstW = ctypes.windll.kernel32.Process32FirstW
            Process32NextW = ctypes.windll.kernel32.Process32NextW
            CloseHandle = ctypes.windll.kernel32.CloseHandle

            snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if snapshot == wintypes.HANDLE(-1).value:
                return processes

            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)

            if Process32FirstW(snapshot, ctypes.byref(entry)):
                while True:
                    processes.append(
                        {
                            "pid": str(entry.th32ProcessID),
                            "name": entry.szExeFile,
                            "command": entry.szExeFile,
                        }
                    )
                    if not Process32NextW(snapshot, ctypes.byref(entry)):
                        break

            CloseHandle(snapshot)
        except Exception as e:
            logger.error(f"Error listing Windows processes: {e}")

        return processes

    # Binary names that indicate a KiCAD GUI process (hosts the IPC API server).
    _LINUX_KICAD_BINARIES = frozenset({"kicad", "pcbnew", "eeschema"})

    @staticmethod
    def _linux_proc_exe_basename(pid: str) -> Optional[str]:
        """Resolve /proc/<pid>/exe to a normalized lowercase basename, or None.

        Linux appends ``" (deleted)"`` to the symlink target when the binary
        was replaced on disk while the process is still running — typical
        after ``pacman -Syu kicad`` mid-session.  Strip that suffix before
        comparing so we don't lose the process on every package upgrade.
        """
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return None
        name = os.path.basename(exe).lower()
        if name.endswith(" (deleted)"):
            name = name[: -len(" (deleted)")]
        return name

    @staticmethod
    def _linux_pids_for(names: frozenset) -> List[str]:
        """Return PIDs whose /proc/<pid>/exe basename is in ``names``.

        Resolving the exe symlink avoids false positives from command-line
        substring matches (e.g. a shell whose cwd contains "/kicad").
        """
        pids: List[str] = []
        try:
            entries = os.listdir("/proc")
        except OSError:
            return pids
        for entry in entries:
            if not entry.isdigit():
                continue
            name = KiCADProcessManager._linux_proc_exe_basename(entry)
            if name is not None and name in names:
                pids.append(entry)
        return pids

    @staticmethod
    def _linux_kicad_pids() -> List[str]:
        """PIDs running any known KiCAD GUI binary (kicad / pcbnew / eeschema)."""
        return KiCADProcessManager._linux_pids_for(KiCADProcessManager._LINUX_KICAD_BINARIES)

    # macOS GUI executable basenames.  The single source of truth for macOS
    # detection is ``_darwin_kicad_processes`` below; every macOS branch
    # (is_running / is_pcb_editor_running / get_process_info) routes through it
    # so they can never disagree (the P5 contradiction: get_backend_info said
    # "running" while manage_kicad_ui said "not running").
    _DARWIN_GUI_BINARIES = frozenset({"kicad", "pcbnew", "eeschema"})

    @staticmethod
    def _darwin_kicad_processes(pcbnew_only: bool = False) -> List[dict]:
        """Strict macOS KiCad GUI process list (pid / name / command).

        Matches on the executable BASENAME (``kicad`` / ``pcbnew`` /
        ``eeschema``) resolved from the process's argv[0], NOT on a substring
        of the whole command line.  The old ``pgrep -f "KiCad|pcbnew"`` matched
        anything whose arguments merely *mentioned* those words and produced
        constant false positives:

          * ``kicad-cli`` subprocesses the server itself spawns for ERC / DRC /
            export (argv[0] basename ``kicad-cli`` — excluded here);
          * the MCP server process, or any unrelated process whose command line
            contains "KiCad"/"pcbnew" (a shell sitting in a KiCad repo, or an
            agent whose system prompt discusses KiCad) — these have basenames
            like ``zsh`` / ``python`` / ``claude`` and are excluded.

        Real GUI frames run from a macOS app bundle
        (``…/Contents/MacOS/<bin>``); a bare absolute path to the exact GUI
        binary name is also accepted so Homebrew / source installs still count.
        """
        wanted = (
            KiCADProcessManager._PCBNEW_ONLY
            if pcbnew_only
            else KiCADProcessManager._DARWIN_GUI_BINARIES
        )
        procs: List[dict] = []
        try:
            result = subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True, text=True)
        except Exception as e:
            logger.error(f"Error listing macOS processes: {e}")
            return procs
        self_pid = str(os.getpid())
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            pid, args = parts[0], parts[1]
            if pid == self_pid or "kicad_interface.py" in args:
                continue
            exe = args.split(None, 1)[0]
            base = os.path.basename(exe).lower()
            if base not in wanted:
                continue
            # A real GUI binary is either inside an app bundle or an absolute
            # path — never a relative token accidentally basenamed to "kicad".
            if "/Contents/MacOS/" not in exe and not exe.startswith("/"):
                continue
            procs.append({"pid": pid, "name": base, "command": args})
        return procs

    # Directories a KiCad IPC endpoint (``api.sock``) can live in.  The sibling
    # ``api.lock`` is deliberately NOT consulted: KiCad removes ``api.sock`` on
    # exit but ``api.lock`` can linger after a crash, so keying on the lock is
    # exactly what makes stale-state detection lie.
    @staticmethod
    def _ipc_socket_dirs() -> List[str]:
        dirs: List[str] = ["/tmp/kicad"]
        if hasattr(os, "getuid"):
            dirs.append(f"/run/user/{os.getuid()}/kicad")
        if platform.system() == "Darwin":
            dirs.append(os.path.expanduser("~/Library/Caches/kicad"))
        dirs.append(os.path.expanduser("~/.var/app/org.kicad.KiCad/cache/tmp/kicad"))
        xdg_cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
        dirs.append(f"{xdg_cache}/kicad")
        return dirs

    @staticmethod
    def is_ipc_socket_live() -> bool:
        """True when a KiCad IPC endpoint socket (``api.sock`` or a PID-suffixed
        ``api-<pid>.sock``) is present.

        Windows uses a named pipe (no socket file on disk), so this is Unix-only
        and returns False there — process detection covers Windows.  Used only
        as a *corroborating* signal for get_backend_info's guidance branch; it
        is never trusted on its own to declare KiCad "running" (a crash can, in
        principle, leave a stale socket behind).
        """
        if platform.system() == "Windows":
            return False
        import glob as _glob

        for d in KiCADProcessManager._ipc_socket_dirs():
            candidates = [os.path.join(d, "api.sock")]
            try:
                candidates.extend(_glob.glob(os.path.join(d, "api-*.sock")))
            except Exception:
                pass
            for sock in candidates:
                try:
                    if stat.S_ISSOCK(os.stat(sock).st_mode):
                        return True
                except OSError:
                    continue
        return False

    @staticmethod
    def is_running() -> bool:
        """
        Check if KiCAD is currently running

        Returns:
            True if KiCAD process found, False otherwise
        """
        system = platform.system()

        try:
            if system == "Linux":
                return bool(KiCADProcessManager._linux_kicad_pids())

            elif system == "Darwin":  # macOS
                # Strict, argv[0]-basename detection — never the loose
                # ``pgrep -f`` substring match that flagged kicad-cli / the MCP
                # server / unrelated processes as a running KiCad (P5).
                return bool(KiCADProcessManager._darwin_kicad_processes())

            elif system == "Windows":
                processes = KiCADProcessManager._windows_list_processes()
                for proc in processes:
                    name = (proc.get("name") or "").lower()
                    if name in ("pcbnew.exe", "kicad.exe"):
                        return True
                return False

            else:
                logger.warning(f"Process detection not implemented for {system}")
                return False

        except Exception as e:
            logger.error(f"Error checking if KiCAD is running: {e}")
            return False

    _PCBNEW_ONLY = frozenset({"pcbnew"})

    @staticmethod
    def is_pcb_editor_running() -> bool:
        """Whether the PCB editor (pcbnew) frame is currently a running process.

        The project manager (`kicad`) hosts the IPC API server on its own —
        ``is_running()`` returns True for a bare project manager, but no
        ``.kicad_pcb`` document is open in that state.  Board-level IPC ops
        attempted then either fail with cryptic kipy errors or silently route
        to a closed document, so callers that mutate the board gate on this
        and ask the user to open the PCB editor.
        """
        system = platform.system()
        try:
            if system == "Linux":
                # Reuses _linux_pids_for so the " (deleted)" fix-up in
                # _linux_proc_exe_basename applies to this gate too.
                return bool(KiCADProcessManager._linux_pids_for(KiCADProcessManager._PCBNEW_ONLY))

            elif system == "Darwin":  # macOS
                # Same strict basename detection, narrowed to the pcbnew frame.
                return bool(KiCADProcessManager._darwin_kicad_processes(pcbnew_only=True))

            elif system == "Windows":
                for proc in KiCADProcessManager._windows_list_processes():
                    name = (proc.get("name") or "").lower()
                    if name == "pcbnew.exe":
                        return True
                return False

            else:
                logger.warning(f"PCB editor detection not implemented for {system}")
                return False

        except Exception as e:
            logger.error(f"Error checking if PCB editor is running: {e}")
            return False

    @staticmethod
    def get_executable_path() -> Optional[Path]:
        """
        Get path to KiCAD executable

        Returns:
            Path to pcbnew/kicad executable, or None if not found
        """
        system = platform.system()

        # Prefer the `kicad` project manager — it hosts both the PCB and
        # schematic editors as panes, matching how users normally work.
        # Fall back to `pcbnew` only if the project manager isn't installed.
        for cmd in ["kicad", "pcbnew"]:
            exe_path = shutil.which(cmd)
            if exe_path:
                logger.info(f"Found KiCAD executable: {exe_path}")
                return Path(exe_path)

        # Platform-specific default paths
        if system == "Linux":
            candidates = [
                Path("/usr/bin/kicad"),
                Path("/usr/local/bin/kicad"),
                Path("/usr/bin/pcbnew"),
            ]
        elif system == "Darwin":  # macOS
            # The GUI project manager lives directly in the outer bundle's
            # MacOS dir; the standalone pcbnew binary is NESTED inside a
            # sub-bundle (Contents/Applications/pcbnew.app/...), the same
            # bundle gap that bit kicad-cli discovery.
            candidates = [
                Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad"),
                Path(os.path.expanduser("~/Applications/KiCad/KiCad.app/Contents/MacOS/kicad")),
                Path(
                    "/Applications/KiCad/KiCad.app/Contents/Applications/"
                    "pcbnew.app/Contents/MacOS/pcbnew"
                ),
            ]
        elif system == "Windows":
            candidates = [
                Path("C:/Program Files/KiCad/10.0/bin/pcbnew.exe"),
                Path("C:/Program Files/KiCad/9.0/bin/pcbnew.exe"),
                Path("C:/Program Files/KiCad/8.0/bin/pcbnew.exe"),
                Path("C:/Program Files (x86)/KiCad/9.0/bin/pcbnew.exe"),
            ]
        else:
            candidates = []

        for path in candidates:
            if path.exists():
                logger.info(f"Found KiCAD executable: {path}")
                return path

        logger.warning("Could not find KiCAD executable")
        return None

    # KiCad's Flatpak application id (system-wide and user-local installs
    # share it).  Used to build the ``flatpak run --command=pcbnew`` form
    # when no native pcbnew binary is on disk.
    _FLATPAK_APP_ID = "org.kicad.KiCad"

    @staticmethod
    def _flatpak_kicad_installed() -> bool:
        """True when KiCad is present as a Flatpak (system or user scope)."""
        app = KiCADProcessManager._FLATPAK_APP_ID
        candidates = [
            Path("/var/lib/flatpak/app") / app,
            Path.home() / ".local/share/flatpak/app" / app,
        ]
        return any(p.exists() for p in candidates)

    @staticmethod
    def get_pcb_editor_path() -> Optional[Path]:
        """Locate the standalone PCB editor (``pcbnew``) executable.

        Opening a ``.kicad_pcb`` requires the *standalone PCB editor*, NOT the
        ``kicad`` project manager: on KiCad 10.x ``kicad <board.kicad_pcb>``
        only raises the project manager with no board document open over IPC
        (so the PCB-editor gate never lifts), whereas ``pcbnew <board.kicad_pcb>``
        opens the editor frame whose IPC server exposes the board.

        Resolution order:
          1. a ``pcbnew`` binary next to the resolved ``kicad``/``pcbnew``
             executable (covers non-PATH installs found by
             ``get_executable_path``);
          2. ``pcbnew`` on PATH;
          3. platform default install locations.

        Returns None when only a Flatpak install is present (callers use
        ``get_pcb_editor_command`` for the ``flatpak run`` form) or nothing is
        found.
        """
        system = platform.system()
        exe_name = "pcbnew.exe" if system == "Windows" else "pcbnew"

        # 1. Sibling of the resolved project-manager/editor executable — so a
        #    non-standard install picked up by get_executable_path yields the
        #    matching pcbnew from the same bin dir.
        base = KiCADProcessManager.get_executable_path()
        if base is not None:
            sibling = base.parent / exe_name
            if sibling.exists():
                return sibling

        # 2. PATH.
        which = shutil.which("pcbnew")
        if which:
            return Path(which)

        # 3. Platform defaults.
        if system == "Linux":
            candidates = [Path("/usr/bin/pcbnew"), Path("/usr/local/bin/pcbnew")]
        elif system == "Darwin":  # macOS
            # pcbnew is a NESTED sub-bundle on macOS — NOT directly in the
            # outer bundle's MacOS dir (that path never existed and was the
            # cause of "PCB editor executable not found", P6b).
            candidates = [
                Path(
                    "/Applications/KiCad/KiCad.app/Contents/Applications/"
                    "pcbnew.app/Contents/MacOS/pcbnew"
                ),
                Path(
                    os.path.expanduser(
                        "~/Applications/KiCad/KiCad.app/Contents/Applications/"
                        "pcbnew.app/Contents/MacOS/pcbnew"
                    )
                ),
            ]
        elif system == "Windows":
            candidates = [
                Path("C:/Program Files/KiCad/10.0/bin/pcbnew.exe"),
                Path("C:/Program Files/KiCad/9.0/bin/pcbnew.exe"),
                Path("C:/Program Files/KiCad/8.0/bin/pcbnew.exe"),
                Path("C:/Program Files (x86)/KiCad/9.0/bin/pcbnew.exe"),
            ]
        else:
            candidates = []
        for path in candidates:
            if path.exists():
                logger.info(f"Found KiCAD PCB editor: {path}")
                return path
        return None

    @staticmethod
    def get_pcb_editor_command(board_path: Optional[Path] = None) -> Optional[List[str]]:
        """Argv to launch the standalone PCB editor, optionally opening ``board_path``.

        Prefers a native ``pcbnew`` binary (``get_pcb_editor_path``); falls back
        to ``flatpak run --command=pcbnew org.kicad.KiCad`` when only a Flatpak
        install is present.  Returns None when no PCB editor can be located.
        """
        exe = KiCADProcessManager.get_pcb_editor_path()
        if exe is not None:
            cmd = [str(exe)]
        elif KiCADProcessManager._flatpak_kicad_installed():
            cmd = ["flatpak", "run", "--command=pcbnew", KiCADProcessManager._FLATPAK_APP_ID]
        else:
            return None
        if board_path is not None:
            cmd.append(str(board_path))
        return cmd

    @staticmethod
    def ensure_ipc_api_enabled() -> bool:
        """Flip ``api.enable_server`` to true in every kicad_common.json found.

        KiCad reads this setting once at startup, and a *running* KiCad
        rewrites the file on exit — so the only safe window to set it is
        right before we launch the process ourselves.  Doing it here means
        the IPC backend can attach on first launch instead of telling the
        user to click Preferences → Plugins → Enable IPC API Server.

        Returns True when at least one config file now has the server
        enabled (already-enabled counts), False when none could be updated.
        """
        import json

        system = platform.system()
        if system == "Darwin":
            config_root = Path.home() / "Library" / "Preferences" / "kicad"
        elif system == "Windows":
            config_root = Path.home() / "AppData" / "Roaming" / "kicad"
        else:
            config_root = Path.home() / ".config" / "kicad"

        if not config_root.is_dir():
            logger.debug(f"No KiCad config dir at {config_root}; skipping IPC enable")
            return False

        any_enabled = False
        for version_dir in sorted(config_root.iterdir()):
            common = version_dir / "kicad_common.json"
            if not common.is_file():
                continue
            try:
                data = json.loads(common.read_text(encoding="utf-8"))
                api = data.setdefault("api", {})
                if api.get("enable_server") is True:
                    any_enabled = True
                    continue
                api["enable_server"] = True
                common.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                logger.info(f"Enabled KiCad IPC API server in {common}")
                any_enabled = True
            except (OSError, ValueError) as e:
                logger.warning(f"Could not enable IPC API in {common}: {e}")
        return any_enabled

    @staticmethod
    def launch(project_path: Optional[Path] = None, wait_for_start: bool = True) -> bool:
        """
        Launch KiCAD PCB Editor

        Args:
            project_path: Optional path to .kicad_pcb file to open
            wait_for_start: Wait for process to start before returning

        Returns:
            True if launch successful, False otherwise
        """
        try:
            # Check if already running
            if KiCADProcessManager.is_running():
                logger.info("KiCAD is already running")
                return True

            # KiCad reads api.enable_server at startup; flip it now (KiCad
            # isn't running, so the file won't be rewritten under us) so the
            # IPC backend can attach without manual Preferences clicks.
            KiCADProcessManager.ensure_ipc_api_enabled()

            # Build command.  A .kicad_pcb must be opened with the standalone
            # PCB editor (pcbnew): `kicad <board.kicad_pcb>` only raises the
            # project manager with no board document open over IPC (verified on
            # KiCad 10.x), which then keeps the PCB-editor gate closed.  A
            # project file / no path goes to the project manager as before.
            cmd: Optional[List[str]] = None
            if project_path is not None and Path(project_path).suffix == ".kicad_pcb":
                cmd = KiCADProcessManager.get_pcb_editor_command(Path(project_path))

            if cmd is None:
                exe_path = KiCADProcessManager.get_executable_path()
                if not exe_path:
                    logger.error("Cannot launch KiCAD: executable not found")
                    return False
                cmd = [str(exe_path)]
                if project_path:
                    cmd.append(str(project_path))

            logger.info(f"Launching KiCAD: {' '.join(cmd)}")

            # Launch process in background
            system = platform.system()
            if system == "Windows":
                # Windows: Use CREATE_NEW_PROCESS_GROUP to detach
                proc = subprocess.Popen(
                    cmd,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                # Unix: Use nohup or start in background
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            # Track the PID we launched so manage_kicad_ui(action=quit) can
            # terminate exactly this GUI later (D6).  The Popen child is the GUI
            # frame itself (standalone pcbnew for a board, or the project
            # manager otherwise), so its PID appears in get_process_info().
            KiCADProcessManager._record_launched_pid(proc.pid)

            # Wait for process to start
            if wait_for_start:
                logger.info("Waiting for KiCAD to start...")
                for i in range(10):  # Wait up to 5 seconds
                    time.sleep(0.5)
                    if KiCADProcessManager.is_running():
                        logger.info("✓ KiCAD started successfully")
                        return True

                logger.warning("KiCAD process not detected after launch")
                # Return True anyway, it might be starting
                return True

            return True

        except Exception as e:
            logger.error(f"Error launching KiCAD: {e}")
            return False

    @staticmethod
    def get_process_info() -> List[dict]:
        """
        Get information about running KiCAD processes

        Returns:
            List of process info dicts with pid, name, and command
        """
        system = platform.system()
        processes = []

        try:
            if system == "Linux":
                for pid in KiCADProcessManager._linux_kicad_pids():
                    try:
                        exe = os.readlink(f"/proc/{pid}/exe")
                        with open(f"/proc/{pid}/cmdline", "rb") as f:
                            cmdline = (
                                f.read()
                                .replace(b"\x00", b" ")
                                .decode("utf-8", errors="replace")
                                .strip()
                            )
                    except OSError:
                        continue
                    processes.append(
                        {"pid": pid, "name": os.path.basename(exe), "command": cmdline or exe}
                    )

            elif system == "Darwin":
                # Single source of truth shared with is_running() — argv[0]
                # basename match, so the process list and the boolean running
                # check can never contradict each other (P5).
                processes = KiCADProcessManager._darwin_kicad_processes()

            elif system == "Windows":
                for proc in KiCADProcessManager._windows_list_processes():
                    name = (proc.get("name") or "").lower()
                    if "pcbnew" in name or "kicad" in name:
                        processes.append(proc)

        except Exception as e:
            logger.error(f"Error getting process info: {e}")

        return processes

    # ------------------------------------------------------------------
    # Termination — quit the GUI the server launched (D6).
    # ------------------------------------------------------------------
    @classmethod
    def _record_launched_pid(cls, pid: Optional[int]) -> None:
        """Remember a PID launch() spawned so quit can target it later.

        Only real positive ints are recorded — a mocked Popen in tests yields a
        non-int ``.pid``, which is ignored rather than polluting the tracked set
        (and, later, ``terminate_launched`` only ever signals a tracked PID that
        is currently a live GUI, so a stray value could never be signalled).
        """
        if isinstance(pid, int) and pid > 0:
            cls._launched_pids.add(pid)

    @classmethod
    def _running_gui_pids(cls) -> "Set[int]":
        """PIDs of currently-running KiCad GUI processes (as ints).

        Reuses ``get_process_info`` — the same strict, resolver-based detection
        the rest of this module uses — so termination never falls back to a
        ``pgrep``-by-name match (the machine gotcha) and can only ever see real
        GUI frames.
        """
        out: "Set[int]" = set()
        for proc in cls.get_process_info():
            try:
                out.add(int(proc.get("pid")))
            except (TypeError, ValueError):
                continue
        return out

    @staticmethod
    def _signal_pid(pid: int, sig: int) -> bool:
        """Send ``sig`` to ``pid``. Returns False if the process was already gone.

        On Windows there are no POSIX signals: SIGTERM maps to a graceful
        ``taskkill`` and SIGKILL to a forceful ``taskkill /F`` (both with ``/T``
        to take the process tree).
        """
        if platform.system() == "Windows":
            force = ["/F"] if sig == getattr(signal, "SIGKILL", 9) else []
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", *force],
                    capture_output=True,
                    check=False,
                )
                return True
            except Exception as e:  # pragma: no cover - Windows-only path
                logger.debug(f"taskkill for pid {pid} failed: {e}")
                return False
        try:
            os.kill(pid, sig)
            return True
        except ProcessLookupError:
            return False
        except PermissionError as e:  # pragma: no cover - unusual
            logger.warning(f"Not permitted to signal pid {pid}: {e}")
            return False
        except OSError as e:  # pragma: no cover - defensive
            logger.debug(f"os.kill({pid}, {sig}) failed: {e}")
            return False

    @classmethod
    def terminate_launched(cls, timeout_s: float = 5.0) -> dict:
        """Terminate the KiCad GUI process(es) THIS server launched.

        Only signals a PID that is BOTH tracked (recorded by ``launch()``) AND
        currently a running KiCad GUI — so an externally started editor is left
        alone and a reused PID can't be killed by mistake.  Escalation:
        SIGTERM → bounded wait (``timeout_s``) → SIGKILL → short verify.

        Returns a truthful status dict:
          ``terminated``          PIDs we confirmed killed
          ``forced``              subset that needed SIGKILL
          ``survived``            tracked GUIs still alive after SIGKILL
          ``alreadyExited``       tracked PIDs no longer running (pruned)
          ``externalGuiPids``     running GUI PIDs we did NOT launch (untouched)
          ``launchedGuiRunning``  was any GUI we launched running to begin with
          ``externalGuiRunning``  is a GUI we did not launch still running
        """
        running = cls._running_gui_pids()
        tracked = set(cls._launched_pids)
        ours_running = sorted(tracked & running)
        already_exited = sorted(tracked - running)
        external_pids = sorted(running - tracked)

        terminated: List[int] = []
        forced: List[int] = []

        sigterm = getattr(signal, "SIGTERM", 15)
        sigkill = getattr(signal, "SIGKILL", 9)

        # SIGTERM everyone first, then wait (bounded) for a graceful exit.
        for pid in ours_running:
            cls._signal_pid(pid, sigterm)

        pending = set(ours_running)
        deadline = time.monotonic() + timeout_s
        while pending and time.monotonic() < deadline:
            time.sleep(0.1)
            still = cls._running_gui_pids()
            gone = {p for p in pending if p not in still}
            terminated.extend(gone)
            pending -= gone

        # SIGKILL any survivor, then a short verify pass.
        if pending:
            for pid in sorted(pending):
                cls._signal_pid(pid, sigkill)
            kill_deadline = time.monotonic() + min(timeout_s, 2.0)
            while pending and time.monotonic() < kill_deadline:
                time.sleep(0.1)
                still = cls._running_gui_pids()
                gone = {p for p in pending if p not in still}
                for p in gone:
                    terminated.append(p)
                    forced.append(p)
                pending -= gone

        survived = sorted(pending)

        # Prune what we confirmed gone (killed or already-exited) from tracking;
        # keep survivors so a retry can target them again.
        cls._launched_pids -= set(terminated)
        cls._launched_pids -= set(already_exited)

        return {
            "terminated": sorted(terminated),
            "forced": sorted(forced),
            "survived": survived,
            "alreadyExited": already_exited,
            "externalGuiPids": external_pids,
            "launchedGuiRunning": bool(ours_running),
            "externalGuiRunning": bool(external_pids),
        }


def check_and_launch_kicad(project_path: Optional[Path] = None, auto_launch: bool = True) -> dict:
    """
    Check if KiCAD is running and optionally launch it

    Args:
        project_path: Optional path to .kicad_pcb file to open
        auto_launch: If True, launch KiCAD if not running

    Returns:
        Dict with status information
    """
    manager = KiCADProcessManager()

    # ``is_running`` and ``get_process_info`` now share one strict detector, so
    # they can't disagree — but guard the invariant explicitly: only report
    # alreadyRunning when the process list is actually non-empty.  A truthy
    # ``running`` with an EMPTY process list is the exact P5/P6 contradiction
    # (get_backend_info claimed running while manage_kicad_ui showed none), and
    # in that state we must NOT skip the launch.  ``get_process_info`` is only
    # consulted once ``is_running`` is True so the auto_launch=False / not-
    # running path stays a single cheap probe (and never spawns a subprocess).
    if manager.is_running():
        processes = manager.get_process_info()
        if processes:
            # alreadyRunning is load-bearing: handlers.ui.handle_launch_kicad_ui
            # only forwards a file-open to the running instance when it sees
            # alreadyRunning=True and launched=False.
            return {
                "running": True,
                "launched": False,
                "alreadyRunning": True,
                "processes": processes,
                "message": "KiCAD is already running",
            }
        logger.info(
            "is_running() was True but the process list is empty; treating KiCAD "
            "as not running so the launch is not skipped."
        )

    if not auto_launch:
        return {
            "running": False,
            "launched": False,
            "alreadyRunning": False,
            "processes": [],
            "message": "KiCAD is not running (auto-launch disabled)",
        }

    # Try to launch
    logger.info("KiCAD not detected, attempting to launch...")
    success = manager.launch(project_path)

    return {
        "running": success,
        "launched": success,
        "alreadyRunning": False,
        "processes": manager.get_process_info() if success else [],
        "message": "KiCAD launched successfully" if success else "Failed to launch KiCAD",
        "project": str(project_path) if project_path else None,
    }
