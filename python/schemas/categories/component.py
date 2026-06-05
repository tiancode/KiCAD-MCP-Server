"""Component tool schema definitions for KiCAD MCP Server.

Following the MCP 2025-06-18 specification for tool definitions.
Split out of the former monolithic schemas/tool_schemas.py.
"""

COMPONENT_TOOLS = [
    {
        "name": "place_component",
        "title": "Place Component",
        "description": "Places a component with specified footprint at given coordinates on the board.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "Component reference designator (e.g., R1, C2, U3)",
                },
                "footprint": {
                    "type": "string",
                    "description": "Footprint library:name (e.g., Resistor_SMD:R_0805_2012Metric)",
                },
                "x": {"type": "number", "description": "X coordinate in millimeters"},
                "y": {"type": "number", "description": "Y coordinate in millimeters"},
                "rotation": {
                    "type": "number",
                    "description": "Rotation angle in degrees (0-360)",
                    "minimum": 0,
                    "maximum": 360,
                    "default": 0,
                },
                "layer": {
                    "type": "string",
                    "enum": ["F.Cu", "B.Cu"],
                    "description": "Board layer (top or bottom)",
                    "default": "F.Cu",
                },
            },
            "required": ["reference", "footprint", "x", "y"],
        },
    },
    {
        "name": "move_component",
        "title": "Move Component",
        "description": "Moves an existing component to a new position on the board.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "Component reference designator",
                },
                "x": {
                    "type": "number",
                    "description": "New X coordinate in millimeters",
                },
                "y": {
                    "type": "number",
                    "description": "New Y coordinate in millimeters",
                },
            },
            "required": ["reference", "x", "y"],
        },
    },
    {
        "name": "rotate_component",
        "title": "Rotate Component",
        "description": "Rotates a component by specified angle. Rotation is cumulative with existing rotation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "Component reference designator",
                },
                "angle": {
                    "type": "number",
                    "description": "Rotation angle in degrees (positive = counterclockwise)",
                },
            },
            "required": ["reference", "angle"],
        },
    },
    {
        "name": "delete_component",
        "title": "Delete Component",
        "description": "Removes a component from the board.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "Component reference designator",
                }
            },
            "required": ["reference"],
        },
    },
    {
        "name": "edit_component",
        "title": "Edit Component Properties",
        "description": "Modifies properties of an existing component (value, footprint, etc.).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "Component reference designator",
                },
                "value": {"type": "string", "description": "New component value"},
                "footprint": {
                    "type": "string",
                    "description": "New footprint library:name",
                },
            },
            "required": ["reference"],
        },
    },
    {
        "name": "get_component_properties",
        "title": "Get Component Properties",
        "description": "Retrieves detailed properties of a specific component.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "Component reference designator",
                }
            },
            "required": ["reference"],
        },
    },
    {
        "name": "get_component_list",
        "title": "List All Components",
        "description": "Returns a list of all components on the board with their properties.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "find_component",
        "title": "Find Components",
        "description": "Searches for components matching specified criteria. Supports partial matching on reference, value, or footprint patterns.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "Reference designator pattern to match (e.g., 'R1', 'U', 'C2')",
                },
                "value": {
                    "type": "string",
                    "description": "Value pattern to match (e.g., '10k', '100nF')",
                },
                "footprint": {
                    "type": "string",
                    "description": "Footprint pattern to match (e.g., '0805', 'SOIC')",
                },
            },
        },
    },
    {
        "name": "get_component_pads",
        "title": "Get Component Pads",
        "description": "Returns all pads for a component with their positions, net connections, sizes, and shapes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "Component reference designator (e.g., U1, R5)",
                }
            },
            "required": ["reference"],
        },
    },
    {
        "name": "get_pad_position",
        "title": "Get Pad Position",
        "description": "Returns the position and properties of a specific pad on a component.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "Component reference designator",
                },
                "padName": {
                    "type": "string",
                    "description": "Pad name or number (e.g., '1', '2', 'A1')",
                },
                "padNumber": {
                    "type": "string",
                    "description": "Alternative to padName - pad number",
                },
            },
            "required": ["reference"],
        },
    },
    {
        "name": "place_component_array",
        "title": "Place Component Array",
        "description": "Places multiple copies of a component in a grid or circular pattern.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "referencePrefix": {
                    "type": "string",
                    "description": "Reference prefix (e.g., 'R' for R1, R2, R3...)",
                },
                "startNumber": {
                    "type": "integer",
                    "description": "Starting number for references",
                    "minimum": 1,
                    "default": 1,
                },
                "footprint": {
                    "type": "string",
                    "description": "Footprint library:name",
                },
                "pattern": {
                    "type": "string",
                    "enum": ["grid", "circular"],
                    "description": "Array pattern type",
                },
                "count": {
                    "type": "integer",
                    "description": "Total number of components to place",
                    "minimum": 1,
                },
                "startX": {
                    "type": "number",
                    "description": "Starting X coordinate in millimeters",
                },
                "startY": {
                    "type": "number",
                    "description": "Starting Y coordinate in millimeters",
                },
                "spacingX": {
                    "type": "number",
                    "description": "Horizontal spacing in mm (for grid pattern)",
                },
                "spacingY": {
                    "type": "number",
                    "description": "Vertical spacing in mm (for grid pattern)",
                },
                "radius": {
                    "type": "number",
                    "description": "Circle radius in mm (for circular pattern)",
                },
                "rows": {
                    "type": "integer",
                    "description": "Number of rows (for grid pattern)",
                    "minimum": 1,
                },
                "columns": {
                    "type": "integer",
                    "description": "Number of columns (for grid pattern)",
                    "minimum": 1,
                },
            },
            "required": [
                "referencePrefix",
                "footprint",
                "pattern",
                "count",
                "startX",
                "startY",
            ],
        },
    },
    {
        "name": "align_components",
        "title": "Align Components",
        "description": "Aligns multiple components horizontally or vertically.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "references": {
                    "type": "array",
                    "description": "Array of component reference designators to align",
                    "items": {"type": "string"},
                    "minItems": 2,
                },
                "direction": {
                    "type": "string",
                    "enum": ["horizontal", "vertical"],
                    "description": "Alignment direction",
                },
                "spacing": {
                    "type": "number",
                    "description": "Spacing between components in mm (optional, for even distribution)",
                },
            },
            "required": ["references", "direction"],
        },
    },
    {
        "name": "check_courtyard_overlaps",
        "title": "Check Courtyard Overlaps",
        "description": (
            "Detects courtyard overlaps between footprints and (optionally) flags "
            "footprints whose courtyard extends past the board outline. "
            "Returns overlap pairs with intersection extents and per-component "
            "boundary violations, both in mm. Accepts a 'positions' dict to "
            "evaluate a HYPOTHETICAL placement without modifying the board — "
            "use this before committing a move_component / place_component call "
            "to know if it will trigger DRC. "
            "Approach ported from morningfire-pcb-automation "
            "(https://github.com/NiNjA-CodE/morningfire-pcb-automation, "
            "scripts/placement/check_overlaps.py); this version reads real "
            "courtyard polygons from the board (not a static lookup table) and "
            "supports virtual placement + rotation + clearance margin."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "positions": {
                    "type": "object",
                    "description": (
                        "Virtual placements: map of reference designator to "
                        "[x, y] or [x, y, rotation_degrees] in mm. Each listed "
                        "ref is checked AS IF it were at the given coordinates. "
                        "Unspecified refs use their current board position."
                    ),
                    "additionalProperties": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 2,
                        "maxItems": 3,
                    },
                },
                "refs": {
                    "type": "array",
                    "description": (
                        "Limit the check to these refs (default: every " "footprint on the board)."
                    ),
                    "items": {"type": "string"},
                },
                "margin": {
                    "type": "number",
                    "description": (
                        "Extra clearance in mm added around every courtyard "
                        "(default 0). Useful to enforce a manufacturing keepout "
                        "wider than the symbol's declared courtyard."
                    ),
                    "default": 0,
                },
                "include_boundary": {
                    "type": "boolean",
                    "description": (
                        "Also flag courtyards that extend past the board outline " "(default true)."
                    ),
                    "default": True,
                },
                "board_outline": {
                    "type": "object",
                    "description": (
                        "Optional override for the board outline bbox. Default: "
                        "derived from Edge.Cuts."
                    ),
                    "properties": {
                        "x1": {"type": "number"},
                        "y1": {"type": "number"},
                        "x2": {"type": "number"},
                        "y2": {"type": "number"},
                        "unit": {
                            "type": "string",
                            "enum": ["mm", "mil", "inch"],
                            "default": "mm",
                        },
                    },
                    "required": ["x1", "y1", "x2", "y2"],
                },
            },
        },
    },
    {
        "name": "duplicate_component",
        "title": "Duplicate Component",
        "description": "Creates a copy of an existing component with new reference designator.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sourceReference": {
                    "type": "string",
                    "description": "Reference of component to duplicate",
                },
                "newReference": {
                    "type": "string",
                    "description": "Reference designator for the new component",
                },
                "offsetX": {
                    "type": "number",
                    "description": "X offset from original position in mm",
                    "default": 0,
                },
                "offsetY": {
                    "type": "number",
                    "description": "Y offset from original position in mm",
                    "default": 0,
                },
            },
            "required": ["sourceReference", "newReference"],
        },
    },
]
