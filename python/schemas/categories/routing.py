"""Routing tool schema definitions for KiCAD MCP Server.

Following the MCP 2025-06-18 specification for tool definitions.
Split out of the former monolithic schemas/tool_schemas.py.
"""

ROUTING_TOOLS = [
    {
        "name": "add_net",
        "title": "Create Electrical Net",
        "description": "Creates a new electrical net for signal routing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "netName": {
                    "type": "string",
                    "description": "Name of the net (e.g., VCC, GND, SDA)",
                    "minLength": 1,
                },
                "netClass": {
                    "type": "string",
                    "description": "Optional net class to assign (must exist first)",
                },
            },
            "required": ["netName"],
        },
    },
    {
        "name": "route_trace",
        "title": "Route PCB Trace",
        "description": "Routes a copper trace between two points or pads on a specified layer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "netName": {"type": "string", "description": "Net name for this trace"},
                "layer": {
                    "type": "string",
                    "description": "Layer to route on (e.g., F.Cu, B.Cu)",
                    "default": "F.Cu",
                },
                "width": {
                    "type": "number",
                    "description": "Trace width in millimeters",
                    "minimum": 0.1,
                },
                "points": {
                    "type": "array",
                    "description": "Array of [x, y] waypoints in millimeters",
                    "items": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    "minItems": 2,
                },
            },
            "required": ["points", "width"],
        },
    },
    {
        "name": "add_via",
        "title": "Add Via",
        "description": "Adds a via (plated through-hole) to connect traces between layers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "number", "description": "X coordinate in millimeters"},
                "y": {"type": "number", "description": "Y coordinate in millimeters"},
                "diameter": {
                    "type": "number",
                    "description": "Via diameter in millimeters",
                    "minimum": 0.1,
                },
                "drill": {
                    "type": "number",
                    "description": "Drill diameter in millimeters",
                    "minimum": 0.1,
                },
                "netName": {
                    "type": "string",
                    "description": "Net name to assign to this via",
                },
            },
            "required": ["x", "y", "diameter", "drill"],
        },
    },
    {
        "name": "delete_trace",
        "title": "Delete Trace",
        "description": "Removes traces from the board. Can delete by UUID, position, or bulk-delete all traces on a net.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "uuid": {
                    "type": "string",
                    "description": "UUID of a specific trace to delete",
                },
                "position": {
                    "type": "object",
                    "description": "Delete trace nearest to this position",
                    "properties": {
                        "x": {"type": "number", "description": "X coordinate"},
                        "y": {"type": "number", "description": "Y coordinate"},
                        "unit": {
                            "type": "string",
                            "enum": ["mm", "mil", "inch"],
                            "default": "mm",
                        },
                    },
                    "required": ["x", "y"],
                },
                "net": {
                    "type": "string",
                    "description": "Delete all traces on this net (bulk delete)",
                },
                "layer": {
                    "type": "string",
                    "description": "Filter by layer when using net-based deletion",
                },
                "includeVias": {
                    "type": "boolean",
                    "description": "Include vias in net-based deletion",
                    "default": False,
                },
            },
        },
    },
    {
        "name": "query_traces",
        "title": "Query Traces",
        "description": "Queries traces on the board with optional filters by net, layer, or bounding box. Returns trace details including UUID, positions, width, and length.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "net": {
                    "type": "string",
                    "description": "Filter by net name (e.g., 'GND', 'VCC')",
                },
                "layer": {
                    "type": "string",
                    "description": "Filter by layer name (e.g., 'F.Cu', 'B.Cu')",
                },
                "boundingBox": {
                    "type": "object",
                    "description": "Filter by bounding box region",
                    "properties": {
                        "x1": {"type": "number", "description": "Left X coordinate"},
                        "y1": {"type": "number", "description": "Top Y coordinate"},
                        "x2": {"type": "number", "description": "Right X coordinate"},
                        "y2": {"type": "number", "description": "Bottom Y coordinate"},
                        "unit": {
                            "type": "string",
                            "enum": ["mm", "mil", "inch"],
                            "default": "mm",
                        },
                    },
                },
                "includeVias": {
                    "type": "boolean",
                    "description": "Include vias in the result",
                    "default": False,
                },
            },
        },
    },
    {
        "name": "query_zones",
        "title": "Query Zones",
        "description": "Queries copper zones (filled pours) on the board with optional filters by net, layer, or bounding box. Returns one entry per zone with net, layers, priority, fill state, and bounding box. Useful for auditing power planes and GND pours, which 'query_traces' does not include.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "net": {
                    "type": "string",
                    "description": "Filter by net name (e.g., 'GND', '+3V3')",
                },
                "layer": {
                    "type": "string",
                    "description": "Filter by layer name (e.g., 'In1.Cu', 'B.Cu'). Matches zones that include this layer in their layer set.",
                },
                "boundingBox": {
                    "type": "object",
                    "description": "Filter to zones whose bounding box overlaps this region",
                    "properties": {
                        "x1": {"type": "number", "description": "Left X coordinate"},
                        "y1": {"type": "number", "description": "Top Y coordinate"},
                        "x2": {"type": "number", "description": "Right X coordinate"},
                        "y2": {"type": "number", "description": "Bottom Y coordinate"},
                        "unit": {
                            "type": "string",
                            "enum": ["mm", "mil", "inch"],
                            "default": "mm",
                        },
                    },
                },
            },
        },
    },
    {
        "name": "add_gnd_stitching_vias",
        "title": "Add GND Stitching Vias",
        "description": (
            "Drop GND stitching vias across the board with collision "
            "checking against every non-GND segment, via, and pad on "
            "every copper layer (PTH vias penetrate the full stackup, "
            "so missing one layer is the classic silent-short failure "
            "mode that other GND-stitching tools have). Combines three "
            "strategies: a regular `grid` across the interior, "
            "`around_refs` (densify around named ICs like an MCU or "
            "switching regulator), and `in_zones` (only place vias "
            "where they actually land on a GND copper zone so they "
            "stitch real polygons together rather than floating on "
            "silkscreen). Supports `dryRun` to preview placements "
            "without writing to the board. "
            "Approach ported from morningfire-pcb-automation "
            "(https://github.com/NiNjA-CodE/morningfire-pcb-automation, "
            "scripts/ground/add_gnd_vias.py); this version reads "
            "obstacles via the pcbnew API (handles rotation, picks up "
            "net classes, integrates with the live in-memory board) "
            "and adds the in-zones strategy + maxVias cap + dry-run."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "gndNet": {
                    "type": "string",
                    "description": (
                        "Name of the ground net (default: auto-detect "
                        "GND / GROUND / VSS / /GND)."
                    ),
                },
                "strategies": {
                    "type": "array",
                    "description": (
                        "Which placement strategies to combine (default: "
                        "['grid']). Pass ['grid', 'around_refs', "
                        "'in_zones'] for full coverage."
                    ),
                    "items": {
                        "type": "string",
                        "enum": ["grid", "around_refs", "in_zones"],
                    },
                },
                "viaSize": {
                    "type": "number",
                    "description": "Via pad diameter in mm (default 0.6).",
                    "default": 0.6,
                },
                "viaDrill": {
                    "type": "number",
                    "description": (
                        "Via drill diameter in mm (default 0.3). " "Must be smaller than viaSize."
                    ),
                    "default": 0.3,
                },
                "clearance": {
                    "type": "number",
                    "description": (
                        "Extra clearance beyond required between each new "
                        "via and existing copper, in mm. Default 0.2."
                    ),
                    "default": 0.2,
                },
                "spacing": {
                    "type": "number",
                    "description": (
                        "Grid spacing in mm for the `grid` and "
                        "`around_refs` strategies. Default 5.0."
                    ),
                    "default": 5.0,
                },
                "densifyRefs": {
                    "type": "array",
                    "description": (
                        "Reference designators to densify ground around "
                        "(used by `around_refs` strategy). Good targets: "
                        "MCUs, switching regulators, RF parts."
                    ),
                    "items": {"type": "string"},
                },
                "densifyRadius": {
                    "type": "integer",
                    "description": (
                        "How many grid cells around each ref to try "
                        "(default 2 = 5x5 candidate field per ref)."
                    ),
                    "default": 2,
                },
                "edgeMargin": {
                    "type": "number",
                    "description": ("Keep-out from the board edge in mm. Default 0.5."),
                    "default": 0.5,
                },
                "maxVias": {
                    "type": "integer",
                    "description": (
                        "Cap on total placements across all strategies "
                        "(default unlimited). Useful when iterating."
                    ),
                },
                "dryRun": {
                    "type": "boolean",
                    "description": (
                        "If true, return the placements that would be "
                        "made but don't modify the board. Default false."
                    ),
                    "default": False,
                },
            },
        },
    },
    {
        "name": "modify_trace",
        "title": "Modify Trace",
        "description": "Modifies properties of an existing trace. Find trace by UUID or position, then change width, layer, or net assignment.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "uuid": {
                    "type": "string",
                    "description": "UUID of the trace to modify",
                },
                "position": {
                    "type": "object",
                    "description": "Find trace nearest to this position",
                    "properties": {
                        "x": {"type": "number", "description": "X coordinate"},
                        "y": {"type": "number", "description": "Y coordinate"},
                        "unit": {
                            "type": "string",
                            "enum": ["mm", "mil", "inch"],
                            "default": "mm",
                        },
                    },
                    "required": ["x", "y"],
                },
                "width": {"type": "number", "description": "New trace width in mm"},
                "layer": {
                    "type": "string",
                    "description": "New layer name (e.g., 'F.Cu', 'B.Cu')",
                },
                "net": {"type": "string", "description": "New net name to assign"},
            },
        },
    },
    {
        "name": "copy_routing_pattern",
        "title": "Copy Routing Pattern",
        "description": "Copies routing pattern from source components to target components. Enables routing replication between identical component groups by calculating and applying position offset.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sourceRefs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Source component references (e.g., ['U1', 'U2', 'U3'])",
                },
                "targetRefs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Target component references (e.g., ['U4', 'U5', 'U6'])",
                },
                "includeVias": {
                    "type": "boolean",
                    "description": "Include vias in the pattern copy",
                    "default": True,
                },
                "traceWidth": {
                    "type": "number",
                    "description": "Override trace width in mm (uses original if not specified)",
                },
            },
            "required": ["sourceRefs", "targetRefs"],
        },
    },
    {
        "name": "get_nets_list",
        "title": "List All Nets",
        "description": "Returns a list of all electrical nets defined on the board.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_netclass",
        "title": "Create Net Class",
        "description": (
            "Defines (or updates) a net class with specific routing rules "
            "(trace width, clearance, etc.) and persists it to the .kicad_pro "
            "project file. In KiCad 9/10 net classes live in the project JSON, "
            "not the board object, so this writes there."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Net class name",
                    "minLength": 1,
                },
                "traceWidth": {
                    "type": "number",
                    "description": "Default trace width in millimeters",
                    "minimum": 0.1,
                },
                "clearance": {
                    "type": "number",
                    "description": "Clearance in millimeters",
                    "minimum": 0.1,
                },
                "viaDiameter": {
                    "type": "number",
                    "description": "Via diameter in millimeters",
                },
                "viaDrill": {
                    "type": "number",
                    "description": "Via drill diameter in millimeters",
                },
                "nets": {
                    "type": "array",
                    "description": "Exact net names to assign to this class",
                    "items": {"type": "string"},
                },
                "patterns": {
                    "type": "array",
                    "description": (
                        "Wildcard membership patterns ('*' = any, '?' = one "
                        "char). Matches the full hierarchical net name, so a "
                        "leading '*' is often needed (e.g. '*VLV?_DRAIN')."
                    ),
                    "items": {"type": "string"},
                },
            },
            "required": ["name", "traceWidth", "clearance"],
        },
    },
    {
        "name": "assign_net_to_class",
        "title": "Assign Net to Class",
        "description": (
            "Assigns an existing net to a net class and persists the "
            "membership to the .kicad_pro project file "
            "(net_settings.netclass_assignments)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "net": {"type": "string", "description": "Name of the net"},
                "netClass": {
                    "type": "string",
                    "description": "Name of the (existing) net class",
                },
            },
            "required": ["net", "netClass"],
        },
    },
    {
        "name": "assign_netclass_pattern",
        "title": "Assign Net Class Pattern",
        "description": (
            "Appends a wildcard pattern -> net-class rule to the .kicad_pro "
            "(net_settings.netclass_patterns). '*' = any, '?' = one char. "
            "Patterns match the full hierarchical net name, so a leading '*' "
            "is often needed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "netClass": {
                    "type": "string",
                    "description": "Name of the (existing) net class",
                },
                "pattern": {
                    "type": "string",
                    "description": "Wildcard pattern, e.g. '+24V_*' or '*VLV?_DRAIN'",
                },
            },
            "required": ["netClass", "pattern"],
        },
    },
    {
        "name": "add_copper_pour",
        "title": "Add Copper Pour",
        "description": "Creates a copper pour/zone (typically for ground or power planes).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "netName": {
                    "type": "string",
                    "description": "Net to connect this copper pour to (e.g., GND, VCC)",
                },
                "layer": {
                    "type": "string",
                    "description": "Layer for the copper pour (e.g., F.Cu, B.Cu)",
                },
                "priority": {
                    "type": "integer",
                    "description": "Pour priority (higher priorities fill first)",
                    "minimum": 0,
                    "default": 0,
                },
                "clearance": {
                    "type": "number",
                    "description": "Clearance from other objects in millimeters",
                    "minimum": 0.1,
                },
                "outline": {
                    "type": "array",
                    "description": "Array of [x, y] points defining the pour boundary",
                    "items": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    "minItems": 3,
                },
            },
            "required": ["netName", "layer", "outline"],
        },
    },
    {
        "name": "route_differential_pair",
        "title": "Route Differential Pair",
        "description": "Routes a differential signal pair with matched lengths and spacing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "positiveName": {
                    "type": "string",
                    "description": "Positive signal net name",
                },
                "negativeName": {
                    "type": "string",
                    "description": "Negative signal net name",
                },
                "layer": {"type": "string", "description": "Layer to route on"},
                "width": {
                    "type": "number",
                    "description": "Trace width in millimeters",
                },
                "gap": {
                    "type": "number",
                    "description": "Gap between traces in millimeters",
                },
                "points": {
                    "type": "array",
                    "description": "Waypoints for the pair routing",
                    "items": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    "minItems": 2,
                },
            },
            "required": ["positiveName", "negativeName", "width", "gap", "points"],
        },
    },
]
