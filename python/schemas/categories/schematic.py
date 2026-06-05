"""Schematic tool schema definitions for KiCAD MCP Server.

Following the MCP 2025-06-18 specification for tool definitions.
Split out of the former monolithic schemas/tool_schemas.py.
"""

SCHEMATIC_TOOLS = [
    {
        "name": "create_schematic",
        "title": "Create New Schematic",
        "description": "Creates a new KiCAD schematic file for circuit design.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Path for the new schematic file (.kicad_sch)",
                },
                "title": {"type": "string", "description": "Schematic title"},
                "overwrite": {
                    "type": "boolean",
                    "description": "Replace an existing schematic file. Defaults to false: if the target .kicad_sch already exists the tool refuses (errorCode SCHEMATIC_EXISTS) instead of overwriting it.",
                },
            },
            "required": ["filename"],
        },
    },
    {
        "name": "load_schematic",
        "title": "Load Existing Schematic",
        "description": "Opens an existing KiCAD schematic file for editing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Path to schematic file (.kicad_sch)",
                }
            },
            "required": ["filename"],
        },
    },
    {
        "name": "add_schematic_component",
        "title": "Add Component to Schematic",
        "description": "Places a symbol (resistor, capacitor, IC, etc.) on the schematic.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "Reference designator (e.g., R1, C2, U3)",
                },
                "symbol": {
                    "type": "string",
                    "description": "Symbol library:name (e.g., Device:R, Device:C)",
                },
                "value": {
                    "type": "string",
                    "description": "Component value (e.g., 10k, 0.1uF)",
                },
                "x": {"type": "number", "description": "X coordinate on schematic"},
                "y": {"type": "number", "description": "Y coordinate on schematic"},
            },
            "required": ["reference", "symbol", "x", "y"],
        },
    },
    {
        "name": "add_schematic_wire",
        "title": "Draw Wire Between Pins",
        "description": "Draws a wire on the schematic between two or more coordinate points. Always call get_schematic_pin_locations first to get the approximate pin coordinates, then pass them as the first and last waypoints. snapToPins (on by default) will correct any float imprecision by snapping endpoints to the exact nearest pin coordinate. To route around components, add intermediate waypoints between the start and end: e.g. [[x1,y1], [xMid,y1], [xMid,y2], [x2,y2]] routes horizontally then vertically. Intermediate waypoints are never snapped.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to schematic file",
                },
                "waypoints": {
                    "type": "array",
                    "description": "Array of [x, y] coordinates defining the wire path. First and last points are the pin locations (from get_schematic_pin_locations). Add intermediate points to route around obstacles.",
                    "items": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    "minItems": 2,
                },
                "snapToPins": {
                    "type": "boolean",
                    "description": "When true, the first and last waypoints are snapped to the nearest schematic pin within snapTolerance mm. Intermediate waypoints are left unchanged. Enabled by default to correct float coordinate imprecision.",
                    "default": True,
                },
                "snapTolerance": {
                    "type": "number",
                    "description": "Maximum distance in mm to search for a nearby pin when snapToPins is enabled.",
                    "default": 1.0,
                },
            },
            "required": ["schematicPath", "waypoints"],
        },
    },
    {
        "name": "add_schematic_net_label",
        "title": "Add Net Label",
        "description": (
            "Add a net label to a schematic. "
            "PREFERRED: supply componentRef + pinNumber to snap the label to the exact pin endpoint — "
            "this guarantees an electrical connection. "
            "Alternatively supply position [x, y], but the coordinates must match the pin endpoint exactly "
            "(even a 0.01 mm offset breaks the connection). "
            "The response includes actual_position (coordinates actually used) and snapped_to_pin "
            "(present when a pin reference was resolved)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to schematic file",
                },
                "netName": {
                    "type": "string",
                    "description": "Name of the net (e.g., VCC, GND, SDA)",
                },
                "position": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 2,
                    "maxItems": 2,
                    "description": "Position [x, y] for the label. Required when componentRef/pinNumber are not given.",
                },
                "componentRef": {
                    "type": "string",
                    "description": "Component reference to snap label to (e.g. U1, R1). Use with pinNumber.",
                },
                "pinNumber": {
                    "type": "string",
                    "description": "Pin number or name on componentRef (e.g. '1', 'GND'). Use with componentRef.",
                },
                "labelType": {
                    "type": "string",
                    "enum": ["label", "global_label", "hierarchical_label"],
                    "description": "Label type (default: label)",
                    "default": "label",
                },
                "orientation": {
                    "type": "number",
                    "description": "Rotation angle in degrees (0, 90, 180, 270)",
                    "default": 0,
                },
            },
            "required": ["schematicPath", "netName"],
        },
    },
    {
        "name": "connect_to_net",
        "title": "Connect Pin to Net",
        "description": (
            "Connect a component pin to a named net by adding a wire stub and net label at the exact "
            "pin endpoint. The response includes pin_location (exact pin coords), label_location "
            "(where the label was placed), and wire_stub (the wire segment added) so you can confirm "
            "the placement without a separate verification call."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to schematic file",
                },
                "componentRef": {
                    "type": "string",
                    "description": "Component reference designator (e.g., R1, U3)",
                },
                "pinName": {
                    "type": "string",
                    "description": "Pin number or name on the component",
                },
                "netName": {
                    "type": "string",
                    "description": "Name of the net to connect to",
                },
            },
            "required": ["schematicPath", "componentRef", "pinName", "netName"],
        },
    },
    {
        "name": "get_net_connections",
        "title": "Get Net Connections",
        "description": "Returns all components and pins connected to a specified net.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to schematic file",
                },
                "netName": {
                    "type": "string",
                    "description": "Name of the net to query",
                },
            },
            "required": ["schematicPath", "netName"],
        },
    },
    {
        "name": "get_wire_connections",
        "title": "Get Wire Connections",
        "description": (
            "Returns the net name and all wires and component pins connected at a given point. "
            "Accepts either a component reference + pin number (e.g. reference='U1', pin='3') "
            "or a schematic coordinate (x, y in mm). "
            "The response includes: 'net' (label name or null for unnamed nets), "
            "'pins' (all component pins on the net), 'wires' (all wire segments on the net), "
            "and 'query_point' (the resolved coordinate used). "
            "The query point must be at a wire endpoint or junction — wire midpoints are not matched. "
            "Use get_schematic_pin_locations or list_schematic_wires to obtain exact endpoint coordinates."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to the schematic file (.kicad_sch)",
                },
                "reference": {
                    "type": "string",
                    "description": "Component reference (e.g. U1, R1). Pair with pin.",
                },
                "pin": {
                    "type": "string",
                    "description": "Pin number or name (e.g. '3', 'SDA'). Pair with reference.",
                },
                "x": {
                    "type": "number",
                    "description": "X coordinate of a wire endpoint in mm. Pair with y.",
                },
                "y": {
                    "type": "number",
                    "description": "Y coordinate of a wire endpoint in mm. Pair with x.",
                },
            },
            "required": ["schematicPath"],
        },
    },
    {
        "name": "get_net_at_point",
        "title": "Get Net At Point",
        "description": (
            "Returns the net name at a given (x, y) coordinate in a schematic, "
            "or null if no net label or wire endpoint is present at that position. "
            "Checks net label positions first, then wire endpoints. "
            "Useful for quickly identifying what net occupies a specific coordinate "
            "without traversing the full wire graph."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to the schematic file (.kicad_sch)",
                },
                "x": {
                    "type": "number",
                    "description": "X coordinate in mm",
                },
                "y": {
                    "type": "number",
                    "description": "Y coordinate in mm",
                },
            },
            "required": ["schematicPath", "x", "y"],
        },
    },
    {
        "name": "get_schematic_pin_locations",
        "title": "Get Schematic Pin Locations",
        "description": "Returns the exact absolute coordinates of all pins on a schematic component. Use this BEFORE placing net labels with add_schematic_net_label to get the correct x/y position for each pin endpoint.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to the schematic file",
                },
                "reference": {
                    "type": "string",
                    "description": "Component reference designator (e.g., U1, R1, J2)",
                },
            },
            "required": ["schematicPath", "reference"],
        },
    },
    {
        "name": "connect_passthrough",
        "title": "Connect Passthrough (Pin-to-Pin)",
        "description": "Connects all pins of a source connector to the matching pins of a target connector using shared net labels. Ideal for passthrough adapters where J1 pin N connects directly to J2 pin N. Each pair gets a net label '{netPrefix}_{pinNumber}'. Use this instead of calling connect_to_net 15 times for FFC/ribbon cable passthroughs. NOTE: KiCAD Connector_Generic symbols always have pin 1 at the TOP of the symbol and pin N at the BOTTOM. When assigning named nets (e.g. GND, CAM_SCL) to specific pin numbers, always use the physical pin number as shown in the connector datasheet — pin 1 = top of symbol.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to the schematic file",
                },
                "sourceRef": {
                    "type": "string",
                    "description": "Reference of the source connector (e.g., J1)",
                },
                "targetRef": {
                    "type": "string",
                    "description": "Reference of the target connector (e.g., J2)",
                },
                "netPrefix": {
                    "type": "string",
                    "description": "Prefix for generated net names, e.g. 'CSI' produces CSI_1, CSI_2, ... (default: PIN)",
                },
                "pinOffset": {
                    "type": "integer",
                    "description": "Add this value to the pin number when building net names (default: 0)",
                },
            },
            "required": ["schematicPath", "sourceRef", "targetRef"],
        },
    },
    {
        "name": "run_erc",
        "title": "Run Electrical Rules Check (ERC)",
        "description": "Runs the KiCAD Electrical Rules Check (ERC) on a schematic via kicad-cli and returns all violations with type, severity, and location. Use this to verify the schematic is electrically correct before generating a netlist or exporting.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to the .kicad_sch schematic file",
                }
            },
            "required": ["schematicPath"],
        },
    },
    {
        "name": "sync_schematic_to_board",
        "title": "Sync Schematic to PCB (F8)",
        "description": "Reads net connections from the schematic and assigns them to matching component pads in the PCB board file. Equivalent to KiCAD Pcbnew F8 'Update PCB from Schematic'. Must be called after placing components and before routing traces, so that pad-to-net assignments are correct.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to .kicad_sch file. If omitted, auto-detected from current board path.",
                },
                "boardPath": {
                    "type": "string",
                    "description": "Path to .kicad_pcb file. If omitted, uses currently loaded board.",
                },
            },
        },
    },
    {
        "name": "generate_netlist",
        "title": "Generate Netlist (JSON)",
        "description": (
            "Returns a structured JSON netlist from the schematic: component list "
            "(reference, value, footprint) and net list (net name + all connected "
            "component/pin pairs). Uses kicad-cli internally — requires a saved "
            ".kicad_sch file. For writing to a file or exporting SPICE/Cadstar/OrcadPCB2 "
            "format, use export_netlist instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Absolute path to the .kicad_sch schematic file",
                },
            },
            "required": ["schematicPath"],
        },
    },
    {
        "name": "list_schematic_libraries",
        "title": "List Symbol Libraries",
        "description": "Lists all available symbol libraries for schematic design.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "searchPaths": {
                    "type": "array",
                    "description": "Optional additional paths to search for libraries",
                    "items": {"type": "string"},
                }
            },
        },
    },
    {
        "name": "export_schematic_pdf",
        "title": "Export Schematic to PDF",
        "description": "Exports the schematic as a PDF document for printing or documentation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to schematic file",
                },
                "outputPath": {"type": "string", "description": "Path for output PDF"},
            },
            "required": ["schematicPath", "outputPath"],
        },
    },
    # --- Schematic Analysis Tools (read-only) ---
    {
        "name": "get_schematic_view_region",
        "title": "Get Schematic View Region",
        "description": "Exports a cropped region of the schematic as an image (PNG or SVG). Specify a bounding box in schematic mm coordinates to zoom into a specific area.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to the .kicad_sch schematic file",
                },
                "x1": {
                    "type": "number",
                    "description": "Left X coordinate of the region in mm",
                },
                "y1": {
                    "type": "number",
                    "description": "Top Y coordinate of the region in mm",
                },
                "x2": {
                    "type": "number",
                    "description": "Right X coordinate of the region in mm",
                },
                "y2": {
                    "type": "number",
                    "description": "Bottom Y coordinate of the region in mm",
                },
                "format": {
                    "type": "string",
                    "enum": ["png", "svg"],
                    "description": "Output image format (default: png)",
                },
                "width": {
                    "type": "integer",
                    "description": "Output image width in pixels (default: 800)",
                },
                "height": {
                    "type": "integer",
                    "description": "Output image height in pixels (default: 600)",
                },
            },
            "required": ["schematicPath", "x1", "y1", "x2", "y2"],
        },
    },
    {
        "name": "find_overlapping_elements",
        "title": "Find Overlapping Elements",
        "description": "Detects spatially overlapping symbols, wires, and labels in the schematic. Finds: duplicate power symbols at the same position, collinear overlapping wire segments, and labels stacked on top of each other.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to the .kicad_sch schematic file",
                },
                "tolerance": {
                    "type": "number",
                    "description": "Distance threshold in mm for label proximity and wire collinearity checks. Symbol overlap uses bounding-box intersection. (default: 0.5)",
                },
            },
            "required": ["schematicPath"],
        },
    },
    {
        "name": "get_elements_in_region",
        "title": "Get Elements in Region",
        "description": "Lists all symbols, wires, and labels within a rectangular region of the schematic. Useful for understanding what is in a specific area before modifying it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to the .kicad_sch schematic file",
                },
                "x1": {
                    "type": "number",
                    "description": "Left X coordinate of the region in mm",
                },
                "y1": {
                    "type": "number",
                    "description": "Top Y coordinate of the region in mm",
                },
                "x2": {
                    "type": "number",
                    "description": "Right X coordinate of the region in mm",
                },
                "y2": {
                    "type": "number",
                    "description": "Bottom Y coordinate of the region in mm",
                },
            },
            "required": ["schematicPath", "x1", "y1", "x2", "y2"],
        },
    },
    {
        "name": "find_wires_crossing_symbols",
        "title": "Find Wires Crossing Symbols",
        "description": "Find all wires that cross over component symbol bodies. Wires passing over symbols are unacceptable in schematics — they indicate routing mistakes where a wire was drawn across a component instead of around it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to the .kicad_sch schematic file",
                }
            },
            "required": ["schematicPath"],
        },
    },
    {
        "name": "find_orphaned_wires",
        "title": "Find Orphaned Wires",
        "description": (
            "Find wire segments with at least one dangling endpoint — an endpoint not connected "
            "to a component pin, net label, or another wire. "
            "Orphaned wires cause ERC 'wire end unconnected' errors and indicate routing mistakes. "
            "Does not require the KiCad UI to be running."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to the .kicad_sch schematic file",
                }
            },
            "required": ["schematicPath"],
        },
    },
    {
        "name": "list_floating_labels",
        "title": "List Floating Net Labels",
        "description": (
            "Returns all net labels in the schematic that are not connected to any component pin. "
            "A label is 'floating' when no component pin's coordinate falls on the wire-network "
            "reachable from the label's anchor position. "
            "Floating labels indicate misplaced or off-grid labels that will cause ERC errors. "
            "Does not require the KiCad UI to be running."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to the .kicad_sch schematic file",
                }
            },
            "required": ["schematicPath"],
        },
    },
    {
        "name": "snap_to_grid",
        "title": "Snap Schematic Elements to Grid",
        "description": (
            "Snap schematic element coordinates to the nearest grid point. "
            "KiCAD eeschema uses exact integer matching (10 000 IU/mm) for connectivity, "
            "so even a sub-pixel coordinate offset will make wires appear connected visually "
            "but fail ERC checks. Running this tool before ERC eliminates that class of error. "
            "Modifies the .kicad_sch file in place. "
            "Does not require the KiCAD UI to be running."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to the .kicad_sch schematic file",
                },
                "gridSize": {
                    "type": "number",
                    "description": (
                        "Grid spacing in mm (default: 1.27 — standard KiCAD schematic grid). "
                        "Do NOT use 2.54: half of all valid KiCAD pin positions are at odd "
                        "multiples of 1.27 mm and would be displaced 1.27 mm, breaking "
                        "connectivity."
                    ),
                    "default": 1.27,
                },
                "elements": {
                    "type": "array",
                    "description": (
                        "Element types to snap. "
                        'Valid values: "wires", "junctions", "labels", "components". '
                        'Defaults to ["wires", "junctions", "labels"] when omitted. '
                        '"components" is opt-in because moving a component without re-routing '
                        "its wires will create new mismatches."
                    ),
                    "items": {
                        "type": "string",
                        "enum": ["wires", "junctions", "labels", "components"],
                    },
                },
            },
            "required": ["schematicPath"],
        },
    },
]
