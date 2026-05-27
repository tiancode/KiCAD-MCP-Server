"""Tests for backend metadata added by KiCADInterface.handle_command."""

import sys
import types
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _make_iface(command_routes, use_ipc=False):
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = use_ipc
    iface.ipc_backend = None
    iface.ipc_board_api = None
    iface.board = None
    iface.command_routes = command_routes
    iface._board_disk_signature = None
    iface._current_project_path = None
    iface._last_auto_save_status = None
    return iface


class _FakeIPCBoardAPI:
    def get_size(self):
        return {"width": 10, "height": 20, "unit": "mm"}

    def list_components(self):
        return []

    def get_tracks(self):
        return [
            {
                "id": "track-1",
                "start": {"x": 0, "y": 0},
                "end": {"x": 3, "y": 4},
                "width": 0.25,
                "layer": "BL_F_Cu",
                "net": "N$1",
            }
        ]

    def get_vias(self):
        return []

    def get_nets(self):
        return [{"name": "N$1", "code": 1}]

    def get_enabled_layers(self):
        return ["F.Cu", "B.Cu"]


class _FakeBoard:
    def __init__(self, filename):
        self._filename = str(filename)

    def GetFileName(self):
        return self._filename

    def GetDesignSettings(self):
        return object()

    def GetBoardEdgesBoundingBox(self):
        return object()


class _FakeIPCBackend:
    def __init__(self):
        self.connected = False

    def connect(self):
        self.connected = True
        return True

    def is_connected(self):
        return self.connected

    def get_board(self):
        return _FakeIPCBoardAPI()

    def get_version(self):
        return "9.0-test"


class _ConnectShouldNotBeCalledIPCBackend(_FakeIPCBackend):
    def connect(self):
        raise AssertionError("IPC reconnect should not be attempted")


class _FailingConnectIPCBackend(_FakeIPCBackend):
    def connect(self):
        raise RuntimeError("IPC unavailable")


class _NoBoardIPCBackend(_FakeIPCBackend):
    def get_board(self):
        raise RuntimeError("No board open")


class _FilteringIPCBoardAPI(_FakeIPCBoardAPI):
    def get_tracks(self):
        return [
            {
                "id": "track-1",
                "start": {"x": 0, "y": 0},
                "end": {"x": 3, "y": 4},
                "width": 0.25,
                "layer": "BL_F_Cu",
                "net": "N$1",
                "netCode": 1,
            },
            {
                "id": "track-2",
                "start": {"x": 10, "y": 10},
                "end": {"x": 11, "y": 11},
                "width": 0.2,
                "layer": "BL_B_Cu",
                "net": "N$2",
                "netCode": 2,
            },
        ]

    def get_vias(self):
        return [
            {
                "id": "via-1",
                "position": {"x": 0.5, "y": 0.5},
                "diameter": 0.8,
                "drill": 0.4,
                "net": "N$1",
                "netCode": 1,
            },
            {
                "id": "via-2",
                "position": {"x": 20, "y": 20},
                "diameter": 0.8,
                "drill": 0.4,
                "net": "N$2",
                "netCode": 2,
            },
        ]


class _Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y


class _BoxWithPosSize:
    def __init__(self, x, y, width, height):
        self.pos = _Point(x, y)
        self.size = _Point(width, height)


class _BoxWithMinMax:
    def __init__(self, min_x, min_y, max_x, max_y):
        self.min = _Point(min_x, min_y)
        self.max = _Point(max_x, max_y)


class _BoundingBoxBoard:
    def __init__(self, boxes):
        self._boxes = boxes

    def get_shapes(self):
        return list(range(len(self._boxes)))

    def get_item_bounding_box(self, shape):
        return self._boxes[shape]


def _stub_kipy_units(monkeypatch):
    units_module = types.ModuleType("kipy.util.units")
    units_module.to_mm = lambda nm: nm / 1_000_000
    monkeypatch.setitem(sys.modules, "kipy", types.ModuleType("kipy"))
    monkeypatch.setitem(sys.modules, "kipy.util", types.ModuleType("kipy.util"))
    monkeypatch.setitem(sys.modules, "kipy.util.units", units_module)


def test_dispatcher_no_longer_stamps_backend_banner_fields():
    """The dispatcher used to inject ``_backend`` / ``_realtime`` (and a
    long ``_recommendation`` string on every SWIG success) into every
    response.  The user asked for all three to be gone — they were noise
    on hot paths and the SWIG-tagged recommendation was misleading when
    surfaced on schematic / file-only tools that can't use IPC anyway.
    Backend state is still queryable via ``get_backend_state`` /
    ``get_backend_info``; tools just no longer carry it inline.
    """
    # Generic SWIG-routed command.
    iface = _make_iface(
        {
            "get_project_info": lambda params: {
                "success": True,
                "project": {"name": "demo"},
            }
        },
        use_ipc=True,
    )
    result = iface.handle_command("get_project_info", {})
    for noisy in ("_backend", "_realtime", "_recommendation"):
        assert noisy not in result, f"{noisy} should not be stamped on responses"

    # Explicit IPC handler (ipc_add_track) — used to get ``_backend: ipc``.
    iface = _make_iface(
        {
            "ipc_add_track": lambda params: {
                "success": True,
                "message": "Track added",
                "realtime": True,
            }
        },
        use_ipc=True,
    )
    result = iface.handle_command("ipc_add_track", {})
    for noisy in ("_backend", "_realtime", "_recommendation"):
        assert noisy not in result

    # Meta command (get_backend_info) — handler returns its own
    # backend/realtime_sync fields, dispatcher must not shadow them.
    iface = _make_iface(
        {
            "get_backend_info": lambda params: {
                "success": True,
                "backend": "ipc",
                "realtime_sync": True,
            }
        },
        use_ipc=True,
    )
    result = iface.handle_command("get_backend_info", {})
    assert result["backend"] == "ipc"
    assert result["realtime_sync"] is True
    for noisy in ("_backend", "_realtime", "_recommendation"):
        assert noisy not in result


def test_backend_state_without_board_reports_loaded_flags(monkeypatch):
    """Backend state must explicitly say when no board/project is loaded."""
    import kicad_interface

    monkeypatch.setattr(kicad_interface.KiCADProcessManager, "is_running", lambda: False)

    iface = _make_iface({}, use_ipc=False)
    iface.command_routes["get_backend_state"] = iface._handle_get_backend_state

    result = iface.handle_command("get_backend_state", {})

    assert result["success"] is True
    assert result["backend"] == "swig"
    assert result["realtime"] is False
    assert result["loadedProject"] is False
    assert result["loadedBoard"] is False
    assert result["projectPath"] is None
    assert result["boardPath"] is None
    assert result["dirty"] is False


def test_backend_state_reports_loaded_project_board_and_clean_signature(tmp_path, monkeypatch):
    """A loaded board with a matching disk signature should be visible and clean."""
    import kicad_interface

    monkeypatch.setattr(kicad_interface.KiCADProcessManager, "is_running", lambda: False)

    board_path = tmp_path / "demo.kicad_pcb"
    project_path = tmp_path / "demo.kicad_pro"
    board_path.write_text("(kicad_pcb demo)\n", encoding="utf-8")
    project_path.write_text("{}\n", encoding="utf-8")

    iface = _make_iface({}, use_ipc=False)
    iface.board = _FakeBoard(board_path)
    iface._current_project_path = tmp_path
    iface._record_board_signature()
    iface.command_routes["get_backend_state"] = iface._handle_get_backend_state

    result = iface.handle_command("get_backend_state", {})

    assert result["loadedProject"] is True
    assert result["loadedBoard"] is True
    assert result["projectPath"] == str(project_path.resolve())
    assert result["boardPath"] == str(board_path.resolve())
    assert result["dirty"] is False
    assert result["diskChangedExternally"] is False


def test_backend_state_reports_disk_divergence_as_dirty(tmp_path, monkeypatch):
    """If the board file changed after load, callers need a loud dirty signal."""
    import kicad_interface

    monkeypatch.setattr(kicad_interface.KiCADProcessManager, "is_running", lambda: False)

    board_path = tmp_path / "demo.kicad_pcb"
    board_path.write_text("(kicad_pcb original)\n", encoding="utf-8")

    iface = _make_iface({}, use_ipc=False)
    iface.board = _FakeBoard(board_path)
    iface._record_board_signature()
    board_path.write_text("(kicad_pcb changed)\n", encoding="utf-8")
    iface.command_routes["get_backend_state"] = iface._handle_get_backend_state

    result = iface.handle_command("get_backend_state", {})

    assert result["loadedBoard"] is True
    assert result["dirty"] is True
    assert result["diskChangedExternally"] is True
    assert "changed on disk" in result["dirtyReason"]


def test_backend_state_reports_unsaved_memory_after_refused_autosave(tmp_path, monkeypatch):
    """Auto-save refusal means the MCP has memory changes that are not persisted."""
    import kicad_interface

    monkeypatch.setattr(kicad_interface.KiCADProcessManager, "is_running", lambda: False)

    board_path = tmp_path / "demo.kicad_pcb"
    board_path.write_text("(kicad_pcb demo)\n", encoding="utf-8")

    iface = _make_iface({}, use_ipc=False)
    iface.board = _FakeBoard(board_path)
    iface._record_board_signature()
    iface._last_auto_save_status = {
        "saved": False,
        "memChangesUnsaved": True,
        "diskChangedExternally": True,
    }
    iface.command_routes["get_backend_state"] = iface._handle_get_backend_state

    result = iface.handle_command("get_backend_state", {})

    assert result["dirty"] is True
    assert result["diskChangedExternally"] is True
    assert "memory changes" in result["dirtyReason"]


def test_backend_state_reports_ipc_connection_without_loaded_board(monkeypatch):
    """Backend state separates IPC connectivity from whether a board is loaded."""
    import kicad_interface

    monkeypatch.setattr(kicad_interface.KiCADProcessManager, "is_running", lambda: False)

    backend = _FakeIPCBackend()
    backend.connected = True
    iface = _make_iface({}, use_ipc=True)
    iface.ipc_backend = backend
    iface.command_routes["get_backend_state"] = iface._handle_get_backend_state

    result = iface.handle_command("get_backend_state", {})

    assert result["backend"] == "ipc"
    assert result["realtime"] is True
    assert result["ipcConnected"] is True
    assert result["loadedBoard"] is False


def test_ipc_capable_command_reconnects_when_kicad_is_running(monkeypatch):
    import kicad_interface

    monkeypatch.setattr(kicad_interface, "KICAD_BACKEND", "auto")
    monkeypatch.setattr(kicad_interface.KiCADProcessManager, "is_running", lambda: True)
    # PCB editor gate: IPC board ops short-circuit when pcbnew isn't a
    # running process. The test simulates a full KiCAD UI, so report True.
    monkeypatch.setattr(kicad_interface.KiCADProcessManager, "is_pcb_editor_running", lambda: True)
    monkeypatch.setitem(
        sys.modules,
        "kicad_api.ipc_backend",
        types.SimpleNamespace(IPCBackend=_FakeIPCBackend),
    )

    iface = _make_iface({}, use_ipc=False)

    result = iface.handle_command("get_board_info", {})

    assert result["success"] is True
    # IPC reconnection should have taken: confirm via the connection state,
    # not via a per-response banner (which we no longer stamp).
    assert iface.use_ipc is True
    assert iface.ipc_board_api is not None


def test_ipc_capable_command_does_not_reconnect_in_strict_swig_mode(monkeypatch):
    import kicad_interface

    monkeypatch.setattr(kicad_interface, "KICAD_BACKEND", "swig")
    monkeypatch.setattr(kicad_interface.KiCADProcessManager, "is_running", lambda: True)
    monkeypatch.setitem(
        sys.modules,
        "kicad_api.ipc_backend",
        types.SimpleNamespace(IPCBackend=_ConnectShouldNotBeCalledIPCBackend),
    )

    iface = _make_iface(
        {
            "get_board_info": lambda params: {
                "success": True,
                "board": {"filename": "demo.kicad_pcb"},
            }
        },
        use_ipc=False,
    )

    result = iface.handle_command("get_board_info", {})

    assert result["success"] is True
    assert iface.ipc_board_api is None


def test_ipc_reconnect_failure_falls_back_to_swig(monkeypatch):
    import kicad_interface

    monkeypatch.setattr(kicad_interface, "KICAD_BACKEND", "auto")
    monkeypatch.setattr(kicad_interface.KiCADProcessManager, "is_running", lambda: True)
    monkeypatch.setitem(
        sys.modules,
        "kicad_api.ipc_backend",
        types.SimpleNamespace(IPCBackend=_FailingConnectIPCBackend),
    )

    iface = _make_iface(
        {
            "get_board_info": lambda params: {
                "success": True,
                "board": {"filename": "demo.kicad_pcb"},
            }
        },
        use_ipc=False,
    )

    result = iface.handle_command("get_board_info", {})

    assert result["success"] is True
    assert iface.use_ipc is False
    assert iface.ipc_board_api is None


def test_connected_ipc_without_board_api_reports_status_but_board_tools_fallback(monkeypatch):
    import kicad_interface

    monkeypatch.setattr(kicad_interface, "KICAD_BACKEND", "auto")
    monkeypatch.setattr(kicad_interface.KiCADProcessManager, "is_running", lambda: True)
    monkeypatch.setitem(
        sys.modules,
        "kicad_api.ipc_backend",
        types.SimpleNamespace(IPCBackend=_NoBoardIPCBackend),
    )

    iface = _make_iface(
        {
            "get_board_info": lambda params: {
                "success": True,
                "board": {"filename": "demo.kicad_pcb"},
            }
        },
        use_ipc=False,
    )
    iface.command_routes["get_backend_info"] = iface._handle_get_backend_info

    board_result = iface.handle_command("get_board_info", {})
    backend_result = iface.handle_command("get_backend_info", {})

    assert board_result["success"] is True
    assert iface.use_ipc is True
    assert iface.ipc_board_api is None

    assert backend_result["success"] is True
    assert backend_result["backend"] == "ipc"
    assert backend_result["realtime_sync"] is True
    assert backend_result["ipc_connected"] is True


def test_ui_status_tools_report_live_ipc_backend_status(monkeypatch):
    import kicad_interface

    monkeypatch.setattr(kicad_interface, "KICAD_BACKEND", "auto")
    monkeypatch.setattr(kicad_interface.KiCADProcessManager, "is_running", lambda self=None: True)
    monkeypatch.setattr(
        kicad_interface.KiCADProcessManager,
        "get_process_info",
        lambda self: [{"pid": "1234", "name": "pcbnew.exe", "command": "pcbnew.exe"}],
    )
    monkeypatch.setitem(
        sys.modules,
        "kicad_api.ipc_backend",
        types.SimpleNamespace(IPCBackend=_FakeIPCBackend),
    )

    iface = _make_iface({}, use_ipc=False)
    iface.ipc_backend = _FakeIPCBackend()
    iface.command_routes = {
        "check_kicad_ui": iface._handle_check_kicad_ui,
        "launch_kicad_ui": iface._handle_launch_kicad_ui,
        "get_backend_info": iface._handle_get_backend_info,
    }
    monkeypatch.setattr(
        kicad_interface,
        "check_and_launch_kicad",
        lambda path_obj, auto_launch: {
            "running": True,
            "launched": True,
            "processes": [{"pid": "1234", "name": "pcbnew.exe", "command": "pcbnew.exe"}],
            "message": "KiCAD launched successfully",
            "project": str(path_obj) if path_obj else None,
        },
    )

    for command in ("check_kicad_ui", "launch_kicad_ui", "get_backend_info"):
        result = iface.handle_command(command, {})

        assert result["success"] is True
        assert result["backend"] == "ipc"
        assert result["realtime_sync"] is True
        assert result["ipc_connected"] is True


def test_query_traces_can_use_ipc_backend(monkeypatch):
    import kicad_interface

    monkeypatch.setattr(kicad_interface, "KICAD_BACKEND", "swig")
    # IPC board ops gate on pcbnew being open; the fake setup simulates that.
    monkeypatch.setattr(kicad_interface.KiCADProcessManager, "is_pcb_editor_running", lambda: True)

    iface = _make_iface({}, use_ipc=True)
    iface.ipc_board_api = _FakeIPCBoardAPI()

    result = iface.handle_command(
        "query_traces",
        {"layer": "F.Cu", "boundingBox": {"x1": -1, "y1": -1, "x2": 1, "y2": 1}},
    )

    assert result["success"] is True
    assert result["traceCount"] == 1
    assert result["traces"][0]["layer"] == "F.Cu"
    assert result["traces"][0]["length"] == 5


def test_query_traces_ipc_filters_and_vias(monkeypatch):
    import kicad_interface

    monkeypatch.setattr(kicad_interface, "KICAD_BACKEND", "swig")
    monkeypatch.setattr(kicad_interface.KiCADProcessManager, "is_pcb_editor_running", lambda: True)

    iface = _make_iface({}, use_ipc=True)
    iface.ipc_board_api = _FilteringIPCBoardAPI()

    net_miss = iface.handle_command("query_traces", {"net": "NO_MATCH"})
    layer_match = iface.handle_command("query_traces", {"layer": "B.Cu"})
    reversed_bbox_with_vias = iface.handle_command(
        "query_traces",
        {
            "net": "N$1",
            "includeVias": True,
            "boundingBox": {"x1": 1, "y1": 1, "x2": -1, "y2": -1},
        },
    )

    assert net_miss["success"] is True
    assert net_miss["traceCount"] == 0

    assert layer_match["success"] is True
    assert layer_match["traceCount"] == 1
    assert layer_match["traces"][0]["uuid"] == "track-2"
    assert layer_match["traces"][0]["layer"] == "B.Cu"

    assert reversed_bbox_with_vias["success"] is True
    assert reversed_bbox_with_vias["traceCount"] == 1
    assert reversed_bbox_with_vias["traces"][0]["uuid"] == "track-1"
    assert reversed_bbox_with_vias["viaCount"] == 1
    assert reversed_bbox_with_vias["vias"][0]["uuid"] == "via-1"


def test_ipc_board_size_supports_kicad_10_box2_pos_size(monkeypatch):
    from kicad_api.ipc_backend import IPCBoardAPI

    _stub_kipy_units(monkeypatch)
    board_api = IPCBoardAPI(None, lambda *_args: None)
    board_api._board = _BoundingBoxBoard(
        [
            _BoxWithPosSize(1_000_000, 2_000_000, 3_000_000, 4_000_000),
            _BoxWithPosSize(0, 1_000_000, 2_000_000, 1_000_000),
        ]
    )

    result = board_api.get_size()

    assert result == {"width": 4.0, "height": 5.0, "unit": "mm"}


def test_ipc_board_size_keeps_min_max_box2_compatibility(monkeypatch):
    from kicad_api.ipc_backend import IPCBoardAPI

    _stub_kipy_units(monkeypatch)
    board_api = IPCBoardAPI(None, lambda *_args: None)
    board_api._board = _BoundingBoxBoard(
        [
            _BoxWithMinMax(1_000_000, 2_000_000, 3_000_000, 4_000_000),
            _BoxWithMinMax(0, 1_000_000, 2_000_000, 3_000_000),
        ]
    )

    result = board_api.get_size()

    assert result == {"width": 3.0, "height": 3.0, "unit": "mm"}


def test_get_backend_info_on_swig_carries_actionable_recommendation(monkeypatch):
    """SWIG-mode get_backend_info must tell the agent exactly what to do,
    not just describe the state.  The previous "requires manual reload"
    text gave no next step; we now point at launch_kicad_ui and list the
    capabilities being given up."""
    import kicad_interface

    monkeypatch.setattr(kicad_interface.KiCADProcessManager, "is_running", lambda: False)

    iface = _make_iface({}, use_ipc=False)
    iface.command_routes["get_backend_info"] = iface._handle_get_backend_info

    result = iface.handle_command("get_backend_info", {})

    assert result["success"] is True
    assert result["backend"] == "swig"
    assert "launch_kicad_ui" in result["message"]
    assert "launch_kicad_ui" in result["recommendation"]
    # The recommendation cites the concrete capabilities lost so the
    # agent can weigh "is this worth a launch" against its current task.
    assert "realtime" in result["recommendation"].lower()
    assert "transactions" in result["recommendation"].lower()
    assert "unavailable_tools" in result["recommendation"]
    # And the unavailable_tools list is actually present.
    assert isinstance(result.get("unavailable_tools"), list)
    assert len(result["unavailable_tools"]) > 0


def test_get_backend_info_on_ipc_has_no_recommendation(monkeypatch):
    """When already on IPC the message should be celebratory, not advisory."""
    import kicad_interface

    monkeypatch.setattr(kicad_interface, "KICAD_BACKEND", "auto")
    monkeypatch.setattr(kicad_interface.KiCADProcessManager, "is_running", lambda: True)
    monkeypatch.setitem(
        sys.modules,
        "kicad_api.ipc_backend",
        types.SimpleNamespace(IPCBackend=_FakeIPCBackend),
    )

    iface = _make_iface({}, use_ipc=False)
    iface.ipc_backend = _FakeIPCBackend()
    iface.command_routes["get_backend_info"] = iface._handle_get_backend_info

    result = iface.handle_command("get_backend_info", {})

    assert result["backend"] == "ipc"
    assert "real-time" in result["message"] or "realtime" in result["message"]
    assert "recommendation" not in result


def test_swig_success_no_longer_carries_recommendation_string():
    """The dispatcher used to inject a long 'On SWIG backend — call
    launch_kicad_ui...' string into every successful SWIG response.  The
    user asked for this to go away (it was noise on every tool call and,
    on schematic / file-only tools that can't use IPC, actively
    misleading)."""
    iface = _make_iface(
        {
            "get_project_info": lambda params: {
                "success": True,
                "project": {"name": "demo"},
            }
        },
        use_ipc=False,
    )

    result = iface.handle_command("get_project_info", {})

    assert "_recommendation" not in result
    assert "_backend" not in result
    assert "_realtime" not in result


def test_create_project_default_auto_launches_kicad_ui(monkeypatch, tmp_path):
    """Default behavior must launch KiCAD so the IPC backend can attach —
    this is the flip the user asked for.  Previously create_project was
    pure file I/O, which left the agent stuck on SWIG by default.

    Critically: we must hand KiCAD the .kicad_pro FILE, not its parent
    directory.  The previous version of this test stubbed
    _project_path_from_filename with an identity lambda and masked the
    fact that the real method returns p.parent — so production passed a
    directory to `kicad <path>` and the project never actually opened.
    This test now uses the real launch-file resolver and asserts on the
    .kicad_pro path.
    """
    from handlers import project as project_handler

    calls: Dict[str, Any] = {}
    project_file = tmp_path / "demo.kicad_pro"
    project_file.write_text("(kicad_project)\n", encoding="utf-8")
    (tmp_path / "demo.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")

    def fake_check_and_launch(project_path, auto_launch=True):
        calls["project_path"] = project_path
        calls["auto_launch"] = auto_launch
        return {
            "running": True,
            "launched": True,
            "processes": [],
            "message": "KiCAD launched successfully",
        }

    monkeypatch.setattr(project_handler, "check_and_launch_kicad", fake_check_and_launch)
    monkeypatch.delenv("KICAD_AUTO_LAUNCH", raising=False)
    monkeypatch.delenv("KICAD_BACKEND", raising=False)

    iface = _make_iface({}, use_ipc=False)
    iface.project_commands = types.SimpleNamespace(
        create_project=lambda params: {
            "success": True,
            "project": {"path": str(project_file)},
        }
    )
    # Use the REAL _project_path_from_filename so we test the production
    # path, not a stub.  It returns the parent dir — which is fine for
    # symbol library refresh, but the launcher must still get the file.
    from kicad_interface import KiCADInterface

    iface._project_path_from_filename = KiCADInterface._project_path_from_filename.__get__(iface)
    iface._refresh_symbol_library_for_project = lambda p: None
    iface._try_enable_ipc_backend = lambda force=False: True
    # Make the mismatch check pass — IPC reports the expected board.
    iface._current_board_path = lambda: str(tmp_path / "demo.kicad_pcb")

    result = project_handler.handle_create_project(iface, {"path": str(tmp_path), "name": "demo"})

    assert result["success"] is True
    assert calls["auto_launch"] is True
    # The .kicad_pro FILE must be passed to KiCAD, not the parent dir.
    assert (
        calls["project_path"] == project_file
    ), f"KiCAD should be launched with the .kicad_pro file, got {calls['project_path']}"
    assert result["kicadUi"]["attempted"] is True
    assert result["kicadUi"]["launched"] is True
    assert result["kicadUi"]["running"] is True
    assert result["kicadUi"]["ipcAttached"] is True
    assert result["kicadUi"]["projectMismatch"] is None


def test_create_project_honors_explicit_auto_launch_false(monkeypatch, tmp_path):
    """The opt-out must work without env vars — pure param control."""
    import kicad_interface
    from handlers import project as project_handler

    monkeypatch.setattr(
        project_handler,
        "check_and_launch_kicad",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not launch")),
    )
    monkeypatch.delenv("KICAD_AUTO_LAUNCH", raising=False)
    monkeypatch.delenv("KICAD_BACKEND", raising=False)

    iface = _make_iface({}, use_ipc=False)
    iface.project_commands = types.SimpleNamespace(
        create_project=lambda params: {
            "success": True,
            "project": {"path": str(tmp_path / "demo.kicad_pro")},
        }
    )
    iface._project_path_from_filename = lambda p: Path(p) if p else None
    iface._refresh_symbol_library_for_project = lambda p: None

    result = project_handler.handle_create_project(
        iface, {"path": str(tmp_path), "name": "demo", "autoLaunch": False}
    )

    assert result["success"] is True
    assert result["kicadUi"]["skipped"] is True
    assert "autoLaunch=false" in result["kicadUi"]["reason"]


def test_create_project_honors_env_opt_out(monkeypatch, tmp_path):
    """Operators who set KICAD_AUTO_LAUNCH=false should not get launches."""
    import kicad_interface
    from handlers import project as project_handler

    monkeypatch.setattr(
        project_handler,
        "check_and_launch_kicad",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not launch")),
    )
    monkeypatch.setenv("KICAD_AUTO_LAUNCH", "false")
    monkeypatch.delenv("KICAD_BACKEND", raising=False)

    iface = _make_iface({}, use_ipc=False)
    iface.project_commands = types.SimpleNamespace(
        create_project=lambda params: {
            "success": True,
            "project": {"path": str(tmp_path / "demo.kicad_pro")},
        }
    )
    iface._project_path_from_filename = lambda p: Path(p) if p else None
    iface._refresh_symbol_library_for_project = lambda p: None

    result = project_handler.handle_create_project(iface, {"path": str(tmp_path), "name": "demo"})

    assert result["kicadUi"]["skipped"] is True
    assert "KICAD_AUTO_LAUNCH" in result["kicadUi"]["reason"]


def test_open_project_launches_with_kicad_pro_file_not_parent_dir(monkeypatch, tmp_path):
    """Regression for the directory-vs-file bug found in code review.

    open_project passes the value from the result/params through
    _project_path_from_filename (which returns the PARENT directory)
    for symbol-library refresh, but the auto-launcher needs the
    .kicad_pro FILE — otherwise KiCAD opens to a directory and the
    project never actually loads in the UI.
    """
    from handlers import project as project_handler

    calls: Dict[str, Any] = {}
    project_file = tmp_path / "demo.kicad_pro"
    project_file.write_text("(kicad_project)\n", encoding="utf-8")
    (tmp_path / "demo.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")

    def fake_check_and_launch(project_path, auto_launch=True):
        calls["project_path"] = project_path
        return {
            "running": True,
            "launched": True,
            "processes": [],
            "message": "KiCAD launched successfully",
        }

    monkeypatch.setattr(project_handler, "check_and_launch_kicad", fake_check_and_launch)
    monkeypatch.delenv("KICAD_AUTO_LAUNCH", raising=False)
    monkeypatch.delenv("KICAD_BACKEND", raising=False)

    iface = _make_iface({}, use_ipc=False)
    iface.project_commands = types.SimpleNamespace(
        open_project=lambda params: {
            "success": True,
            "project": {"path": str(project_file)},
        }
    )
    from kicad_interface import KiCADInterface

    iface._project_path_from_filename = KiCADInterface._project_path_from_filename.__get__(iface)
    iface._refresh_symbol_library_for_project = lambda p: None
    iface._try_enable_ipc_backend = lambda force=False: False  # IPC attach fails, fine.

    result = project_handler.handle_open_project(iface, {"filename": str(project_file)})

    assert result["success"] is True
    assert (
        calls["project_path"] == project_file
    ), f"open_project should launch with the .kicad_pro file, got {calls['project_path']}"


def test_autolaunch_detects_cross_project_ipc_mismatch_and_disengages(monkeypatch, tmp_path):
    """If KiCAD already has a DIFFERENT project open, IPC attaching to it
    would silently route this session's mutations to the wrong board.
    The auto-launch must detect the mismatch, tear the IPC attach back
    down, and surface a loud warning rather than corrupt user data."""
    from handlers import project as project_handler

    project_file = tmp_path / "demoB.kicad_pro"
    project_file.write_text("(kicad_project)\n", encoding="utf-8")
    other_pcb = tmp_path / "demoA.kicad_pcb"
    other_pcb.write_text("(kicad_pcb)\n", encoding="utf-8")

    monkeypatch.setattr(
        project_handler,
        "check_and_launch_kicad",
        lambda *a, **k: {
            "running": True,
            "launched": False,  # already running
            "processes": [],
            "message": "KiCAD is already running",
        },
    )
    monkeypatch.delenv("KICAD_AUTO_LAUNCH", raising=False)
    monkeypatch.delenv("KICAD_BACKEND", raising=False)

    iface = _make_iface({}, use_ipc=False)
    iface.project_commands = types.SimpleNamespace(
        open_project=lambda params: {
            "success": True,
            "project": {"path": str(project_file)},
        }
    )
    from kicad_interface import KiCADInterface

    iface._project_path_from_filename = KiCADInterface._project_path_from_filename.__get__(iface)
    iface._refresh_symbol_library_for_project = lambda p: None
    # Simulate a successful IPC attach...
    attach_state = {"ipc_attached": False}

    def fake_attach(force=False):
        iface.use_ipc = True
        iface.ipc_board_api = object()
        attach_state["ipc_attached"] = True
        return True

    iface._try_enable_ipc_backend = fake_attach
    # ...but IPC reports KiCAD has demoA open, not demoB.
    iface._current_board_path = lambda: str(other_pcb)

    result = project_handler.handle_open_project(iface, {"filename": str(project_file)})

    assert result["success"] is True
    ui = result["kicadUi"]
    # IPC must be disengaged so the dispatcher falls back to SWIG.
    assert ui["ipcAttached"] is False
    assert iface.use_ipc is False
    assert iface.ipc_board_api is None
    assert ui["projectMismatch"] is not None
    assert ui["projectMismatch"]["ipcBoardPath"] == str(other_pcb)
    assert "demoB.kicad_pcb" in ui["projectMismatch"]["expectedBoardPath"]
    assert ui["warning"] and "DIFFERENT project" in ui["warning"]


def test_create_project_skips_launch_on_failed_create(monkeypatch, tmp_path):
    """If project creation failed, don't launch the UI to a non-existent project."""
    import kicad_interface
    from handlers import project as project_handler

    monkeypatch.setattr(
        project_handler,
        "check_and_launch_kicad",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not launch")),
    )

    iface = _make_iface({}, use_ipc=False)
    iface.project_commands = types.SimpleNamespace(
        create_project=lambda params: {"success": False, "message": "bad path"}
    )
    iface._project_path_from_filename = lambda p: Path(p) if p else None
    iface._refresh_symbol_library_for_project = lambda p: None

    result = project_handler.handle_create_project(iface, {"path": str(tmp_path), "name": "demo"})

    assert result["success"] is False
    assert "kicadUi" not in result
