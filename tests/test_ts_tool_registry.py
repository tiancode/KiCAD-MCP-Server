"""
Regression test: no MCP tool name is registered more than once across all
TypeScript tool files in src/tools/.

This caught a real bug where move_schematic_component was registered twice
(once in the original code and once in the PR adding wire-preservation),
causing the server to fail on startup with:
  Error: Tool move_schematic_component is already registered
"""

import re
from collections import Counter
from pathlib import Path

import pytest

SRC_TOOLS_DIR = Path(__file__).parent.parent / "src" / "tools"

# Pattern matches the tool-name argument to server.tool(
#   server.tool(
#     "some_tool_name",
_SERVER_TOOL_RE = re.compile(r'server\.tool\(\s*["\']([a-zA-Z0-9_]+)["\']')


@pytest.mark.unit
class TestTsToolRegistry:
    def _collect_registrations(self):
        """Return list of (tool_name, file, line_no) for every server.tool() call."""
        registrations = []
        for ts_file in sorted(SRC_TOOLS_DIR.glob("**/*.ts")):
            text = ts_file.read_text(encoding="utf-8")
            for m in _SERVER_TOOL_RE.finditer(text):
                line_no = text[: m.start()].count("\n") + 1
                registrations.append((m.group(1), ts_file.name, line_no))
        return registrations

    def test_no_duplicate_tool_names(self):
        """Every tool name must appear exactly once across all TS tool files."""
        registrations = self._collect_registrations()
        assert registrations, "No server.tool() calls found — check SRC_TOOLS_DIR path"

        counts = Counter(name for name, _, _ in registrations)
        duplicates = {name: count for name, count in counts.items() if count > 1}

        if duplicates:
            details = []
            for dup_name in sorted(duplicates):
                locations = [
                    f"  {fname}:{line}" for name, fname, line in registrations if name == dup_name
                ]
                details.append(f"{dup_name} ({duplicates[dup_name]}x):\n" + "\n".join(locations))
            pytest.fail(
                "Duplicate MCP tool registrations found — server will fail to start:\n\n"
                + "\n\n".join(details)
            )

    def test_tool_files_exist(self):
        """Sanity check: src/tools/ directory must be present and contain TS files."""
        assert SRC_TOOLS_DIR.is_dir(), f"src/tools/ not found at {SRC_TOOLS_DIR}"
        ts_files = list(SRC_TOOLS_DIR.glob("**/*.ts"))
        assert ts_files, "No .ts files found in src/tools/"

    def test_backend_state_tool_is_registered(self):
        """Backend observability is exposed via get_backend_info; the old
        get_backend_state duplicate was removed in the tool-redundancy
        cleanup (2026-06) and must stay removed."""
        registrations = self._collect_registrations()
        tool_names = {name for name, _, _ in registrations}

        assert "get_backend_info" in tool_names
        assert "get_backend_state" not in tool_names

    def test_duplicate_schematic_component_is_registered(self):
        """S13: the new duplicate_schematic_component tool must be registered."""
        registrations = self._collect_registrations()
        tool_names = {name for name, _, _ in registrations}
        assert "duplicate_schematic_component" in tool_names

    def test_schematic_point_tools_accept_both_shapes(self):
        """S12: schematic tools that take a point must use the shared
        xyPointSchema (which accepts BOTH {x, y} and [x, y]) rather than a
        bare object/array, so both coordinate forms validate everywhere."""
        schematic_dir = SRC_TOOLS_DIR / "schematic"
        for fname in ("component.ts", "wire.ts", "view.ts"):
            text = (schematic_dir / fname).read_text(encoding="utf-8")
            assert "xyPointSchema" in text, f"{fname} should use xyPointSchema for points (S12)"

    def test_redundant_tools_stay_removed(self):
        """Tool-redundancy cleanup (2026-06; python routes removed 2026-07):
        these duplicated higher-level tools and were removed from the MCP
        surface, and the unreachable python routes/implementations were
        removed in the 2026-07 follow-up (except the ipc_* handlers, which
        scripts/ipc_smoke_test.py still drives, and get_drc_violations'
        method, which the drc_violations resource consumes). The TS layer
        must not re-register any of them — each has a canonical replacement:

          export_svg            -> get_board_2d_view(format=svg)
          export_schematic_svg  -> get_schematic_view(format=svg)
          get_drc_violations    -> run_drc (returns summary + violations file)
          get_backend_state     -> get_backend_info
          ipc_add_track         -> route_trace (auto IPC fast-path)
          export_dsn/import_ses -> autoroute (runs both internally)
        """
        registrations = self._collect_registrations()
        tool_names = {name for name, _, _ in registrations}

        removed = {
            "export_svg",
            "export_schematic_svg",
            "get_drc_violations",
            "get_backend_state",
            "ipc_add_track",
            "export_dsn",
            "import_ses",
            "add_zone",
            "ipc_add_via",
            "ipc_add_text",
            "ipc_list_components",
            "ipc_get_tracks",
            "ipc_get_vias",
            "ipc_save_board",
        }
        leaked = removed & tool_names
        assert not leaked, f"Removed tools re-registered in TS: {sorted(leaked)}"
