"""
IPC API Backend (KiCAD 9.0+)

Uses the official kicad-python library for inter-process communication
with a running KiCAD instance. This enables REAL-TIME UI synchronization.

Note: Requires KiCAD to be running with IPC server enabled:
    Preferences > Plugins > Enable IPC API Server

Key Benefits over SWIG:
- Changes appear instantly in KiCAD UI (no reload needed)
- Transaction support for undo/redo
- Stable API that won't break between versions
- Multi-language support
"""

import logging
import os
import platform
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from kicad_api.base import APINotAvailableError, BoardAPI, ConnectionError, KiCADBackend

logger = logging.getLogger(__name__)

# Unit conversion constant: KiCAD IPC uses nanometers internally
MM_TO_NM = 1_000_000
INCH_TO_NM = 25_400_000


def get_open_documents_compat(kicad: Any, doc_type: Any = None) -> List[Any]:
    """Call ``KiCad.get_open_documents`` across kipy 9 and 10.

    kipy 10's signature is ``get_open_documents(doc_type)`` — the arg is
    REQUIRED, so the older no-arg call raises ``TypeError`` and (when
    swallowed) made every "is a board open?" check report False even with
    the PCB editor open.  kipy 9 took no argument.

    * ``doc_type`` given → query just that type (kipy 10), falling back to
      the no-arg form on kipy 9.
    * ``doc_type`` None → aggregate across PCB / schematic / project so
      callers that want "any open document" still work.
    """
    DocumentType = _document_type_enum()

    def _query(dt: Any) -> List[Any]:
        try:
            return list(kicad.get_open_documents(dt) or [])
        except TypeError:
            # kipy 9: no-arg signature.
            try:
                return list(kicad.get_open_documents() or [])
            except Exception:
                return []
        except Exception as e:
            logger.debug(f"get_open_documents({dt}) failed: {e}")
            return []

    if doc_type is not None:
        return _query(doc_type)

    if DocumentType is not None:
        out: List[Any] = []
        for dt in (
            DocumentType.DOCTYPE_PCB,
            DocumentType.DOCTYPE_SCHEMATIC,
            DocumentType.DOCTYPE_PROJECT,
        ):
            out.extend(_query(dt))
        return out

    # No DocumentType enum importable — last resort: kipy 9 no-arg.
    try:
        return list(kicad.get_open_documents() or [])
    except Exception:
        return []


def _document_type_enum() -> Any:
    """Return kipy's ``DocumentType`` enum, or None if unavailable."""
    try:
        from kipy.proto.common.types import DocumentType

        return DocumentType
    except Exception:
        return None


def has_open_pcb_document(kicad: Any) -> bool:
    """True iff KiCAD has at least one ``.kicad_pcb`` document open over IPC."""
    DocumentType = _document_type_enum()
    doc_type = DocumentType.DOCTYPE_PCB if DocumentType is not None else None
    for doc in get_open_documents_compat(kicad, doc_type):
        # Real kipy docs expose ``board_filename`` (+ ``project.path``); some
        # call paths / older stubs expose a single ``path``.  Accept either.
        for attr in ("board_filename", "path"):
            value = getattr(doc, attr, "") or ""
            if str(value).endswith(".kicad_pcb"):
                return True
        dtype = getattr(doc, "type", None)
        # When we queried DOCTYPE_PCB explicitly, any returned doc is a PCB.
        if doc_type is not None and dtype == doc_type:
            return True
        type_name = getattr(dtype, "name", "") if dtype is not None else ""
        if type_name in {"DOCTYPE_PCB", "PCB"}:
            return True
    return False


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

    def connect(self, socket_path: Optional[str] = None) -> bool:
        """
        Connect to running KiCAD instance via IPC.

        Args:
            socket_path: Optional socket path. If not provided, will try common locations.
                        Use format: ipc:///tmp/kicad/api.sock

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
            socket_paths_to_try = []
            if socket_path:
                socket_paths_to_try.append(socket_path)
            else:
                # Common socket locations (Unix-like systems only)
                # Windows uses named pipes, handled by auto-detect
                if platform.system() != "Windows":
                    socket_paths_to_try.append("ipc:///tmp/kicad/api.sock")  # Linux default
                    # XDG runtime directory (requires getuid, Unix only)
                    if hasattr(os, "getuid"):
                        socket_paths_to_try.append(f"ipc:///run/user/{os.getuid()}/kicad/api.sock")
                    # Flatpak sandbox cache dir — KiCAD installed via Flathub
                    # puts the socket under ~/.var/app/org.kicad.KiCad/cache/...
                    # because the sandbox can't write to /tmp/kicad.  Same trick
                    # works for other XDG_CACHE_HOME values too.
                    flatpak_cache = os.path.expanduser(
                        "~/.var/app/org.kicad.KiCad/cache/tmp/kicad/api.sock"
                    )
                    socket_paths_to_try.append(f"ipc://{flatpak_cache}")
                    # Generic XDG_CACHE_HOME location (Linux convention)
                    xdg_cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
                    socket_paths_to_try.append(f"ipc://{xdg_cache}/kicad/api.sock")

                # macOS: KiCAD.app cache directory (sandboxed Mac installs put
                # the socket here, similar to Flatpak on Linux).
                if platform.system() == "Darwin":
                    socket_paths_to_try.append(
                        f"ipc://{os.path.expanduser('~/Library/Caches/kicad/api.sock')}"
                    )

                # Final fall-through: ask kipy to auto-detect (uses
                # KICAD_API_SOCKET env var, or its own default discovery).
                socket_paths_to_try.append(None)

            last_error = None
            for path in socket_paths_to_try:
                try:
                    if path:
                        logger.debug(f"Trying socket path: {path}")
                        self._kicad = KiCad(socket_path=path)
                    else:
                        logger.debug("Trying auto-detection")
                        self._kicad = KiCad()

                    # Verify connection with ping (ping returns None on success)
                    self._kicad.ping()
                    logger.info(f"Connected via socket: {path or 'auto-detected'}")
                    break
                except Exception as e:
                    last_error = e
                    logger.debug(f"Failed to connect via {path}: {e}")
                    continue
            else:
                # None of the paths worked
                raise ConnectionError(f"Could not connect to KiCAD IPC: {last_error}")

            # Get version info
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
            logger.info("Disconnected from KiCAD IPC")

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

            # Look for matching project
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
            return {"success": False, "message": "Failed to check project", "errorDetails": str(e)}

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
            return {"success": False, "message": "Failed to save project", "errorDetails": str(e)}

    def close_project(self) -> None:
        """Close current project (not supported via IPC)."""
        logger.warning("Closing projects via IPC is not supported")

    # Board Operations
    def get_board(self) -> BoardAPI:
        """Get board API for real-time manipulation."""
        if not self.is_connected():
            raise ConnectionError("Not connected to KiCAD")

        return IPCBoardAPI(self._kicad, self._notify_change)

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


class IPCBoardAPI(BoardAPI):
    """
    Board API implementation for IPC backend.

    All changes made through this API appear immediately in the KiCAD UI.
    Uses transactions for proper undo/redo support.
    """

    def __init__(self, kicad_instance: Any, notify_callback: Callable) -> None:
        self._kicad = kicad_instance
        self._board = None
        self._notify = notify_callback
        # Active transaction state. When _current_commit is set, every
        # _apply_create/update/remove call piggy-backs on it instead of
        # opening its own per-call commit, so the whole sequence lands
        # as one undo step in KiCad.
        self._current_commit: Optional[Any] = None
        self._current_commit_description: Optional[str] = None

    def _get_board(self) -> Any:
        """Get board instance, connecting if needed."""
        if self._board is None:
            try:
                self._board = self._kicad.get_board()
            except Exception as e:
                logger.error(f"Failed to get board: {e}")
                raise ConnectionError(f"No board open in KiCAD: {e}")
        return self._board

    #: Default label shown in KiCad's undo history when the caller didn't
    #: supply one.  Single source of truth — handlers pass through
    #: ``None`` rather than copy-substituting their own default.
    _DEFAULT_COMMIT_LABEL = "MCP Operation"

    def begin_transaction(self, description: Optional[str] = None) -> Dict[str, Any]:
        """Open a transaction. Subsequent mutating calls fold into one undo step.

        Refuses to nest — a second begin without an intervening commit /
        rollback would leak the original commit handle and orphan the
        first batch of changes.  Callers should commit or rollback the
        existing transaction first.

        ``description`` of ``None`` (or key omitted) gets the default
        label.  An explicit empty string is preserved — KiCad will show
        a blank undo entry, but that's the caller's choice.

        Note: only mutations that go through ``_apply_create / update /
        remove`` participate.  Property mutations like ``set_origin`` and
        ``set_title_block_info`` are sent as direct kipy commands and are
        NOT part of the undo step (kipy treats them as out-of-band).
        """
        if self._current_commit is not None:
            return {
                "success": False,
                "message": (
                    "A transaction is already open — commit or rollback it "
                    "before starting a new one."
                ),
            }
        label = description if description is not None else self._DEFAULT_COMMIT_LABEL
        try:
            board = self._get_board()
            self._current_commit = board.begin_commit()
            self._current_commit_description = label
            logger.debug(f"Started transaction: {label}")
            return {"success": True, "description": label}
        except Exception as e:
            logger.error(f"Failed to begin transaction: {e}")
            return {"success": False, "message": str(e)}

    def commit_transaction(self, description: Optional[str] = None) -> Dict[str, Any]:
        """Push the open transaction as one undo step. ``description`` of
        ``None`` keeps the label set at ``begin_transaction``; an explicit
        empty string overrides to blank."""
        if self._current_commit is None:
            return {
                "success": False,
                "message": "No open transaction to commit.",
            }
        # Three-state precedence: explicit override (incl. "") > begin label > default.
        if description is not None:
            msg = description
        elif self._current_commit_description is not None:
            msg = self._current_commit_description
        else:
            msg = self._DEFAULT_COMMIT_LABEL
        try:
            board = self._get_board()
            board.push_commit(self._current_commit, msg)
            self._current_commit = None
            self._current_commit_description = None
            logger.debug(f"Committed transaction: {msg}")
            return {"success": True, "description": msg}
        except Exception as e:
            logger.error(f"Failed to commit transaction: {e}")
            # Leave _current_commit set — caller may want to retry or
            # rollback explicitly rather than us silently clearing state.
            return {"success": False, "message": str(e)}

    def rollback_transaction(self) -> Dict[str, Any]:
        """Drop the open transaction — everything done since begin is undone."""
        if self._current_commit is None:
            return {
                "success": False,
                "message": "No open transaction to roll back.",
            }
        try:
            board = self._get_board()
            board.drop_commit(self._current_commit)
            self._current_commit = None
            self._current_commit_description = None
            logger.debug("Rolled back transaction")
            return {"success": True}
        except Exception as e:
            logger.error(f"Failed to rollback transaction: {e}")
            return {"success": False, "message": str(e)}

    def get_transaction_status(self) -> Dict[str, Any]:
        """Whether a transaction is currently open and its description."""
        return {
            "success": True,
            "open": self._current_commit is not None,
            "description": self._current_commit_description,
        }

    # ------------------------------------------------------------------
    # Mutation helpers — every mutator funnels through these so that an
    # open transaction (via begin_transaction) catches the change instead
    # of opening its own commit.
    # ------------------------------------------------------------------
    def _apply_create(self, board: Any, item: Any, description: str) -> str:
        """Create one item, respecting any open transaction.

        Returns the new item's KIID string. kipy's ``create_items``
        returns fresh wrappers with the server-assigned IDs; the input
        wrapper is *not* mutated, so we must read the id from the
        return value (not from the local ``item``).
        """
        if self._current_commit is not None:
            created = board.create_items(item)
        else:
            commit = board.begin_commit()
            created = board.create_items(item)
            board.push_commit(commit, description)
        # create_items returns a list (or None from older stubs). Take
        # the first entry's id; fall back to the input item if the
        # backend gave us nothing useful (defensive — real kipy always
        # returns the created wrapper list, but tests / stubs vary).
        if created:
            first = created[0]
            if hasattr(first, "id"):
                return str(first.id)
        return str(item.id) if hasattr(item, "id") else ""

    def _apply_update(self, board: Any, items: List[Any], description: str) -> None:
        """Update items, respecting any open transaction."""
        if self._current_commit is not None:
            board.update_items(items)
        else:
            commit = board.begin_commit()
            board.update_items(items)
            board.push_commit(commit, description)

    def _apply_remove(self, board: Any, items: List[Any], description: str) -> None:
        """Remove items, respecting any open transaction."""
        if self._current_commit is not None:
            board.remove_items(items)
        else:
            commit = board.begin_commit()
            board.remove_items(items)
            board.push_commit(commit, description)

    def save(self) -> bool:
        """Save the board immediately."""
        try:
            board = self._get_board()
            board.save()
            self._notify("save", {})
            return True
        except Exception as e:
            logger.error(f"Failed to save board: {e}")
            return False

    def revert(self) -> bool:
        """Discard KiCad's in-memory board and reload it from the .kicad_pcb
        on disk (the IPC equivalent of File → Revert).

        Used by ``reconcile_backends(swig_to_ipc)`` to pull SWIG-written disk
        content into the running KiCad instance — the direction we long
        (wrongly) documented as impossible.  kipy *does* expose this via
        ``Board.revert()`` → ``RevertDocument`` (kicad-python ≥ 0.7, KiCad
        ≥ 10.0.1).

        WARNING: this throws away any *unsaved* IPC changes in KiCad memory,
        so callers must only invoke it when the IPC side is known clean
        (``_ipc_writes_pending`` is False).  We deliberately do NOT fire the
        change callback here: ``_on_ipc_change`` would mark the IPC side dirty
        for any non-``save`` event, but a revert leaves KiCad memory == disk.
        The reconcile handler resets the gate flags explicitly instead.
        """
        try:
            board = self._get_board()
            board.revert()
            # Drop the cached Board handle so the next query re-fetches against
            # the freshly-reloaded document.
            self._board = None
            return True
        except Exception as e:
            logger.error(f"Failed to revert board: {e}")
            return False

    def set_size(self, width: float, height: float, unit: str = "mm") -> bool:
        """
        Set board size.

        Note: Board size in KiCAD is typically defined by the board outline,
        not a direct size property. This method may need to create/modify
        the board outline.
        """
        try:
            from kipy.board_types import BoardRectangle
            from kipy.geometry import Vector2
            from kipy.proto.board.board_types_pb2 import BoardLayer
            from kipy.util.units import from_mm

            board = self._get_board()

            # Convert to nm
            if unit == "mm":
                w = from_mm(width)
                h = from_mm(height)
            else:
                w = int(width * INCH_TO_NM)
                h = int(height * INCH_TO_NM)

            # Create board outline rectangle on Edge.Cuts layer
            rect = BoardRectangle()
            rect.start = Vector2.from_xy(0, 0)
            rect.end = Vector2.from_xy(w, h)
            rect.layer = BoardLayer.BL_Edge_Cuts
            rect.width = from_mm(0.1)  # Standard edge cut width

            self._apply_create(board, rect, f"Set board size to {width}x{height} {unit}")

            self._notify("board_size", {"width": width, "height": height, "unit": unit})

            return True

        except Exception as e:
            logger.error(f"Failed to set board size: {e}")
            return False

    def get_size(self) -> Dict[str, Any]:
        """Get current board size from bounding box."""
        try:
            board = self._get_board()

            # Get shapes on Edge.Cuts layer to determine board size
            shapes = board.get_shapes()

            if not shapes:
                return {"width": 0, "height": 0, "unit": "mm"}

            # Find bounding box of edge cuts
            from kipy.util.units import to_mm

            min_x = min_y = float("inf")
            max_x = max_y = float("-inf")

            for shape in shapes:
                # Check if on Edge.Cuts layer
                bbox = board.get_item_bounding_box(shape)
                if bbox:
                    left, top, right, bottom = self._get_box2_extents(bbox)
                    min_x = min(min_x, left)
                    min_y = min(min_y, top)
                    max_x = max(max_x, right)
                    max_y = max(max_y, bottom)

            if min_x == float("inf"):
                return {"width": 0, "height": 0, "unit": "mm"}

            return {"width": to_mm(max_x - min_x), "height": to_mm(max_y - min_y), "unit": "mm"}

        except Exception as e:
            logger.error(f"Failed to get board size: {e}")
            return {"width": 0, "height": 0, "unit": "mm", "error": str(e)}

    @staticmethod
    def _get_box2_extents(bbox: Any) -> tuple[float, float, float, float]:
        """Return left/top/right/bottom for kipy Box2 wrappers across versions."""
        if hasattr(bbox, "min") and hasattr(bbox, "max"):
            return bbox.min.x, bbox.min.y, bbox.max.x, bbox.max.y

        if hasattr(bbox, "pos") and hasattr(bbox, "size"):
            x1 = bbox.pos.x
            y1 = bbox.pos.y
            x2 = x1 + bbox.size.x
            y2 = y1 + bbox.size.y
            return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)

        raise AttributeError("Unsupported Box2 shape: expected min/max or pos/size")

    def add_layer(self, layer_name: str, layer_type: str) -> bool:
        """Add layer to the board (layers are typically predefined in KiCAD)."""
        logger.warning("Layer management via IPC is limited - layers are predefined")
        return False

    def get_enabled_layers(self) -> List[str]:
        """Get list of enabled layers."""
        try:
            board = self._get_board()
            layers = board.get_enabled_layers()
            return [str(layer) for layer in layers]
        except Exception as e:
            logger.error(f"Failed to get enabled layers: {e}")
            return []

    def list_components(self) -> List[Dict[str, Any]]:
        """List all components (footprints) on the board."""
        try:
            from kipy.util.units import to_mm

            board = self._get_board()
            footprints = board.get_footprints()

            components = []
            for fp in footprints:
                try:
                    pos = fp.position

                    # Try to get bounding box
                    bbox_data = None
                    try:
                        bbox = board.get_item_bounding_box(fp)
                        if bbox:
                            bbox_data = {
                                "min_x": to_mm(bbox.min.x),
                                "min_y": to_mm(bbox.min.y),
                                "max_x": to_mm(bbox.max.x),
                                "max_y": to_mm(bbox.max.y),
                                "width": to_mm(bbox.max.x - bbox.min.x),
                                "height": to_mm(bbox.max.y - bbox.min.y),
                                "unit": "mm",
                            }
                    except Exception:
                        pass  # Bounding box may not be available via IPC

                    # Fallback: compute bounding box from pad positions + sizes
                    if not bbox_data:
                        try:
                            pads = fp.pads if hasattr(fp, "pads") else []
                            pad_list = list(pads)
                            if pad_list:
                                min_x = float("inf")
                                min_y = float("inf")
                                max_x = float("-inf")
                                max_y = float("-inf")
                                for pad in pad_list:
                                    px = to_mm(pad.position.x) if pad.position else 0
                                    py = to_mm(pad.position.y) if pad.position else 0
                                    pw = (
                                        to_mm(pad.size.x) / 2
                                        if hasattr(pad, "size") and pad.size
                                        else 0.5
                                    )
                                    ph = (
                                        to_mm(pad.size.y) / 2
                                        if hasattr(pad, "size") and pad.size
                                        else 0.5
                                    )
                                    min_x = min(min_x, px - pw)
                                    min_y = min(min_y, py - ph)
                                    max_x = max(max_x, px + pw)
                                    max_y = max(max_y, py + ph)
                                margin = 0.25  # mm — small margin for component body beyond pads
                                bbox_data = {
                                    "min_x": min_x - margin,
                                    "min_y": min_y - margin,
                                    "max_x": max_x + margin,
                                    "max_y": max_y + margin,
                                    "width": (max_x - min_x) + 2 * margin,
                                    "height": (max_y - min_y) + 2 * margin,
                                    "unit": "mm",
                                }
                        except Exception as e:
                            logger.debug(f"Could not compute bbox from pads: {e}")

                    # kipy returns ``fp.layer`` as a ``BoardLayer`` enum.  On
                    # some kipy versions ``str(enum)`` is the enum *name*
                    # (``"BL_F_Cu"``) which we can strip to ``"F.Cu"``; on
                    # others it's the raw int value (``"3"``).  Prefer
                    # ``.name`` when present so the user sees a layer name
                    # instead of an opaque integer.
                    raw_layer = getattr(fp, "layer", None)
                    if raw_layer is None:
                        layer_str = "F.Cu"
                    else:
                        layer_name = getattr(raw_layer, "name", None) or str(raw_layer)
                        if layer_name.startswith("BL_"):
                            layer_name = layer_name[3:].replace("_", ".")
                        layer_str = layer_name

                    components.append(
                        {
                            "reference": (
                                fp.reference_field.text.value if fp.reference_field else ""
                            ),
                            "value": fp.value_field.text.value if fp.value_field else "",
                            "footprint": (
                                str(fp.definition.library_link)
                                if fp.definition and hasattr(fp.definition, "library_link")
                                else (
                                    str(fp.definition.id)
                                    if fp.definition and hasattr(fp.definition, "id")
                                    else ""
                                )
                            ),
                            "position": {
                                "x": to_mm(pos.x) if pos else 0,
                                "y": to_mm(pos.y) if pos else 0,
                                "unit": "mm",
                            },
                            "rotation": fp.orientation.degrees if fp.orientation else 0,
                            "layer": layer_str,
                            "id": str(fp.id) if hasattr(fp, "id") else "",
                            "boundingBox": bbox_data,
                        }
                    )
                except Exception as e:
                    logger.warning(f"Error processing footprint: {e}")
                    continue

            return components

        except Exception as e:
            logger.error(f"Failed to list components: {e}")
            return []

    def place_component(
        self,
        reference: str,
        footprint: str,
        x: float,
        y: float,
        rotation: float = 0,
        layer: str = "F.Cu",
        value: str = "",
    ) -> bool:
        """
        Place a component on the board.

        The component appears immediately in the KiCAD UI.

        This method uses a hybrid approach:
        1. Load the footprint definition from the library using pcbnew (SWIG)
        2. Place it on the board via IPC for real-time UI updates

        Args:
            reference: Component reference designator (e.g., "R1", "U1")
            footprint: Footprint path in format "Library:FootprintName" or just "FootprintName"
            x: X position in mm
            y: Y position in mm
            rotation: Rotation angle in degrees
            layer: Layer name ("F.Cu" for top, "B.Cu" for bottom)
            value: Component value (optional)
        """
        try:
            # First, try to load the footprint from library using pcbnew SWIG
            loaded_fp = self._load_footprint_from_library(footprint)

            if loaded_fp:
                # We have the footprint from the library - place it via SWIG
                # then sync to IPC for UI update
                return self._place_loaded_footprint(
                    loaded_fp, reference, x, y, rotation, layer, value
                )
            else:
                # Fallback: Create a basic placeholder footprint via IPC
                logger.warning(
                    f"Could not load footprint '{footprint}' from library, creating placeholder"
                )
                return self._place_placeholder_footprint(
                    reference, footprint, x, y, rotation, layer, value
                )

        except Exception as e:
            logger.error(f"Failed to place component: {e}")
            return False

    def _load_footprint_from_library(self, footprint_path: str) -> Any:
        """
        Load a footprint from the library using pcbnew SWIG API.

        Args:
            footprint_path: Either "Library:FootprintName" or just "FootprintName"

        Returns:
            pcbnew.FOOTPRINT object or None if not found
        """
        try:
            import pcbnew
            from commands.library import get_library_manager

            # ``pcbnew.GetGlobalFootprintLib()`` does NOT exist in KiCad 9/10
            # — the old code AttributeError'd here, so every IPC placement
            # silently failed.  Resolve the nickname to its ``.pretty``
            # directory via the library table (same path the working SWIG
            # place_component uses) and load by path. Cached manager so a
            # multi-component placement doesn't re-parse the lib-table per part.
            resolved = get_library_manager().find_footprint(footprint_path)
            if not resolved:
                logger.warning(f"Footprint '{footprint_path}' not found in any library")
                return None

            library_path, fp_name = resolved
            loaded_fp = pcbnew.FootprintLoad(library_path, fp_name)
            if loaded_fp:
                logger.info(f"Loaded footprint '{fp_name}' from '{library_path}'")
                return loaded_fp

            logger.warning(f"FootprintLoad returned None for {library_path}/{fp_name}")
            return None

        except ImportError:
            logger.warning("pcbnew not available - cannot load footprints from library")
            return None
        except Exception as e:
            logger.error(f"Error loading footprint from library: {e}")
            return None

    def _place_loaded_footprint(
        self,
        loaded_fp: Any,
        reference: str,
        x: float,
        y: float,
        rotation: float,
        layer: str,
        value: str,
    ) -> bool:
        """
        Place a loaded pcbnew footprint onto the board.

        Uses SWIG to add the footprint, then notifies for IPC sync.
        """
        try:
            import pcbnew

            # Get the board file path from IPC to load via pcbnew
            board = self._get_board()
            board_path = None

            # Try to get the board path from kipy.  Docs expose
            # ``board_filename`` (relative) + ``project.path`` (dir), not a
            # single ``path`` attribute; stitch them.
            try:
                DocumentType = _document_type_enum()
                dt = DocumentType.DOCTYPE_PCB if DocumentType is not None else None
                for doc in get_open_documents_compat(self._kicad, dt):
                    fname = getattr(doc, "board_filename", "") or ""
                    if not str(fname).endswith(".kicad_pcb"):
                        continue
                    proj = getattr(doc, "project", None)
                    proj_dir = getattr(proj, "path", "") if proj is not None else ""
                    candidate = os.path.join(proj_dir, fname) if proj_dir else fname
                    board_path = candidate
                    break
            except Exception as e:
                logger.debug(f"Could not get board path from IPC: {e}")

            if board_path and os.path.exists(board_path):
                # Load board via pcbnew
                pcb_board = pcbnew.LoadBoard(board_path)
            else:
                # Try to get from pcbnew directly
                pcb_board = pcbnew.GetBoard()

            if not pcb_board:
                logger.error("Could not get pcbnew board instance")
                return self._place_placeholder_footprint(
                    reference, "", x, y, rotation, layer, value
                )

            # Set footprint position and properties
            scale = MM_TO_NM
            loaded_fp.SetPosition(pcbnew.VECTOR2I(int(x * scale), int(y * scale)))
            loaded_fp.SetOrientationDegrees(rotation)

            # Set reference
            loaded_fp.SetReference(reference)

            # Set value if provided
            if value:
                loaded_fp.SetValue(value)

            # Set layer (flip if bottom)
            if layer == "B.Cu":
                if not loaded_fp.IsFlipped():
                    loaded_fp.Flip(loaded_fp.GetPosition(), False)

            # Add to board
            pcb_board.Add(loaded_fp)

            # Save the board so IPC can see the changes
            pcbnew.SaveBoard(board_path, pcb_board)

            # Refresh IPC view
            try:
                board.revert()  # Reload from disk to sync IPC
            except Exception as e:
                logger.debug(f"Could not refresh IPC board: {e}")

            self._notify(
                "component_placed",
                {
                    "reference": reference,
                    "footprint": loaded_fp.GetFPIDAsString(),
                    "position": {"x": x, "y": y},
                    "rotation": rotation,
                    "layer": layer,
                    "loaded_from_library": True,
                },
            )

            logger.info(
                f"Placed component {reference} ({loaded_fp.GetFPIDAsString()}) at ({x}, {y}) mm"
            )
            return True

        except Exception as e:
            logger.error(f"Error placing loaded footprint: {e}")
            # Fall back to placeholder
            return self._place_placeholder_footprint(reference, "", x, y, rotation, layer, value)

    def _place_placeholder_footprint(
        self,
        reference: str,
        footprint: str,
        x: float,
        y: float,
        rotation: float,
        layer: str,
        value: str,
    ) -> bool:
        """
        Place a placeholder footprint when library loading fails.

        Creates a basic footprint via IPC with just reference/value fields.
        """
        try:
            from kipy.board_types import Footprint
            from kipy.geometry import Angle, Vector2
            from kipy.proto.board.board_types_pb2 import BoardLayer
            from kipy.util.units import from_mm

            board = self._get_board()

            # Create footprint
            fp = Footprint()
            fp.position = Vector2.from_xy(from_mm(x), from_mm(y))
            fp.orientation = Angle.from_degrees(rotation)

            # Set layer
            if layer == "B.Cu":
                fp.layer = BoardLayer.BL_B_Cu
            else:
                fp.layer = BoardLayer.BL_F_Cu

            # Set reference and value
            if fp.reference_field:
                fp.reference_field.text.value = reference
            if fp.value_field:
                fp.value_field.text.value = value if value else footprint

            self._apply_create(board, fp, f"Placed component {reference}")

            self._notify(
                "component_placed",
                {
                    "reference": reference,
                    "footprint": footprint,
                    "position": {"x": x, "y": y},
                    "rotation": rotation,
                    "layer": layer,
                    "loaded_from_library": False,
                    "is_placeholder": True,
                },
            )

            logger.info(f"Placed placeholder component {reference} at ({x}, {y}) mm")
            return True

        except Exception as e:
            logger.error(f"Failed to place placeholder component: {e}")
            return False

    def move_component(
        self, reference: str, x: float, y: float, rotation: Optional[float] = None
    ) -> bool:
        """Move a component to a new position (updates UI immediately)."""
        try:
            from kipy.geometry import Angle, Vector2
            from kipy.util.units import from_mm

            board = self._get_board()
            footprints = board.get_footprints()

            # Find the footprint by reference
            target_fp = None
            for fp in footprints:
                if fp.reference_field and fp.reference_field.text.value == reference:
                    target_fp = fp
                    break

            if not target_fp:
                logger.error(f"Component not found: {reference}")
                return False

            # Update position
            target_fp.position = Vector2.from_xy(from_mm(x), from_mm(y))

            if rotation is not None:
                target_fp.orientation = Angle.from_degrees(rotation)

            self._apply_update(board, [target_fp], f"Moved component {reference}")

            self._notify(
                "component_moved",
                {"reference": reference, "position": {"x": x, "y": y}, "rotation": rotation},
            )

            return True

        except Exception as e:
            logger.error(f"Failed to move component: {e}")
            return False

    def delete_component(self, reference: str) -> bool:
        """Delete a component from the board."""
        try:
            board = self._get_board()
            footprints = board.get_footprints()

            # Find the footprint by reference
            target_fp = None
            for fp in footprints:
                if fp.reference_field and fp.reference_field.text.value == reference:
                    target_fp = fp
                    break

            if not target_fp:
                logger.error(f"Component not found: {reference}")
                return False

            self._apply_remove(board, [target_fp], f"Deleted component {reference}")

            self._notify("component_deleted", {"reference": reference})

            return True

        except Exception as e:
            logger.error(f"Failed to delete component: {e}")
            return False

    def add_track(
        self,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        width: float = 0.25,
        layer: str = "F.Cu",
        net_name: Optional[str] = None,
    ) -> bool:
        """
        Add a track (trace) to the board.

        The track appears immediately in the KiCAD UI.
        """
        try:
            from kipy.board_types import Track
            from kipy.geometry import Vector2
            from kipy.proto.board.board_types_pb2 import BoardLayer
            from kipy.util.units import from_mm

            board = self._get_board()

            # Create track
            track = Track()
            track.start = Vector2.from_xy(from_mm(start_x), from_mm(start_y))
            track.end = Vector2.from_xy(from_mm(end_x), from_mm(end_y))
            track.width = from_mm(width)

            # Set layer
            layer_map = {
                "F.Cu": BoardLayer.BL_F_Cu,
                "B.Cu": BoardLayer.BL_B_Cu,
                "In1.Cu": BoardLayer.BL_In1_Cu,
                "In2.Cu": BoardLayer.BL_In2_Cu,
            }
            track.layer = layer_map.get(layer, BoardLayer.BL_F_Cu)

            # Set net if specified
            if net_name:
                nets = board.get_nets()
                for net in nets:
                    if net.name == net_name:
                        track.net = net
                        break

            self._apply_create(board, track, "Added track")

            self._notify(
                "track_added",
                {
                    "start": {"x": start_x, "y": start_y},
                    "end": {"x": end_x, "y": end_y},
                    "width": width,
                    "layer": layer,
                    "net": net_name,
                },
            )

            logger.info(f"Added track from ({start_x}, {start_y}) to ({end_x}, {end_y}) mm")
            return True

        except Exception as e:
            logger.error(f"Failed to add track: {e}")
            return False

    def add_arc_track(
        self,
        start_x: float,
        start_y: float,
        mid_x: float,
        mid_y: float,
        end_x: float,
        end_y: float,
        width: float = 0.25,
        layer: str = "F.Cu",
        net_name: Optional[str] = None,
    ) -> bool:
        """Add a copper arc track to the board."""
        try:
            from kipy.board_types import ArcTrack
            from kipy.geometry import Vector2
            from kipy.proto.board.board_types_pb2 import BoardLayer
            from kipy.util.units import from_mm

            board = self._get_board()

            arc = ArcTrack()
            arc.start = Vector2.from_xy(from_mm(start_x), from_mm(start_y))
            arc.mid = Vector2.from_xy(from_mm(mid_x), from_mm(mid_y))
            arc.end = Vector2.from_xy(from_mm(end_x), from_mm(end_y))
            arc.width = from_mm(width)

            layer_map = {
                "F.Cu": BoardLayer.BL_F_Cu,
                "B.Cu": BoardLayer.BL_B_Cu,
                "In1.Cu": BoardLayer.BL_In1_Cu,
                "In2.Cu": BoardLayer.BL_In2_Cu,
            }
            arc.layer = layer_map.get(layer, BoardLayer.BL_F_Cu)

            if net_name:
                nets = board.get_nets()
                for net in nets:
                    if net.name == net_name:
                        arc.net = net
                        break

            self._apply_create(board, arc, "Added arc track")

            self._notify(
                "arc_track_added",
                {
                    "start": {"x": start_x, "y": start_y},
                    "mid": {"x": mid_x, "y": mid_y},
                    "end": {"x": end_x, "y": end_y},
                    "width": width,
                    "layer": layer,
                    "net": net_name,
                },
            )
            logger.info(
                f"Added arc track start=({start_x}, {start_y}) mid=({mid_x}, {mid_y}) end=({end_x}, {end_y}) mm"
            )
            return True
        except Exception as e:
            logger.error(f"Failed to add arc track: {e}")
            return False

    def add_via(
        self,
        x: float,
        y: float,
        diameter: float = 0.8,
        drill: float = 0.4,
        net_name: Optional[str] = None,
        via_type: str = "through",
    ) -> bool:
        """
        Add a via to the board.

        The via appears immediately in the KiCAD UI.
        """
        try:
            from kipy.board_types import Via
            from kipy.geometry import Vector2
            from kipy.proto.board.board_types_pb2 import ViaType
            from kipy.util.units import from_mm

            board = self._get_board()

            # Create via
            via = Via()
            via.position = Vector2.from_xy(from_mm(x), from_mm(y))
            via.diameter = from_mm(diameter)
            via.drill_diameter = from_mm(drill)

            # Set via type (enum values: VT_THROUGH=1, VT_BLIND_BURIED=2, VT_MICRO=3)
            type_map = {
                "through": ViaType.VT_THROUGH,
                "blind": ViaType.VT_BLIND_BURIED,
                "micro": ViaType.VT_MICRO,
            }
            via.type = type_map.get(via_type, ViaType.VT_THROUGH)

            # Set net if specified
            if net_name:
                nets = board.get_nets()
                for net in nets:
                    if net.name == net_name:
                        via.net = net
                        break

            self._apply_create(board, via, "Added via")

            self._notify(
                "via_added",
                {
                    "position": {"x": x, "y": y},
                    "diameter": diameter,
                    "drill": drill,
                    "net": net_name,
                    "type": via_type,
                },
            )

            logger.info(f"Added via at ({x}, {y}) mm")
            return True

        except Exception as e:
            logger.error(f"Failed to add via: {e}")
            return False

    def add_text(
        self,
        text: str,
        x: float,
        y: float,
        layer: str = "F.SilkS",
        size: float = 1.0,
        rotation: float = 0,
    ) -> bool:
        """Add text to the board."""
        try:
            from kipy.board_types import BoardText
            from kipy.geometry import Angle, Vector2
            from kipy.proto.board.board_types_pb2 import BoardLayer
            from kipy.util.units import from_mm

            board = self._get_board()

            # Create text
            board_text = BoardText()
            board_text.value = text
            board_text.position = Vector2.from_xy(from_mm(x), from_mm(y))
            board_text.angle = Angle.from_degrees(rotation)

            # Set layer
            layer_map = {
                "F.SilkS": BoardLayer.BL_F_SilkS,
                "B.SilkS": BoardLayer.BL_B_SilkS,
                "F.Cu": BoardLayer.BL_F_Cu,
                "B.Cu": BoardLayer.BL_B_Cu,
            }
            board_text.layer = layer_map.get(layer, BoardLayer.BL_F_SilkS)

            self._apply_create(board, board_text, f"Added text: {text}")

            self._notify("text_added", {"text": text, "position": {"x": x, "y": y}, "layer": layer})

            return True

        except Exception as e:
            logger.error(f"Failed to add text: {e}")
            return False

    def get_tracks(self) -> List[Dict[str, Any]]:
        """Get all tracks on the board."""
        try:
            from kipy.util.units import to_mm

            board = self._get_board()
            tracks = board.get_tracks()

            result = []
            for track in tracks:
                try:
                    result.append(
                        {
                            "start": {"x": to_mm(track.start.x), "y": to_mm(track.start.y)},
                            "end": {"x": to_mm(track.end.x), "y": to_mm(track.end.y)},
                            "width": to_mm(track.width),
                            "layer": str(track.layer),
                            "net": track.net.name if track.net else "",
                            "id": str(track.id) if hasattr(track, "id") else "",
                        }
                    )
                except Exception as e:
                    logger.warning(f"Error processing track: {e}")
                    continue

            return result

        except Exception as e:
            logger.error(f"Failed to get tracks: {e}")
            return []

    def get_vias(self) -> List[Dict[str, Any]]:
        """Get all vias on the board."""
        try:
            from kipy.util.units import to_mm

            board = self._get_board()
            vias = board.get_vias()

            result = []
            for via in vias:
                try:
                    result.append(
                        {
                            "position": {"x": to_mm(via.position.x), "y": to_mm(via.position.y)},
                            "diameter": to_mm(via.diameter),
                            "drill": to_mm(via.drill_diameter),
                            "net": via.net.name if via.net else "",
                            "type": str(via.type),
                            "id": str(via.id) if hasattr(via, "id") else "",
                        }
                    )
                except Exception as e:
                    logger.warning(f"Error processing via: {e}")
                    continue

            return result

        except Exception as e:
            logger.error(f"Failed to get vias: {e}")
            return []

    def get_nets(self) -> List[Dict[str, Any]]:
        """Get all nets on the board."""
        try:
            board = self._get_board()
            nets = board.get_nets()

            result = []
            for net in nets:
                try:
                    result.append(
                        {"name": net.name, "code": net.code if hasattr(net, "code") else 0}
                    )
                except Exception as e:
                    logger.warning(f"Error processing net: {e}")
                    continue

            return result

        except Exception as e:
            logger.error(f"Failed to get nets: {e}")
            return []

    def add_zone(
        self,
        points: List[Dict[str, float]],
        layer: str = "F.Cu",
        net_name: Optional[str] = None,
        clearance: float = 0.5,
        min_thickness: float = 0.25,
        priority: int = 0,
        fill_mode: str = "solid",
        name: str = "",
    ) -> bool:
        """
        Add a copper pour zone to the board.

        The zone appears immediately in the KiCAD UI.

        Args:
            points: List of points defining the zone outline, e.g. [{"x": 0, "y": 0}, ...]
            layer: Layer name (F.Cu, B.Cu, etc.)
            net_name: Net to connect the zone to (e.g., "GND")
            clearance: Clearance from other copper in mm
            min_thickness: Minimum copper thickness in mm
            priority: Zone priority (higher = fills first)
            fill_mode: "solid" or "hatched"
            name: Optional zone name
        """
        try:
            from kipy.board_types import Zone, ZoneType
            from kipy.geometry import PolyLine, PolyLineNode
            from kipy.proto.board.board_types_pb2 import BoardLayer, ZoneFillMode
            from kipy.util.units import from_mm

            board = self._get_board()

            if len(points) < 3:
                logger.error("Zone requires at least 3 points")
                return False

            # Create zone
            zone = Zone()
            zone.type = ZoneType.ZT_COPPER

            # Set layer
            layer_map = {
                "F.Cu": BoardLayer.BL_F_Cu,
                "B.Cu": BoardLayer.BL_B_Cu,
                "In1.Cu": BoardLayer.BL_In1_Cu,
                "In2.Cu": BoardLayer.BL_In2_Cu,
                "In3.Cu": BoardLayer.BL_In3_Cu,
                "In4.Cu": BoardLayer.BL_In4_Cu,
            }
            zone.layers = [layer_map.get(layer, BoardLayer.BL_F_Cu)]

            # Set net if specified
            if net_name:
                nets = board.get_nets()
                for net in nets:
                    if net.name == net_name:
                        zone.net = net
                        break

            # Set zone properties
            zone.clearance = from_mm(clearance)
            zone.min_thickness = from_mm(min_thickness)
            zone.priority = priority

            if name:
                zone.name = name

            # Set fill mode.  kipy 10 made Zone.fill_mode getter-only, so
            # assign the underlying proto enum directly (the old
            # `zone.fill_mode = ...` raised "property has no setter" and
            # every copper pour silently failed).
            zone._proto.copper_settings.fill_mode = (
                ZoneFillMode.ZFM_HATCHED if fill_mode == "hatched" else ZoneFillMode.ZFM_SOLID
            )

            # Create outline polyline
            outline = PolyLine()
            outline.closed = True

            for point in points:
                x = point.get("x", 0)
                y = point.get("y", 0)
                node = PolyLineNode.from_xy(from_mm(x), from_mm(y))
                outline.append(node)

            # Set the outline on the zone
            # Note: Zone outline is set via the proto directly since kipy
            # doesn't expose a direct setter for creating new zones
            zone._proto.outline.polygons.add()
            zone._proto.outline.polygons[0].outline.CopyFrom(outline._proto)

            self._apply_create(board, zone, f"Added copper zone on {layer}")

            self._notify(
                "zone_added",
                {"layer": layer, "net": net_name, "points": len(points), "priority": priority},
            )

            logger.info(f"Added zone on {layer} with {len(points)} points")
            return True

        except Exception as e:
            logger.error(f"Failed to add zone: {e}")
            return False

    def get_zones(self) -> List[Dict[str, Any]]:
        """Get all zones on the board."""
        try:
            board = self._get_board()
            zones = board.get_zones()

            result = []
            for zone in zones:
                try:
                    result.append(
                        {
                            "name": zone.name if hasattr(zone, "name") else "",
                            "net": zone.net.name if zone.net else "",
                            "priority": zone.priority if hasattr(zone, "priority") else 0,
                            "layers": (
                                [str(l) for l in zone.layers] if hasattr(zone, "layers") else []
                            ),
                            "filled": zone.filled if hasattr(zone, "filled") else False,
                            "id": str(zone.id) if hasattr(zone, "id") else "",
                        }
                    )
                except Exception as e:
                    logger.warning(f"Error processing zone: {e}")
                    continue

            return result

        except Exception as e:
            logger.error(f"Failed to get zones: {e}")
            return []

    def refill_zones(self) -> bool:
        """Refill all copper pour zones."""
        try:
            board = self._get_board()
            board.refill_zones()
            self._notify("zones_refilled", {})
            return True
        except Exception as e:
            logger.error(f"Failed to refill zones: {e}")
            return False

    # ------------------------------------------------------------------
    # Selection / interaction
    # ------------------------------------------------------------------
    def get_selection(self) -> List[Dict[str, Any]]:
        """Get currently selected items in the KiCAD UI.

        Returns one dict per item with at least ``id`` and ``type``, plus a
        few common attributes (reference / value for footprints, position /
        layer where available) so a caller can identify what's selected
        without a second round-trip.
        """
        try:
            board = self._get_board()
            selection = board.get_selection()
            return [self._describe_item(item) for item in selection]
        except Exception as e:
            logger.error(f"Failed to get selection: {e}")
            return []

    def clear_selection(self) -> bool:
        """Clear the current selection in KiCAD UI."""
        try:
            board = self._get_board()
            board.clear_selection()
            self._notify("selection_cleared", {})
            return True
        except Exception as e:
            logger.error(f"Failed to clear selection: {e}")
            return False

    def add_to_selection(self, ids: List[str]) -> Dict[str, Any]:
        """Add board items (by KIID) to the current selection."""
        return self._mutate_selection(ids, add=True)

    def remove_from_selection(self, ids: List[str]) -> Dict[str, Any]:
        """Remove board items (by KIID) from the current selection."""
        return self._mutate_selection(ids, add=False)

    def _mutate_selection(self, ids: List[str], *, add: bool) -> Dict[str, Any]:
        try:
            board = self._get_board()
            items = self._resolve_items_by_ids(board, ids)
            if not items:
                return {
                    "success": False,
                    "message": "No items resolved from supplied IDs",
                    "requested": list(ids),
                    "resolved": 0,
                }
            updated = board.add_to_selection(items) if add else board.remove_from_selection(items)
            event = "selection_added" if add else "selection_removed"
            self._notify(event, {"ids": list(ids), "count": len(items)})
            return {
                "success": True,
                "requested": list(ids),
                "resolved": len(items),
                "selection": [self._describe_item(i) for i in updated],
            }
        except Exception as e:
            logger.error(f"Failed to {'add to' if add else 'remove from'} selection: {e}")
            return {"success": False, "message": str(e)}

    def hit_test(
        self,
        x: float,
        y: float,
        item_id: Optional[str] = None,
        tolerance: float = 0,
        unit: str = "mm",
    ) -> Dict[str, Any]:
        """Hit-test a board item at ``(x, y)``.

        If ``item_id`` is given, test only that item. Otherwise, sweep all
        footprints, tracks, vias, zones, and graphic shapes and return every
        item whose ``hit_test`` returns True — useful for "what's at this
        coordinate?" queries.

        ``tolerance`` is in the same unit as the coordinates.
        """
        try:
            from kipy.geometry import Vector2
            from kipy.util.units import from_mm

            board = self._get_board()
            scale = MM_TO_NM if unit == "mm" else INCH_TO_NM
            position = Vector2.from_xy(int(x * scale), int(y * scale))
            tol_nm = int(tolerance * scale)

            if item_id:
                items = self._resolve_items_by_ids(board, [item_id])
                if not items:
                    return {"success": False, "message": f"Item {item_id} not found"}
                hit = bool(board.hit_test(items[0], position, tol_nm))
                return {
                    "success": True,
                    "hit": hit,
                    "items": [self._describe_item(items[0])] if hit else [],
                }

            # Sweep — collect anything underneath the cursor.
            from_mm  # keep import for type-checkers; not used here directly
            candidates: List[Any] = []
            for getter in (
                "get_footprints",
                "get_tracks",
                "get_vias",
                "get_zones",
                "get_shapes",
            ):
                try:
                    candidates.extend(list(getattr(board, getter)()))
                except Exception as e:
                    logger.debug(f"hit_test sweep: {getter} failed: {e}")

            hits = []
            for item in candidates:
                try:
                    if board.hit_test(item, position, tol_nm):
                        hits.append(self._describe_item(item))
                except Exception as e:
                    logger.debug(f"hit_test on item failed: {e}")
                    continue

            return {"success": True, "hit": bool(hits), "items": hits, "count": len(hits)}
        except Exception as e:
            logger.error(f"Failed to hit-test: {e}")
            return {"success": False, "message": str(e)}

    def interactive_move(self, ids: List[str]) -> Dict[str, Any]:
        """Initiate KiCad's interactive move tool on the given items.

        This is a blocking-style operation in KiCad — future API calls return
        AS_BUSY until the user finishes the drag.  We return immediately;
        callers should not chain further mutations until the user releases.
        """
        try:
            board = self._get_board()
            items = self._resolve_items_by_ids(board, ids)
            if not items:
                return {
                    "success": False,
                    "message": "No items resolved from supplied IDs",
                    "requested": list(ids),
                }
            # kipy's interactive_move accepts a single KIID or an iterable.
            # Pass the proto KIIDs (item.id), not the wrappers.
            board.interactive_move([item.id for item in items])
            self._notify("interactive_move", {"ids": list(ids), "count": len(items)})
            return {
                "success": True,
                "requested": list(ids),
                "resolved": len(items),
                "message": (
                    "Interactive move started — KiCAD UI is now in drag mode. "
                    "Further API calls will return AS_BUSY until the user releases."
                ),
            }
        except Exception as e:
            logger.error(f"Failed to start interactive move: {e}")
            return {"success": False, "message": str(e)}

    # ------------------------------------------------------------------
    # Drawing primitives — graphic shapes on any layer.
    #
    # These are *graphic* shapes (no net association unless layer is Cu).
    # For copper traces use add_track / route_trace; for filled copper use
    # add_zone.  Routed *arc tracks* (copper) live on add_arc_track —
    # add_arc here is the graphic version for silk / fab / Edge.Cuts /
    # User layers.
    # ------------------------------------------------------------------
    def add_segment(
        self,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        width: float = 0.15,
        layer: str = "F.SilkS",
    ) -> Dict[str, Any]:
        """Add a straight graphic line on any layer."""
        try:
            from kipy.board_types import BoardSegment
            from kipy.geometry import Vector2
            from kipy.util.units import from_mm

            board = self._get_board()
            seg = BoardSegment()
            seg.start = Vector2.from_xy(from_mm(start_x), from_mm(start_y))
            seg.end = Vector2.from_xy(from_mm(end_x), from_mm(end_y))
            seg.layer = self._layer_to_enum(layer)
            seg.attributes.stroke.width = from_mm(width)
            created_id = self._apply_create(board, seg, f"Added segment on {layer}")
            self._notify(
                "shape_added",
                {
                    "kind": "segment",
                    "layer": layer,
                    "start": {"x": start_x, "y": start_y},
                    "end": {"x": end_x, "y": end_y},
                },
            )
            return {"success": True, "id": created_id, "layer": layer}
        except Exception as e:
            logger.error(f"Failed to add segment: {e}")
            return {"success": False, "message": str(e)}

    def add_arc(
        self,
        start_x: float,
        start_y: float,
        mid_x: float,
        mid_y: float,
        end_x: float,
        end_y: float,
        width: float = 0.15,
        layer: str = "F.SilkS",
    ) -> Dict[str, Any]:
        """Add a graphic arc on any layer (start → mid → end)."""
        try:
            from kipy.board_types import BoardArc
            from kipy.geometry import Vector2
            from kipy.util.units import from_mm

            board = self._get_board()
            arc = BoardArc()
            arc.start = Vector2.from_xy(from_mm(start_x), from_mm(start_y))
            arc.mid = Vector2.from_xy(from_mm(mid_x), from_mm(mid_y))
            arc.end = Vector2.from_xy(from_mm(end_x), from_mm(end_y))
            arc.layer = self._layer_to_enum(layer)
            arc.attributes.stroke.width = from_mm(width)
            created_id = self._apply_create(board, arc, f"Added arc on {layer}")
            self._notify(
                "shape_added",
                {
                    "kind": "arc",
                    "layer": layer,
                    "start": {"x": start_x, "y": start_y},
                    "mid": {"x": mid_x, "y": mid_y},
                    "end": {"x": end_x, "y": end_y},
                },
            )
            return {"success": True, "id": created_id, "layer": layer}
        except Exception as e:
            logger.error(f"Failed to add arc: {e}")
            return {"success": False, "message": str(e)}

    def add_circle(
        self,
        center_x: float,
        center_y: float,
        radius: float,
        width: float = 0.15,
        layer: str = "F.SilkS",
        filled: bool = False,
    ) -> Dict[str, Any]:
        """Add a graphic circle on any layer.

        ``filled=True`` produces a solid disc (radius is the disc radius);
        ``filled=False`` produces a stroked ring of the given ``width``.
        """
        try:
            from kipy.board_types import BoardCircle
            from kipy.geometry import Vector2
            from kipy.util.units import from_mm

            board = self._get_board()
            circle = BoardCircle()
            circle.center = Vector2.from_xy(from_mm(center_x), from_mm(center_y))
            # radius is given as a "point on the circle" in kipy — pick a
            # canonical one to the right of centre.
            circle.radius_point = Vector2.from_xy(from_mm(center_x + radius), from_mm(center_y))
            circle.layer = self._layer_to_enum(layer)
            circle.attributes.stroke.width = from_mm(width)
            circle.attributes.fill.filled = bool(filled)
            created_id = self._apply_create(board, circle, f"Added circle on {layer}")
            self._notify(
                "shape_added",
                {
                    "kind": "circle",
                    "layer": layer,
                    "center": {"x": center_x, "y": center_y},
                    "radius": radius,
                    "filled": filled,
                },
            )
            return {"success": True, "id": created_id, "layer": layer}
        except Exception as e:
            logger.error(f"Failed to add circle: {e}")
            return {"success": False, "message": str(e)}

    def add_rectangle(
        self,
        top_left_x: float,
        top_left_y: float,
        bottom_right_x: float,
        bottom_right_y: float,
        width: float = 0.15,
        layer: str = "F.SilkS",
        filled: bool = False,
    ) -> Dict[str, Any]:
        """Add a graphic rectangle on any layer (axis-aligned)."""
        try:
            from kipy.board_types import BoardRectangle
            from kipy.geometry import Vector2
            from kipy.util.units import from_mm

            board = self._get_board()
            rect = BoardRectangle()
            rect.top_left = Vector2.from_xy(from_mm(top_left_x), from_mm(top_left_y))
            rect.bottom_right = Vector2.from_xy(from_mm(bottom_right_x), from_mm(bottom_right_y))
            rect.layer = self._layer_to_enum(layer)
            rect.attributes.stroke.width = from_mm(width)
            rect.attributes.fill.filled = bool(filled)
            created_id = self._apply_create(board, rect, f"Added rectangle on {layer}")
            self._notify(
                "shape_added",
                {
                    "kind": "rectangle",
                    "layer": layer,
                    "topLeft": {"x": top_left_x, "y": top_left_y},
                    "bottomRight": {"x": bottom_right_x, "y": bottom_right_y},
                    "filled": filled,
                },
            )
            return {"success": True, "id": created_id, "layer": layer}
        except Exception as e:
            logger.error(f"Failed to add rectangle: {e}")
            return {"success": False, "message": str(e)}

    def add_polygon(
        self,
        points: List[Dict[str, float]],
        width: float = 0.15,
        layer: str = "F.SilkS",
        filled: bool = False,
    ) -> Dict[str, Any]:
        """Add a closed graphic polygon on any layer.

        ``points`` is a list of ``{"x": ..., "y": ...}`` in mm.  At least 3
        points are required.  ``filled=True`` produces a solid polygon;
        ``filled=False`` produces a stroked outline of the given ``width``.
        """
        try:
            from kipy.board_types import BoardPolygon
            from kipy.util.units import from_mm

            if len(points) < 3:
                return {"success": False, "message": "Polygon requires at least 3 points"}

            board = self._get_board()
            poly = BoardPolygon()
            # Write the polygon outline through the proto directly — the
            # kipy wrapper's `polygons` list is a one-way cache that doesn't
            # round-trip into the proto on append.  Same trick the existing
            # add_zone() code uses for Zone outlines.
            pwh_proto = poly._proto.shape.polygon.polygons.add()
            pwh_proto.outline.closed = True
            for pt in points:
                px = float(pt.get("x", 0))
                py = float(pt.get("y", 0))
                node = pwh_proto.outline.nodes.add()
                node.point.x_nm = from_mm(px)
                node.point.y_nm = from_mm(py)
            poly.layer = self._layer_to_enum(layer)
            poly.attributes.stroke.width = from_mm(width)
            poly.attributes.fill.filled = bool(filled)
            created_id = self._apply_create(board, poly, f"Added polygon on {layer}")
            self._notify(
                "shape_added",
                {
                    "kind": "polygon",
                    "layer": layer,
                    "points": len(points),
                    "filled": filled,
                },
            )
            return {"success": True, "id": created_id, "layer": layer}
        except Exception as e:
            logger.error(f"Failed to add polygon: {e}")
            return {"success": False, "message": str(e)}

    # ------------------------------------------------------------------
    # Board metadata: origins + title block
    # ------------------------------------------------------------------
    def get_origin(self, origin_type: str = "drill", unit: str = "mm") -> Dict[str, Any]:
        """Return the requested board origin in user units.

        ``origin_type`` is ``"grid"`` (the user grid origin) or
        ``"drill"`` (the drill/place a.k.a. aux origin — what Gerber and
        pick-and-place files use as their coordinate zero).
        ``unit`` is ``"mm"`` or ``"inch"``; anything else is rejected
        (silent fallback would mis-label inch values as mm or vice versa).
        """
        try:
            from kipy.util.units import to_mm

            self._require_unit(unit)
            board = self._get_board()
            type_int = self._origin_name_to_enum(origin_type)
            origin = board.get_origin(type_int)
            x_nm = int(origin.x)
            y_nm = int(origin.y)
            if unit == "inch":
                x = x_nm / INCH_TO_NM
                y = y_nm / INCH_TO_NM
            else:
                x = to_mm(x_nm)
                y = to_mm(y_nm)
            return {
                "success": True,
                "type": origin_type,
                "x": x,
                "y": y,
                "unit": unit,
            }
        except Exception as e:
            logger.error(f"Failed to get origin: {e}")
            return {"success": False, "message": str(e)}

    def set_origin(
        self,
        origin_type: str,
        x: float,
        y: float,
        unit: str = "mm",
    ) -> Dict[str, Any]:
        """Set the grid or drill/place origin to ``(x, y)`` in user units.

        ``unit`` must be ``"mm"`` or ``"inch"`` — unknown units are
        rejected rather than silently treated as mm.
        """
        try:
            from kipy.geometry import Vector2
            from kipy.util.units import from_mm

            self._require_unit(unit)
            board = self._get_board()
            type_int = self._origin_name_to_enum(origin_type)
            if unit == "inch":
                x_nm = int(x * INCH_TO_NM)
                y_nm = int(y * INCH_TO_NM)
            else:
                x_nm = from_mm(x)
                y_nm = from_mm(y)
            board.set_origin(type_int, Vector2.from_xy(x_nm, y_nm))
            self._notify(
                "origin_set",
                {"type": origin_type, "x": x, "y": y, "unit": unit},
            )
            return {
                "success": True,
                "type": origin_type,
                "x": x,
                "y": y,
                "unit": unit,
            }
        except Exception as e:
            logger.error(f"Failed to set origin: {e}")
            return {"success": False, "message": str(e)}

    def get_title_block_info(self) -> Dict[str, Any]:
        """Return the board title block — title / date / revision / company /
        comments (a dict keyed 1..9, KiCad's fixed nine comment slots)."""
        try:
            board = self._get_board()
            tb = board.get_title_block_info()
            return {
                "success": True,
                "title": tb.title,
                "date": tb.date,
                "revision": tb.revision,
                "company": tb.company,
                # Materialise as a string-keyed dict so it survives JSON
                # round-trips without integer-key coercion surprises.
                "comments": {str(k): v for k, v in tb.comments.items()},
            }
        except Exception as e:
            logger.error(f"Failed to get title block: {e}")
            return {"success": False, "message": str(e)}

    def set_title_block_info(
        self,
        title: Optional[str] = None,
        date: Optional[str] = None,
        revision: Optional[str] = None,
        company: Optional[str] = None,
        comments: Optional[Dict[int, str]] = None,
    ) -> Dict[str, Any]:
        """Update title block — any field left ``None`` is preserved.

        ``comments`` is a partial dict ``{slot: text}`` where ``slot`` is
        1..9.  Only listed slots are overwritten; the rest stay put.  Pass
        an explicit empty string to clear a slot.

        kipy's ``set_title_block_info`` replaces the whole block, so we
        fetch the current one, merge the incoming partial update, and send
        the result back.  This makes partial updates safe — without the
        get-merge-set dance a single missing field would erase the rest.
        """
        try:
            from kipy.common_types import TitleBlockInfo

            board = self._get_board()
            current = board.get_title_block_info()
            merged = TitleBlockInfo()
            merged.title = title if title is not None else current.title
            merged.date = date if date is not None else current.date
            merged.revision = revision if revision is not None else current.revision
            merged.company = company if company is not None else current.company
            # Comments are read-only via the wrapper's .comments property
            # (it constructs a fresh dict each call), so write through the
            # proto fields comment1..comment9 directly.  Source-of-truth
            # for unchanged slots is the *current* board state, not the
            # default-zero proto.
            for idx in range(1, 10):
                field = f"comment{idx}"
                setattr(merged._proto, field, getattr(current._proto, field))
            if comments:
                for k, v in comments.items():
                    try:
                        slot = int(k)
                    except (TypeError, ValueError):
                        logger.warning(f"Ignoring non-integer comment slot {k!r}")
                        continue
                    if 1 <= slot <= 9:
                        setattr(merged._proto, f"comment{slot}", str(v))
                    else:
                        logger.warning(f"Comment slot {slot} out of range 1..9; ignored")
            board.set_title_block_info(merged)
            self._notify(
                "title_block_set",
                {
                    "title": merged.title,
                    "date": merged.date,
                    "revision": merged.revision,
                    "company": merged.company,
                },
            )
            return {
                "success": True,
                "title": merged.title,
                "date": merged.date,
                "revision": merged.revision,
                "company": merged.company,
                "comments": {str(k): v for k, v in merged.comments.items()},
            }
        except Exception as e:
            logger.error(f"Failed to set title block: {e}")
            return {"success": False, "message": str(e)}

    @staticmethod
    def _require_unit(unit: str) -> None:
        """Reject any unit other than ``mm``/``inch``. Silent fallback would
        let a ``unit="mil"`` request walk through the mm code path and label
        the result as ``mil`` while the math used mm — wrong by 25.4×."""
        if unit not in ("mm", "inch"):
            raise ValueError(f"Unknown unit {unit!r}; expected 'mm' or 'inch'")

    @staticmethod
    def _origin_name_to_enum(name: str) -> int:
        """Resolve ``"grid"`` / ``"drill"`` / ``"aux"`` (alias for drill) to
        the ``BoardOriginType`` enum value.  Raises ``ValueError`` for
        anything else so callers see a clean error rather than silently
        falling back to ``BOT_UNKNOWN`` which kipy rejects."""
        from kipy.proto.board.board_commands_pb2 import BoardOriginType

        canonical = name.strip().lower()
        # "aux" is what the KiCad UI labels the drill/place origin as in
        # plot/export dialogs — accept it as a synonym.
        if canonical in ("drill", "aux", "drill/place"):
            return BoardOriginType.BOT_DRILL
        if canonical == "grid":
            return BoardOriginType.BOT_GRID
        raise ValueError(f"Unknown origin type {name!r}; expected 'grid', 'drill', or 'aux'")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _layer_to_enum(name: str) -> Any:
        """Map a dotted layer name (e.g. ``F.Cu``, ``Edge.Cuts``) to a
        ``BoardLayer`` enum value.

        The enum names follow ``BL_<dotted-name with '.' replaced by '_'>``,
        e.g. ``BL_F_Cu``, ``BL_Edge_Cuts``, ``BL_User_1``.  Unknown layers
        fall back to ``BL_F_SilkS`` rather than raising — graphic shapes
        with no layer set draw on nothing useful, so a visible default beats
        a hard failure.
        """
        from kipy.proto.board.board_types_pb2 import BoardLayer

        sanitized = "BL_" + name.replace(".", "_")
        value = BoardLayer.Value(sanitized) if sanitized in BoardLayer.keys() else None
        if value is None:
            logger.warning(f"Unknown layer {name!r}; defaulting to F.SilkS")
            return BoardLayer.BL_F_SilkS
        return value

    @staticmethod
    def _resolve_items_by_ids(board: Any, ids: List[str]) -> List[Any]:
        """Resolve KIID strings to BoardItem wrappers via the live board.

        Tries ``board.get_items_by_id`` first (newer kipy); falls back to a
        full scan if that's unavailable.  Unknown IDs are silently skipped —
        callers see the gap in the returned ``resolved`` count.
        """
        if not ids:
            return []
        # Preferred: bulk lookup by ID (kipy ≥ 9.x).
        try:
            return list(board.get_items_by_id(list(ids)))
        except Exception as e:
            logger.debug(f"get_items_by_id failed; falling back to scan: {e}")

        # Fallback: scan all known item collections.
        wanted = set(str(i) for i in ids)
        out: List[Any] = []
        for getter in (
            "get_footprints",
            "get_tracks",
            "get_vias",
            "get_zones",
            "get_shapes",
            "get_pads",
        ):
            try:
                for item in getattr(board, getter)():
                    if str(getattr(item, "id", "")) in wanted:
                        out.append(item)
            except Exception:
                continue
        return out

    @staticmethod
    def _describe_item(item: Any) -> Dict[str, Any]:
        """Build a JSON-safe summary of a BoardItem for selection / hit-test
        responses.  Tolerates missing attributes — the kipy wrapper shape
        varies by item type."""
        info: Dict[str, Any] = {
            "type": type(item).__name__,
            "id": str(getattr(item, "id", "")),
        }
        # Footprint-ish: surface reference + value when present.
        ref_field = getattr(item, "reference_field", None)
        if ref_field is not None:
            try:
                info["reference"] = ref_field.text.value
            except Exception:
                pass
        val_field = getattr(item, "value_field", None)
        if val_field is not None:
            try:
                info["value"] = val_field.text.value
            except Exception:
                pass
        # Position-ish: footprints / vias / pads / text.
        try:
            from kipy.util.units import to_mm

            pos = getattr(item, "position", None)
            if pos is not None and hasattr(pos, "x"):
                info["position"] = {"x": to_mm(pos.x), "y": to_mm(pos.y), "unit": "mm"}
        except Exception:
            pass
        layer = getattr(item, "layer", None)
        if layer is not None:
            info["layer"] = str(layer)
        return info


__all__ = ["IPCBackend", "IPCBoardAPI"]
