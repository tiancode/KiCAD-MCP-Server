"""Library tool schema definitions for KiCAD MCP Server.

Following the MCP 2025-06-18 specification for tool definitions.
Split out of the former monolithic schemas/tool_schemas.py.
"""

LIBRARY_TOOLS = [
    {
        "name": "list_libraries",
        "title": "List Footprint Libraries",
        "description": "Lists all available footprint libraries accessible to KiCAD.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_footprints",
        "title": "Search Footprints",
        "description": "Searches for footprints matching a query string across all libraries.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query (e.g., '0805', 'SOIC', 'QFP')",
                    "minLength": 1,
                },
                "library": {
                    "type": "string",
                    "description": "Optional library to restrict search to",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_library_footprints",
        "title": "List Footprints in Library",
        "description": "Lists all footprints available in a specific library.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "library": {
                    "type": "string",
                    "description": "Library name (e.g., Resistor_SMD, Connector_PinHeader)",
                    "minLength": 1,
                }
            },
            "required": ["library"],
        },
    },
    {
        "name": "get_footprint_info",
        "title": "Get Footprint Details",
        "description": "Retrieves detailed information about a specific footprint including pad layout, dimensions, and description.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "library": {"type": "string", "description": "Library name"},
                "footprint": {"type": "string", "description": "Footprint name"},
            },
            "required": ["library", "footprint"],
        },
    },
]
