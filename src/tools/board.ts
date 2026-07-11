/**
 * Board management tools for KiCAD MCP server
 *
 * These tools handle board setup, layer management, and board properties
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { logger } from "../logger.js";
import { CommandFunction, formatKicadResult, makePassthrough } from "./tool-response.js";

/**
 * Register board management tools with the MCP server
 *
 * @param server MCP server instance
 * @param callKicadScript Function to call KiCAD script commands
 */
export function registerBoardTools(server: McpServer, callKicadScript: CommandFunction): void {
  logger.info("Registering board management tools");

  // ------------------------------------------------------
  // Set Board Size Tool
  // ------------------------------------------------------
  server.tool(
    "set_board_size",
    "Set the PCB board dimensions by drawing a rectangular Edge.Cuts outline. Replaces any existing Edge.Cuts geometry by default — pass clearExisting=false to append instead.",
    {
      width: z.number().describe("Board width"),
      height: z.number().describe("Board height"),
      unit: z.enum(["mm", "mil", "inch"]),
      clearExisting: z
        .boolean()
        .optional()
        .describe(
          "When true (default), remove existing Edge.Cuts shapes first to avoid overlapping outlines. Set to false to keep current outline and add a new rectangle on top.",
        ),
    },
    async ({ width, height, unit, clearExisting }) => {
      logger.debug(`Setting board size to ${width}x${height} ${unit}`);
      const result = await callKicadScript("set_board_size", {
        width,
        height,
        unit,
        clearExisting,
      });

      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Add Layer Tool
  // ------------------------------------------------------
  server.tool(
    "add_layer",
    "Add a new copper or technical layer to the PCB stackup.",
    {
      name: z.string().describe("Layer name"),
      type: z.enum(["copper", "technical", "user", "signal"]).describe("Layer type"),
      position: z.enum(["top", "bottom", "inner"]).describe("Layer position"),
      number: z.number().optional().describe("Layer number (for inner layers)"),
    },
    async ({ name, type, position, number }) => {
      logger.debug(`Adding ${type} layer: ${name}`);
      const result = await callKicadScript("add_layer", {
        name,
        type,
        position,
        number,
      });

      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Set Active Layer Tool
  // ------------------------------------------------------
  server.tool(
    "set_active_layer",
    "Set the currently active PCB layer by name (e.g. F.Cu, B.Cu).",
    {
      layer: z.string().describe("Layer name to set as active"),
    },
    async ({ layer }) => {
      logger.debug(`Setting active layer to: ${layer}`);
      const result = await callKicadScript("set_active_layer", { layer });

      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Get Board Info Tool
  // ------------------------------------------------------
  server.tool(
    "get_board_info",
    "Retrieve general information about the current PCB board (dimensions, layer count, DRC status).",
    {},
    async () => {
      logger.debug("Getting board information");
      const result = await callKicadScript("get_board_info", {});

      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Get Layer List Tool
  // ------------------------------------------------------
  server.tool(
    "get_layer_list",
    "Return the list of all layers defined in the current PCB board.",
    {},
    async () => {
      logger.debug("Getting layer list");
      const result = await callKicadScript("get_layer_list", {});

      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Add Board Outline Tool
  // ------------------------------------------------------
  server.tool(
    "add_board_outline",
    "Draw the PCB board outline (Edge.Cuts layer) as a rectangle, rounded rectangle, circle or polygon.",
    {
      shape: z
        .enum(["rectangle", "circle", "polygon", "rounded_rectangle"])
        .describe("Shape of the outline"),
      params: z
        .object({
          // For rectangle / rounded_rectangle
          width: z.number().optional().describe("Width of rectangle"),
          height: z.number().optional().describe("Height of rectangle"),
          cornerRadius: z.number().optional().describe("Corner radius for rounded_rectangle (mm)"),
          // For circle
          radius: z.number().optional().describe("Radius of circle"),
          // For polygon
          points: z
            .array(
              z.object({
                x: z.number(),
                y: z.number(),
              }),
            )
            .optional()
            .describe("Points of polygon"),
          // Position: top-left corner for rectangles/rounded_rectangle, center for circle
          x: z.number().describe("X coordinate of top-left corner for rectangles (default: 0)"),
          y: z.number().describe("Y coordinate of top-left corner for rectangles (default: 0)"),
          unit: z.enum(["mm", "mil", "inch"]),
        })
        .describe("Parameters for the outline shape"),
    },
    async ({ shape, params }) => {
      logger.debug(`Adding ${shape} board outline`);
      // Pass x/y as-is to Python; outline.py treats them as top-left corner
      // and computes the center internally (center = x + width/2, y + height/2).
      const result = await callKicadScript("add_board_outline", {
        shape,
        ...params,
      });

      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Add Mounting Hole Tool
  // ------------------------------------------------------
  server.tool(
    "add_mounting_hole",
    "Place a mounting hole (NPTH or PTH) at the specified position on the PCB.",
    {
      position: z
        .object({
          x: z.number(),
          y: z.number(),
          unit: z.enum(["mm", "mil", "inch"]),
        })
        .describe("Position of the mounting hole"),
      diameter: z.number().describe("Diameter of the hole"),
      padDiameter: z.number().optional().describe("Optional diameter of the pad around the hole"),
    },
    async ({ position, diameter, padDiameter }) => {
      logger.debug(`Adding mounting hole at (${position.x},${position.y}) ${position.unit}`);
      const result = await callKicadScript("add_mounting_hole", {
        position,
        diameter,
        padDiameter,
      });

      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Add Text Tool
  // ------------------------------------------------------
  server.tool(
    "add_board_text",
    "Add a text label to a PCB layer (e.g. silkscreen, fab, courtyard).",
    {
      text: z.string().describe("Text content"),
      position: z
        .object({
          x: z.number(),
          y: z.number(),
          unit: z.enum(["mm", "mil", "inch"]),
        })
        .describe("Position of the text"),
      layer: z.string().describe("Layer to place the text on"),
      size: z.number().describe("Text size"),
      thickness: z.number().optional().describe("Line thickness"),
      rotation: z.number().optional().describe("Rotation angle in degrees"),
      style: z.enum(["normal", "italic", "bold"]).optional().describe("Text style"),
    },
    async ({ text, position, layer, size, thickness, rotation, style }) => {
      logger.debug(`Adding text "${text}" at (${position.x},${position.y}) ${position.unit}`);
      const result = await callKicadScript("add_board_text", {
        text,
        position,
        layer,
        size,
        thickness,
        rotation,
        style,
      });

      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Get Board Extents Tool
  // ------------------------------------------------------
  server.tool(
    "get_board_extents",
    "Return the bounding box (min/max X and Y) of all objects on the current PCB board.",
    {
      unit: z.enum(["mm", "mil", "inch"]).optional().describe("Unit of measurement for the result"),
    },
    async ({ unit }) => {
      logger.debug("Getting board extents");
      const result = await callKicadScript("get_board_extents", { unit });

      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Get Board 2D View Tool
  // ------------------------------------------------------
  server.tool(
    "get_board_2d_view",
    [
      "Render a 2D image of the current PCB board and return it as PNG, JPG or SVG.",
      "Use responseMode to choose how the image is delivered:",
      '  "inline" (default) — base64-encoded bytes returned in imageData; works well for small boards.',
      '  "file" — image written next to the .kicad_pcb as <board>_2d_view.<ext>; filePath is returned.',
      "Use file mode for large boards to avoid hitting MCP message-size limits.",
    ].join(" "),
    {
      layers: z.array(z.string()).optional().describe("Optional array of layer names to include"),
      width: z.number().optional().describe("Optional width of the image in pixels"),
      height: z.number().optional().describe("Optional height of the image in pixels"),
      format: z.enum(["png", "jpg", "svg"]).optional().describe("Image format"),
      responseMode: z
        .enum(["inline", "file"])
        .optional()
        .describe(
          'How to return the image: "inline" (default) returns base64 imageData; "file" writes to disk and returns filePath',
        ),
      cropToBoard: z
        .boolean()
        .optional()
        .describe(
          "Crop the rendered image to the actual board content + margin (default true). Set false to keep the full plot canvas — useful when isolating a stray outline you want to see.",
        ),
      cropMarginPx: z
        .number()
        .optional()
        .describe("Margin in pixels around the cropped board content (default 20)."),
    },
    async ({ layers, width, height, format, responseMode, cropToBoard, cropMarginPx }) => {
      logger.debug("Getting 2D board view");
      const result = await callKicadScript("get_board_2d_view", {
        layers,
        width,
        height,
        format,
        responseMode,
        cropToBoard,
        cropMarginPx,
      });

      // Inline images must go out as MCP image content, not as base64 inside
      // a JSON text block — clients can't render the latter and the model
      // pays for every base64 character as tokens.
      const r = result as {
        success?: boolean;
        imageData?: string;
        format?: string;
        message?: string;
      };
      if (r?.success && typeof r.imageData === "string" && r.imageData.length > 0) {
        if (r.format === "svg") {
          return {
            content: [
              { type: "text" as const, text: Buffer.from(r.imageData, "base64").toString("utf8") },
            ],
          };
        }
        return {
          content: [
            {
              type: "image" as const,
              data: r.imageData,
              mimeType: r.format === "jpg" ? "image/jpeg" : "image/png",
            },
          ],
        };
      }
      return formatKicadResult(result);
    },
  );

  logger.info("Board management tools registered");

  // Import SVG logo onto PCB layer (silkscreen)
  server.tool(
    "import_svg_logo",
    "Imports an SVG file as filled graphic polygons onto a KiCAD PCB layer (default F.SilkS / front silkscreen). Curves are linearised automatically. Ideal for placing a company or project logo on the board.",
    {
      pcbPath: z.string().describe("Path to the .kicad_pcb file"),
      svgPath: z.string().describe("Path to the SVG logo file"),
      x: z.number().describe("X position of the logo top-left corner in mm"),
      y: z.number().describe("Y position of the logo top-left corner in mm"),
      width: z
        .number()
        .describe("Target width of the logo in mm (height is scaled to preserve aspect ratio)"),
      layer: z
        .string()
        .optional()
        .describe("PCB layer name, e.g. F.SilkS or B.SilkS (default: F.SilkS)"),
      strokeWidth: z
        .number()
        .optional()
        .describe("Outline stroke width in mm (0 = no outline, default 0)"),
      filled: z.boolean().optional().describe("Fill polygons with solid colour (default true)"),
    },
    async (args: {
      pcbPath: string;
      svgPath: string;
      x: number;
      y: number;
      width: number;
      layer?: string;
      strokeWidth?: number;
      filled?: boolean;
    }) => {
      const result = await callKicadScript("import_svg_logo", args);
      if (result.success) {
        return {
          content: [
            {
              type: "text",
              text: [
                result.message,
                `Polygons: ${result.polygon_count}`,
                `Size: ${result.logo_width_mm?.toFixed(2)} × ${result.logo_height_mm?.toFixed(2)} mm`,
                `Layer: ${result.layer}`,
              ].join("\n"),
            },
          ],
        };
      } else {
        return {
          content: [
            { type: "text", text: `SVG import failed: ${result.message || "Unknown error"}` },
          ],
          isError: true,
        };
      }
    },
  );

  // ------------------------------------------------------
  // Board metadata: origins + title block (IPC-only)
  //
  // Drill/place origin matters for Gerber + PnP coordinate alignment —
  // without setting it, fab files come out relative to (0,0) of the
  // KiCad page, which rarely matches what the fab house expects.
  // Title block is what shows up on the printed PDF: company / rev /
  // date / nine free-form comment slots.
  // ------------------------------------------------------
  const passthrough = makePassthrough(callKicadScript);

  // get_pcb_overview: aggregator that fans out to multiple list_ queries
  // server-side and returns a single response — saves 3-4 MCP round-trips.
  server.tool(
    "get_pcb_overview",
    "One-shot snapshot of the loaded PCB: components, tracks, zones, nets, layers in a single response. Use this instead of calling get_component_list + query_traces + query_zones + get_nets_list separately.",
    {},
    passthrough("get_pcb_overview"),
  );

  const originTypeSchema = z
    .enum(["grid", "drill", "aux"])
    .describe(
      "'grid' = user grid origin; 'drill' (or 'aux') = drill/place origin used by Gerber and pick-and-place files.",
    );

  server.tool(
    "get_origin",
    "Return the board's grid or drill/place origin (IPC-only). The drill origin is what Gerber and pick-and-place exports use as their coordinate zero — get it wrong and the fab house gets coordinates shifted by the page margin.",
    {
      type: originTypeSchema.optional().describe("Default 'drill'."),
      unit: z.enum(["mm", "inch"]).optional().describe("Coordinate unit (default mm)."),
    },
    passthrough("get_origin"),
  );

  // `position` is required at the schema level so MCP clients can't
  // accidentally call set_origin with no coordinate and snap the drill
  // origin to (0, 0) — which would silently invalidate every Gerber/PnP
  // file the user exports next. The backend also rejects missing coords
  // as a second line of defence for non-schema callers.
  server.tool(
    "set_origin",
    "Move the board's grid or drill/place origin (IPC-only). Set the drill origin before exporting Gerber / PnP files so fab coordinates align with your reference point (typically a board corner or fiducial). Coordinates are required — calling with no position will be rejected to prevent accidentally snapping to (0,0).",
    {
      type: originTypeSchema,
      position: z
        .object({
          x: z.number(),
          y: z.number(),
          unit: z.enum(["mm", "inch"]).optional(),
        })
        .describe("Target coordinate in mm (default) or inch."),
    },
    passthrough("set_origin"),
  );

  server.tool(
    "get_title_block_info",
    "Read the board's title block — title, date, revision, company, and the nine free-form comment slots that appear on plotted PDF / drawing-sheet output (IPC-only).",
    {},
    passthrough("get_title_block_info"),
  );

  server.tool(
    "set_title_block_info",
    "Partial-update the board's title block (IPC-only). Any omitted field is preserved at its current value; pass an explicit empty string to clear a field/slot. `comments` accepts {'1': 'text', '5': 'more'} (slots 1-9) or a positional list ['a','b'] (index 0 → slot 1).",
    {
      title: z.string().optional().describe("Drawing title."),
      date: z.string().optional().describe("Date string (free-form — KiCad doesn't parse it)."),
      revision: z.string().optional().describe("Revision (e.g. 'A', 'v1.2')."),
      company: z.string().optional().describe("Company / author name."),
      comments: z
        .union([z.record(z.string(), z.string()), z.array(z.string()).max(9)])
        .optional()
        .describe("Comments. Dict keyed '1'..'9' or positional list (max 9)."),
    },
    passthrough("set_title_block_info"),
  );
}
