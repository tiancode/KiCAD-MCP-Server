"""Project tool schema definitions for KiCAD MCP Server.

Following the MCP 2025-06-18 specification for tool definitions.
Split out of the former monolithic schemas/tool_schemas.py.
"""

PROJECT_TOOLS = [
    {
        "name": "create_project",
        "title": "Create New KiCAD Project",
        "description": "Creates a new KiCAD project with PCB board file and optional project configuration. Automatically creates project directory and initializes board with default settings.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "projectName": {
                    "type": "string",
                    "description": "Name of the project (used for file naming)",
                    "minLength": 1,
                },
                "path": {
                    "type": "string",
                    "description": "Directory path where project will be created (defaults to current working directory)",
                },
                "template": {
                    "type": "string",
                    "description": "Optional path to template board file to copy settings from",
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "Replace an existing project. Defaults to false: if the target .kicad_pro/.kicad_pcb/.kicad_sch already exist the tool refuses (errorCode PROJECT_EXISTS) instead of clobbering them.",
                },
            },
            "required": ["projectName"],
        },
    },
    {
        "name": "open_project",
        "title": "Open Existing KiCAD Project",
        "description": "Opens an existing KiCAD project file (.kicad_pro or .kicad_pcb) and loads the board into memory for manipulation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Path to .kicad_pro or .kicad_pcb file",
                }
            },
            "required": ["filename"],
        },
    },
    {
        "name": "save_project",
        "title": "Save Current Project",
        "description": "Saves the current board to disk. Can optionally save to a new location.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Optional new path to save the board (if not provided, saves to current location)",
                }
            },
        },
    },
    {
        "name": "snapshot_project",
        "title": "Snapshot Project (Checkpoint)",
        "description": "Copies the entire project folder to a new timestamped snapshot directory so you can resume from this checkpoint later without redoing earlier steps. Call this after every successfully completed design step (e.g. after Step 1 schematic, after Step 2 PCB layout) before asking for user confirmation to proceed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "step": {
                    "type": "string",
                    "description": "Step number or name to include in snapshot folder name, e.g. '1' or '2'",
                },
                "label": {
                    "type": "string",
                    "description": "Optional short label, e.g. 'schematic_ok' or 'layout_ok'",
                },
                "projectPath": {
                    "type": "string",
                    "description": "Project directory path. Auto-detected from loaded board if omitted.",
                },
            },
        },
    },
    {
        "name": "get_project_info",
        "title": "Get Project Information",
        "description": "Retrieves metadata and properties of the currently open project including name, paths, and board status.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]
