"""UI tool schema definitions for KiCAD MCP Server.

Following the MCP 2025-06-18 specification for tool definitions.
Split out of the former monolithic schemas/tool_schemas.py.
"""

UI_TOOLS = [
    {
        "name": "get_backend_state",
        "title": "Get Backend State",
        "description": ("Returns backend, realtime, loaded project/board paths, and dirty state."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "check_kicad_ui",
        "title": "Check KiCAD UI Status",
        "description": "Checks if KiCAD user interface is currently running and returns process information.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "launch_kicad_ui",
        "title": "Launch KiCAD Application",
        "description": "Opens the KiCAD graphical user interface, optionally with a specific project loaded.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "projectPath": {
                    "type": "string",
                    "description": "Optional path to project file to open in UI",
                },
                "autoLaunch": {
                    "type": "boolean",
                    "description": "Whether to automatically launch if not running",
                    "default": True,
                },
            },
        },
    },
]
