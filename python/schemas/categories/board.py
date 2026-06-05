"""Board (PCB) tool schema definitions for KiCAD MCP Server.

Following the MCP 2025-06-18 specification for tool definitions.
Split out of the former monolithic schemas/tool_schemas.py.
"""

BOARD_TOOLS = [
    {
        "name": "set_board_size",
        "title": "Set Board Dimensions",
        "description": "Sets the PCB board dimensions. The board outline must be added separately using add_board_outline.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "width": {
                    "type": "number",
                    "description": "Board width in millimeters",
                    "minimum": 1,
                },
                "height": {
                    "type": "number",
                    "description": "Board height in millimeters",
                    "minimum": 1,
                },
            },
            "required": ["width", "height"],
        },
    },
    {
        "name": "add_board_outline",
        "title": "Add Board Outline",
        "description": "Adds a board outline shape (rectangle, rounded_rectangle, circle, or polygon) on the Edge.Cuts layer. By default the board top-left corner is placed at (0, 0) so all coordinates are positive. Use x/y to set a different top-left corner position.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "shape": {
                    "type": "string",
                    "enum": ["rectangle", "rounded_rectangle", "circle", "polygon"],
                    "description": "Shape type for the board outline",
                },
                "width": {
                    "type": "number",
                    "description": "Width in mm (for rectangle/rounded_rectangle)",
                    "minimum": 1,
                },
                "height": {
                    "type": "number",
                    "description": "Height in mm (for rectangle/rounded_rectangle)",
                    "minimum": 1,
                },
                "x": {
                    "type": "number",
                    "description": "X coordinate of the top-left corner in mm (default: 0). Board extends from x to x+width.",
                },
                "y": {
                    "type": "number",
                    "description": "Y coordinate of the top-left corner in mm (default: 0). Board extends from y to y+height.",
                },
                "radius": {
                    "type": "number",
                    "description": "Corner radius in mm for rounded_rectangle, or radius for circle",
                    "minimum": 0,
                },
                "points": {
                    "type": "array",
                    "description": "Array of {x, y} point objects in mm (for polygon shape only)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "number"},
                            "y": {"type": "number"},
                        },
                        "required": ["x", "y"],
                    },
                    "minItems": 3,
                },
            },
            "required": ["shape"],
        },
    },
    {
        "name": "add_layer",
        "title": "Add Custom Layer",
        "description": "Adds a new custom layer to the board stack (e.g., User.1, User.Comments).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "layerName": {
                    "type": "string",
                    "description": "Name of the layer to add",
                },
                "layerType": {
                    "type": "string",
                    "enum": ["signal", "power", "mixed", "jumper"],
                    "description": "Type of layer (for copper layers)",
                },
            },
            "required": ["layerName"],
        },
    },
    {
        "name": "set_active_layer",
        "title": "Set Active Layer",
        "description": "Sets the currently active layer for drawing operations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "layerName": {
                    "type": "string",
                    "description": "Name of the layer to make active (e.g., F.Cu, B.Cu, Edge.Cuts)",
                }
            },
            "required": ["layerName"],
        },
    },
    {
        "name": "get_layer_list",
        "title": "List Board Layers",
        "description": "Returns a list of all layers in the board with their properties.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_board_info",
        "title": "Get Board Information",
        "description": "Retrieves comprehensive board information including dimensions, layer count, component count, and design rules.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_board_2d_view",
        "title": "Render Board Preview",
        "description": (
            "Generates a 2D visual representation of the current board state as a PNG, JPG, or SVG image. "
            "Use responseMode to control how the image is returned. "
            'responseMode="inline" (default) returns the image bytes as a base64-encoded imageData '
            "string in the JSON response — convenient for small boards but may exceed message-size limits on "
            "large designs. "
            'responseMode="file" writes the image next to the .kicad_pcb file as '
            "<board>_2d_view.<ext> and returns a filePath; callers that can open local files should "
            "prefer this mode for large boards."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "width": {
                    "type": "number",
                    "description": "Image width in pixels (default: 800)",
                    "minimum": 100,
                    "default": 800,
                },
                "height": {
                    "type": "number",
                    "description": "Image height in pixels (default: 600)",
                    "minimum": 100,
                    "default": 600,
                },
                "format": {
                    "type": "string",
                    "enum": ["png", "jpg", "svg"],
                    "description": "Output image format (default: png)",
                    "default": "png",
                },
                "layers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of layer names to include; all enabled layers if omitted",
                },
                "responseMode": {
                    "type": "string",
                    "enum": ["inline", "file"],
                    "default": "inline",
                    "description": (
                        "How to return the image. "
                        '"inline" (default): base64-encoded bytes in the imageData response field. '
                        '"file": write to <board>_2d_view.<ext> next to the PCB and return filePath.'
                    ),
                },
            },
        },
    },
    {
        "name": "get_board_extents",
        "title": "Get Board Bounding Box",
        "description": "Returns the bounding box extents of the PCB board including all edge cuts, components, and traces.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "unit": {
                    "type": "string",
                    "enum": ["mm", "mil", "inch"],
                    "description": "Unit for returned coordinates (default: mm)",
                    "default": "mm",
                }
            },
        },
    },
    {
        "name": "add_mounting_hole",
        "title": "Add Mounting Hole",
        "description": "Adds a mounting hole at the specified position with given diameter. Defaults to non-plated (NPTH) with mask-only pad layers; set plated=true for a PTH with copper pad.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "position": {
                    "type": "object",
                    "description": "Position of the mounting hole",
                    "properties": {
                        "x": {"type": "number", "description": "X coordinate"},
                        "y": {"type": "number", "description": "Y coordinate"},
                        "unit": {
                            "type": "string",
                            "enum": ["mm", "mil", "inch"],
                            "default": "mm",
                            "description": "Unit for x/y (default mm)",
                        },
                    },
                    "required": ["x", "y"],
                },
                "diameter": {
                    "type": "number",
                    "description": "Hole (drill) diameter in millimeters",
                    "minimum": 0.1,
                },
                "padDiameter": {
                    "type": "number",
                    "description": "Pad diameter in millimeters (defaults to diameter + 1mm). For NPTH this only affects the solder-mask opening, not copper.",
                    "minimum": 0.1,
                },
                "plated": {
                    "type": "boolean",
                    "default": False,
                    "description": "True for plated through-hole (PTH) with copper pad; false (default) for NPTH (mask only).",
                },
                "footprintLibId": {
                    "type": "string",
                    "description": "Optional library:name FPID (e.g. 'MountingHole:MountingHole_3.2mm'). Defaults to MountingHole:MountingHole_<diameter>mm. A non-empty FPID is required for the footprint to be selectable in KiCad's GUI Move tool.",
                },
            },
            "required": ["position", "diameter"],
        },
    },
    {
        "name": "import_svg_logo",
        "title": "Import SVG Logo to PCB",
        "description": "Imports an SVG file as filled graphic polygons onto a KiCAD PCB layer (default F.SilkS). Curves are linearised automatically. Supports path, rect, circle, ellipse, polygon and group transforms.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pcbPath": {
                    "type": "string",
                    "description": "Path to the .kicad_pcb file",
                },
                "svgPath": {
                    "type": "string",
                    "description": "Path to the SVG logo file",
                },
                "x": {
                    "type": "number",
                    "description": "X position of the logo top-left corner in mm",
                },
                "y": {
                    "type": "number",
                    "description": "Y position of the logo top-left corner in mm",
                },
                "width": {
                    "type": "number",
                    "description": "Target width of the logo in mm (height scaled to preserve aspect ratio)",
                    "minimum": 0.1,
                },
                "layer": {
                    "type": "string",
                    "description": "PCB layer name, e.g. F.SilkS or B.SilkS (default: F.SilkS)",
                    "default": "F.SilkS",
                },
                "strokeWidth": {
                    "type": "number",
                    "description": "Outline stroke width in mm (0 = no outline, default 0)",
                    "default": 0,
                },
                "filled": {
                    "type": "boolean",
                    "description": "Fill polygons with solid layer colour (default true)",
                    "default": True,
                },
            },
            "required": ["pcbPath", "svgPath", "x", "y", "width"],
        },
    },
    {
        "name": "add_board_text",
        "title": "Add Text to Board",
        "description": "Adds text annotation to the board on a specified layer (e.g., F.SilkS for top silkscreen).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Text content to add",
                    "minLength": 1,
                },
                "x": {"type": "number", "description": "X coordinate in millimeters"},
                "y": {"type": "number", "description": "Y coordinate in millimeters"},
                "layer": {
                    "type": "string",
                    "description": "Layer name (e.g., F.SilkS, B.SilkS, F.Cu)",
                    "default": "F.SilkS",
                },
                "size": {
                    "type": "number",
                    "description": "Text size in millimeters",
                    "minimum": 0.1,
                    "default": 1.0,
                },
                "thickness": {
                    "type": "number",
                    "description": "Text thickness in millimeters",
                    "minimum": 0.01,
                    "default": 0.15,
                },
            },
            "required": ["text", "x", "y"],
        },
    },
]
