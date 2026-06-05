"""Design rule tool schema definitions for KiCAD MCP Server.

Following the MCP 2025-06-18 specification for tool definitions.
Split out of the former monolithic schemas/tool_schemas.py.
"""

DESIGN_RULE_TOOLS = [
    {
        "name": "set_design_rules",
        "title": "Set Design Rules",
        "description": "Configures board design rules including clearances, trace widths, and via sizes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "clearance": {
                    "type": "number",
                    "description": "Minimum clearance between copper in millimeters",
                    "minimum": 0.1,
                },
                "trackWidth": {
                    "type": "number",
                    "description": "Minimum track width in millimeters",
                    "minimum": 0.1,
                },
                "viaDiameter": {
                    "type": "number",
                    "description": "Minimum via diameter in millimeters",
                },
                "viaDrill": {
                    "type": "number",
                    "description": "Minimum via drill diameter in millimeters",
                },
                "microViaD iameter": {
                    "type": "number",
                    "description": "Minimum micro-via diameter in millimeters",
                },
            },
        },
    },
    {
        "name": "get_design_rules",
        "title": "Get Current Design Rules",
        "description": "Retrieves the currently configured design rules from the board.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "run_drc",
        "title": "Run Design Rule Check",
        "description": "Executes a design rule check (DRC) on the current board and reports violations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "includeWarnings": {
                    "type": "boolean",
                    "description": "Include warnings in addition to errors",
                    "default": True,
                }
            },
        },
    },
    {
        "name": "get_drc_violations",
        "title": "Get DRC Violations",
        "description": "Returns a list of design rule violations from the most recent DRC run.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]
