"""IPCBackend: kipy connection lifecycle + board-API factory.

Split out of the former monolithic kicad_api/ipc_backend.py.
"""

import logging
import os
import platform
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from kicad_api.base import APINotAvailableError, BoardAPI, ConnectionError, KiCADBackend

from ._helpers import (
    get_open_documents_compat,
    has_open_pcb_document,
    normalize_board_path,
    open_pcb_document_paths,
)

logger = logging.getLogger("kicad_interface")

from utils.responses import failed

from ._board_core import IPCBoardAPI


class IPCBackend(KiCADBackend):
    """
    KiCAD IPC API backend for real-time UI synchronization.

    Communicates with KiCAD via Protocol Buffers over UNIX sockets.
    Requires KiCAD 9.0+ to be running with IPC enabled.

    Changes made through this backend appear immediately in the KiCAD UI
    without requiring manual reload.
    """

    def __init__(self) -> None:
        self._kicad: Any = None  # kipy.KiCad once connected (kipy is imported lazily)
        self._connected = False
        self._version: Optional[str] = None
        self._on_change_callbacks: List[Callable] = []
        # Cached IPCBoardAPI — one instance per connection.  Board-API state
        # (open transaction handle, cached Board wrapper) must survive across
        # command dispatches; handing out a fresh instance per get_board()
        # call silently dropped the open commit handle, so the next mutation
        # opened a second KiCad commit and was refused with 'client already
        # has a commit in progress'.
        self._board_api: Any = None

    def connect(
        self,
        socket_path: Optional[str] = None,
        prefer_board_path: Optional[str] = None,
    ) -> bool:
        """
        Connect to running KiCAD instance via IPC.

        Args:
            socket_path: Optional socket path. If not provided, will try common
                        locations. Use format: ipc:///tmp/kicad/api.sock
            prefer_board_path: When several KiCad instances are reachable (a
                        second instance opened on ``api-<pid>.sock``), prefer the
                        one whose open ``.kicad_pcb`` matches this path.  Without
                        it the loop keeps the legacy "first instance with any
                        board open" behaviour.

        Socket selection precedence:
          1. an explicit ``socket_path`` argument (used verbatim);
          2. the ``KICAD_API_SOCKET`` env var — AUTHORITATIVE: it is tried on
             its own and, if unreachable, raises rather than silently falling
             back to a default-socket instance (the documented override
             contract — a default ``api.sock`` instance used to win over it);
          3. auto-detection across the known socket dirs, globbing
             ``api-*.sock`` per-instance sockets and, when ``prefer_board_path``
             is given, choosing the instance whose open board matches it.

        Returns:
            True if connection successful

        Raises:
            ConnectionError: If connection fails
        """
        try:
            # Import here to allow module to load even without kicad-python
            from kipy import KiCad

            logger.info("Connecting to KiCAD via IPC...")

            # Try to connect with provided path or auto-detect
            socket_paths_to_try: List[Optional[str]] = []
            env_socket = (os.environ.get("KICAD_API_SOCKET") or "").strip()
            authoritative_socket: Optional[str] = None
            if socket_path:
                socket_paths_to_try.append(socket_path)
            elif env_socket:
                # KICAD_API_SOCKET is an explicit operator override — honour it
                # BEFORE the default candidate list (the previous code only let
                # kipy read it in the final None fall-through, so a running
                # default-socket instance always won and the override was a
                # no-op).  Try it alone; a failure surfaces a clear error naming
                # the socket instead of silently attaching elsewhere.
                authoritative_socket = env_socket
                socket_paths_to_try.append(env_socket)
            else:
                # Common socket locations (Unix-like systems only)
                # Windows uses named pipes, handled by auto-detect
                socket_dirs: List[str] = []
                if platform.system() != "Windows":
                    socket_dirs.append("/tmp/kicad")  # Linux default
                    # XDG runtime directory (requires getuid, Unix only)
                    if hasattr(os, "getuid"):
                        socket_dirs.append(f"/run/user/{os.getuid()}/kicad")
                    # Flatpak sandbox cache dir — KiCAD installed via Flathub
                    # puts the socket under ~/.var/app/org.kicad.KiCad/cache/...
                    # because the sandbox can't write to /tmp/kicad.  Same trick
                    # works for other XDG_CACHE_HOME values too.
                    socket_dirs.append(
                        os.path.expanduser("~/.var/app/org.kicad.KiCad/cache/tmp/kicad")
                    )
                    # Generic XDG_CACHE_HOME location (Linux convention)
                    xdg_cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
                    socket_dirs.append(f"{xdg_cache}/kicad")

                # macOS: KiCAD.app cache directory (sandboxed Mac installs put
                # the socket here, similar to Flatpak on Linux).
                if platform.system() == "Darwin":
                    socket_dirs.append(os.path.expanduser("~/Library/Caches/kicad"))

                # Each dir can hold the primary instance's api.sock AND
                # PID-suffixed api-<pid>.sock sockets for additional KiCad
                # instances (e.g. a standalone pcbnew opened next to the
                # project manager).  Probe all of them — the selection loop
                # below prefers whichever instance has the requested board (or,
                # failing that, any board) open.
                import glob as _glob

                for d in socket_dirs:
                    socket_paths_to_try.append(f"ipc://{os.path.join(d, 'api.sock')}")
                    for extra in sorted(_glob.glob(os.path.join(d, "api-*.sock"))):
                        socket_paths_to_try.append(f"ipc://{extra}")

                # Final fall-through: ask kipy to auto-detect (uses its own
                # default discovery).  KICAD_API_SOCKET is handled above, so it
                # never reaches here.
                socket_paths_to_try.append(None)

            # Selection, most-specific first:
            #   1. an instance whose open board == prefer_board_path (exact fix
            #      for two-instances-open: pick the one on the project's board);
            #   2. else the first instance with ANY board open (serves board ops
            #      immediately);
            #   3. else the first instance that answers ping at all.
            # With a single KiCad running this degenerates to the previous
            # first-connectable behaviour.
            prefer_norm = normalize_board_path(prefer_board_path)
            last_error = None
            board_fallback: Optional[tuple] = None
            any_fallback: Optional[tuple] = None
            chosen: Optional[tuple] = None
            for path in socket_paths_to_try:
                try:
                    if path:
                        logger.debug(f"Trying socket path: {path}")
                        candidate = KiCad(socket_path=path)
                    else:
                        if any_fallback is not None or board_fallback is not None:
                            continue  # already have a live connection; skip kipy auto-detect
                        logger.debug("Trying auto-detection")
                        candidate = KiCad()
                    # Verify connection with ping (ping returns None on success)
                    candidate.ping()
                except Exception as e:
                    last_error = e
                    logger.debug(f"Failed to connect via {path}: {e}")
                    continue
                try:
                    board_paths = open_pcb_document_paths(candidate)
                except Exception:
                    board_paths = []
                if prefer_norm is not None and any(
                    normalize_board_path(bp) == prefer_norm for bp in board_paths
                ):
                    chosen = (candidate, path)
                    break
                if board_paths and board_fallback is None:
                    board_fallback = (candidate, path)
                if any_fallback is None:
                    any_fallback = (candidate, path)

            if chosen is None:
                chosen = board_fallback or any_fallback
            if chosen is None:
                # None of the paths worked.  Name the socket when the operator
                # pointed us at one explicitly (env override / arg) so the
                # failure is actionable instead of a generic "couldn't connect".
                if authoritative_socket is not None:
                    raise ConnectionError(
                        f"KICAD_API_SOCKET={authoritative_socket} is set but that "
                        f"KiCAD IPC socket is unreachable: {last_error}. Verify a "
                        "KiCAD instance is serving that socket (and has the IPC "
                        "API server enabled), or unset KICAD_API_SOCKET to "
                        "auto-detect."
                    )
                raise ConnectionError(f"Could not connect to KiCAD IPC: {last_error}")
            self._kicad, used_path = chosen
            logger.info(f"Connected via socket: {used_path or 'auto-detected'}")

            self._version = self._get_kicad_version()
            logger.info(f"Connected to KiCAD {self._version} via IPC")
            self._connected = True
            return True

        except ImportError as e:
            logger.error("kicad-python library not found")
            raise APINotAvailableError(
                "IPC backend requires kicad-python. " "Install with: pip install kicad-python"
            ) from e
        except Exception as e:
            logger.error(f"Failed to connect via IPC: {e}")
            logger.info(
                "Ensure KiCAD is running with IPC enabled: "
                "Preferences > Plugins > Enable IPC API Server"
            )
            raise ConnectionError(f"IPC connection failed: {e}") from e

    def _get_kicad_version(self) -> str:
        """Get KiCAD version string.

        Tries multiple call patterns because the kipy public API has
        changed across releases:

          - kipy ≥ 10.x   : ``KiCad.get_version() -> KicadVersion`` with
                            ``.full_version`` attribute (e.g. "10.0.3").
          - kipy 9.x      : ``check_version()`` + ``get_api_version()``.

        ``check_version()`` raises ``FutureVersionError`` when the
        connected KiCAD is newer than the kipy library — that's expected
        (it just means kipy hasn't released a matching version yet), not
        an error condition we should surface as "unknown".  We still
        prefer ``get_version()`` so the user sees the *real* KiCAD
        version they're talking to.
        """
        # Preferred: modern get_version() returns a structured object.
        try:
            version_obj = self._kicad.get_version()
            full = getattr(version_obj, "full_version", None)
            if full:
                return str(full)
        except Exception as e:
            logger.debug(f"kipy get_version() unavailable: {e}")

        # Older API surface.
        try:
            if self._kicad.check_version():
                return self._kicad.get_api_version()
        except Exception as e:
            logger.debug(f"kipy check_version()/get_api_version() failed: {e}")

        return "unknown"

    def disconnect(self) -> None:
        """Disconnect from KiCAD."""
        if self._kicad:
            self._kicad = None
            self._connected = False
            self._board_api = None  # stale handle; new connection gets a fresh one
            logger.info("Disconnected from KiCAD IPC")

    def reselect_preferring_board(self, prefer_board_path: Optional[str] = None) -> bool:
        """Reconnect so the client talks to the KiCad instance that actually
        has the wanted board open.

        The first KiCad process grabs the well-known ``api.sock``; any second
        instance (e.g. a standalone ``pcbnew <board>`` spawned next to a
        running project manager) serves its own ``api-<pid>.sock``. A client
        already connected to the wrong instance never re-runs the connect-time
        socket selection, so it keeps reading/mutating the wrong board.
        ``connect()`` picks the instance whose open board matches
        ``prefer_board_path`` — this gives it a fresh chance.

        With ``prefer_board_path``:
          - No-op (True) when the current connection is already on that board.
          - Otherwise reconnect and re-scan; True only if the reconnected
            instance is on the requested board (so the caller's board-identity
            check passes).
        Without it (legacy call): No-op (True) when the current connection has
        ANY board open; otherwise reconnect and return True if any board is
        then reachable.  Returns False when the target instance can't be
        reached; the client may then be disconnected (the next ``connect()``
        heals it).
        """
        kicad = self._kicad
        prefer_norm = normalize_board_path(prefer_board_path)
        if kicad is not None and self._connected:
            if prefer_norm is None:
                try:
                    if has_open_pcb_document(kicad):
                        return True
                except Exception as e:
                    logger.debug(f"reselect: board-document check failed: {e}")
            else:
                try:
                    board_paths = open_pcb_document_paths(kicad)
                except Exception as e:
                    logger.debug(f"reselect: board-path check failed: {e}")
                    board_paths = []
                if any(normalize_board_path(bp) == prefer_norm for bp in board_paths):
                    return True
                # Current instance is on a different board → reconnect + rescan.
        self.disconnect()
        try:
            if prefer_board_path is None:
                self.connect()
            else:
                self.connect(prefer_board_path=prefer_board_path)
        except Exception as e:
            logger.debug(f"reselect: reconnect failed: {e}")
            return False
        try:
            if self._kicad is None:
                return False
            if prefer_norm is None:
                return bool(has_open_pcb_document(self._kicad))
            board_paths = open_pcb_document_paths(self._kicad)
            return any(normalize_board_path(bp) == prefer_norm for bp in board_paths)
        except Exception as e:
            logger.debug(f"reselect: post-reconnect check failed: {e}")
            return False

    def is_connected(self) -> bool:
        """Check if connected to KiCAD."""
        if not self._connected or not self._kicad:
            return False
        try:
            # ping() returns None on success, raises on failure
            self._kicad.ping()
            return True
        except Exception:
            self._connected = False
            return False

    def get_version(self) -> str:
        """Get KiCAD version."""
        return self._version or "unknown"

    def register_change_callback(self, callback: Callable) -> None:
        """Register a callback to be called when changes are made."""
        self._on_change_callbacks.append(callback)

    def _notify_change(self, change_type: str, details: Dict[str, Any]) -> None:
        """Notify registered callbacks of a change."""
        for callback in self._on_change_callbacks:
            try:
                callback(change_type, details)
            except Exception as e:
                logger.warning(f"Change callback error: {e}")

    # Project Operations
    def create_project(self, path: Path, name: str) -> Dict[str, Any]:
        """
        Create a new KiCAD project.

        Note: The IPC API doesn't directly create projects.
        Projects must be created through the UI or file system.
        """
        if not self.is_connected():
            raise ConnectionError("Not connected to KiCAD")

        # IPC API doesn't have project creation - use file-based approach
        logger.warning("Project creation via IPC not fully supported - using hybrid approach")

        # For now, we'll return info about what needs to happen
        return {
            "success": False,
            "message": "Direct project creation not supported via IPC",
            "suggestion": "Open KiCAD and create a new project, or use SWIG backend",
        }

    def open_project(self, path: Path) -> Dict[str, Any]:
        """Open existing project via IPC."""
        if not self.is_connected():
            raise ConnectionError("Not connected to KiCAD")

        try:
            # Check for open documents (kipy 10 requires a doc_type arg).
            documents = get_open_documents_compat(self._kicad)

            path_str = str(path)
            for doc in documents:
                if path_str in str(doc):
                    return {
                        "success": True,
                        "message": f"Project already open: {path}",
                        "path": str(path),
                    }

            return {
                "success": False,
                "message": "Project not currently open in KiCAD",
                "suggestion": "Open the project in KiCAD first, then connect via IPC",
            }

        except Exception as e:
            logger.error(f"Failed to check project: {e}")
            return failed("Failed to check project", e)

    def save_project(self, path: Optional[Path] = None) -> Dict[str, Any]:
        """Save current project via IPC."""
        if not self.is_connected():
            raise ConnectionError("Not connected to KiCAD")

        try:
            board = self._kicad.get_board()
            if path:
                board.save_as(str(path))
            else:
                board.save()

            self._notify_change("save", {"path": str(path) if path else "current"})

            return {"success": True, "message": "Project saved successfully"}
        except Exception as e:
            logger.error(f"Failed to save project: {e}")
            return failed("Failed to save project", e)

    def close_project(self) -> None:
        """Close current project (not supported via IPC)."""
        logger.warning("Closing projects via IPC is not supported")

    # Board Operations
    def get_board(self) -> BoardAPI:
        """Get board API for real-time manipulation."""
        if not self.is_connected():
            raise ConnectionError("Not connected to KiCAD")

        # Reuse the per-connection instance (see __init__) so transaction
        # state survives across command dispatches; recreate only when the
        # underlying kipy client object was swapped by a reconnect.
        if self._board_api is None or self._board_api._kicad is not self._kicad:
            self._board_api = IPCBoardAPI(self._kicad, self._notify_change)
        return self._board_api

    def invalidate_board_cache(self) -> None:
        """Drop the cached board wrapper so the next read re-fetches the CURRENT
        document.

        Called after a board-identity conflict / reselect heal: the cached
        IPCBoardAPI holds a kipy Board wrapper for the connection's lifetime, so
        without this a same-instance board switch would keep serving the stale
        document to the gate and to reads (finding 2).  The IPCBoardAPI instance
        itself is kept (its open-transaction state must survive) — only its
        cached Board wrapper is invalidated."""
        if self._board_api is not None:
            try:
                self._board_api.invalidate_board()
            except Exception as e:
                logger.debug(f"invalidate_board_cache: {e}")

    # KiCad-level operations (not specific to one document)
    def run_action(self, action: str) -> Dict[str, Any]:
        """
        Invoke a KiCad TOOL_ACTION by name (escape hatch into the editor).

        kipy upstream marks this as unstable — action names are not guaranteed
        across releases and side effects vary. Surface it anyway because some
        operations (close-loop ratsnest refresh, view-fit, plugin triggers,
        cleanup actions) have no other API.

        Returns the raw RUN_ACTION_STATUS enum value alongside a string label
        so callers don't have to import kipy's proto enum to interpret it.
        """
        if not self.is_connected():
            raise ConnectionError("Not connected to KiCAD")
        try:
            response = self._kicad.run_action(action)
            # kipy's run_action returns the full RunActionResponse proto
            # (not just the status enum value, despite the docstring).
            # Extract .status as int and best-effort resolve the enum name.
            status_int = int(getattr(response, "status", 0))
            status_name: Optional[str] = None
            try:
                from kipy.proto.common.commands.editor_commands_pb2 import RunActionStatus

                status_name = RunActionStatus.Name(status_int)
            except Exception:
                status_name = None
            # Success = the action ran (RAS_OK = 1).  Anything else is a
            # client-visible failure (RAS_INVALID action name, RAS_FRAME_NOT_OPEN).
            ok = status_int == 1
            self._notify_change("action_invoked", {"action": action, "status": status_int})
            return {
                "success": ok,
                "action": action,
                "status": status_int,
                "statusName": status_name,
            }
        except Exception as e:
            logger.error(f"Failed to run action {action!r}: {e}")
            return {"success": False, "action": action, "errorDetails": str(e)}
