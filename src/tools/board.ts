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
    "Set the PCB board dimensions by drawing a rectangular Edge.Cuts outline.",
    {
      width: z.number().describe("Board width"),
      height: z.number().describe("Board height"),
      unit: z.enum(["mm", "mil", "inch"]),
      clearExisting: z
        .boolean()
        .optional()
        .describe(
          "true (default): remove existing Edge.Cuts first; false: keep them and add on top",
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
  // Get Board Info Tool (includes layer list + extents)
  // ------------------------------------------------------
  server.tool(
    "get_board_info",
    "Get PCB info: dimensions, full layer list, and bounding-box extents (left/top/right/bottom/center) of all board objects.",
    {
      unit: z.enum(["mm", "mil", "inch"]).optional().describe("Unit for the extents (default mm)"),
    },
    async ({ unit }) => {
      logger.debug("Getting board information");
      const result = await callKicadScript("get_board_info", {});
      if (result && typeof result === "object" && result.success !== false) {
        const extents = await callKicadScript("get_board_extents", { unit });
        if (extents && typeof extents === "object" && extents.success !== false) {
          result.extents = extents.extents;
        }
      }
      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Add Board Outline Tool
  // ------------------------------------------------------
  server.tool(
    "add_board_outline",
    "Draw the PCB board outline (Edge.Cuts layer) as a rectangle, rounded rectangle, circle or polygon. params.unit defaults to mm; params.x/y default to 0 (top-left).",
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
          x: z
            .number()
            .optional()
            .describe("X: top-left for rectangles, center for circle (default 0)"),
          y: z
            .number()
            .optional()
            .describe("Y: top-left for rectangles, center for circle (default 0)"),
          unit: z.enum(["mm", "mil", "inch"]).optional().default("mm").describe("default mm"),
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
    "Add a text label to a PCB layer (e.g. silkscreen, fab, courtyard). Placed text is managed via list_shapes / edit_shape / delete_shape (kind=text).",
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
  // ------------------------------------------------------
  // Get Board 2D View Tool
  // ------------------------------------------------------
  server.tool(
    "get_board_2d_view",
    "Render a 2D image of the current PCB and return it as PNG, JPG or SVG, inline or as a file on disk.",
    {
      layers: z.array(z.string()).optional().describe("Layer names to include"),
      region: z
        .object({ x1: z.number(), y1: z.number(), x2: z.number(), y2: z.number() })
        .optional()
        .describe("Zoom to this board-space rectangle (mm) instead of the whole board"),
      width: z
        .number()
        .optional()
        .describe(
          "Image width in pixels (raster only); the board is fit within width x height, aspect-preserving",
        ),
      height: z.number().optional().describe("Image height in pixels (raster only)"),
      format: z.enum(["png", "jpg", "svg"]).optional().describe("Image format"),
      responseMode: z
        .enum(["inline", "file"])
        .optional()
        .describe(
          "inline (default): base64 imageData; file: writes <board>_2d_view.<ext> next to the .kicad_pcb, returns filePath — use for large boards (MCP size limits)",
        ),
      cropToBoard: z
        .boolean()
        .optional()
        .describe(
          "Crop to board content + margin (default true); false keeps the full plot canvas (e.g. to spot a stray outline)",
        ),
      cropMarginPx: z.number().optional().describe("Crop margin in pixels (default 20)."),
    },
    async ({ layers, region, width, height, format, responseMode, cropToBoard, cropMarginPx }) => {
      logger.debug("Getting 2D board view");
      const result = await callKicadScript("get_board_2d_view", {
        layers,
        region,
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
    "Import an SVG file as filled graphic polygons onto a PCB layer (default F.SilkS). Curves are linearised automatically. Ideal for logos.",
    {
      pcbPath: z.string().describe("Path to the .kicad_pcb file"),
      svgPath: z.string().describe("Path to the SVG logo file"),
      x: z.number().describe("X position of the logo top-left corner in mm"),
      y: z.number().describe("Y position of the logo top-left corner in mm"),
      width: z.number().describe("Target width in mm (height scales to preserve aspect ratio)"),
      layer: z.string().optional().describe("PCB layer name (default F.SilkS)"),
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
    "One-shot snapshot of the loaded PCB: components, tracks, zones, nets, layers in a single response — instead of separate list calls.",
    {},
    passthrough("get_pcb_overview"),
  );

  const originTypeSchema = z
    .enum(["grid", "drill", "aux"])
    .describe("'grid' = user grid origin; 'drill'/'aux' = drill/place origin for Gerber and PnP.");

  // Reading is the no-position call; writing requires an explicit
  // `position` so MCP clients can't accidentally snap the drill origin
  // to (0, 0) — which would silently invalidate every Gerber/PnP file
  // the user exports next. The backend also rejects missing coords as a
  // second line of defence for non-schema callers.
  server.tool(
    "board_origin",
    "Read or move the board's grid or drill/place origin (IPC-only). Without `position` returns the current origin; with `position` moves it. The drill origin is the coordinate zero of Gerber/PnP exports — set it to your reference point (board corner or fiducial) before exporting.",
    {
      type: originTypeSchema.optional().describe("Default 'drill'."),
      position: z
        .object({
          x: z.number(),
          y: z.number(),
          unit: z.enum(["mm", "inch"]).optional(),
        })
        .optional()
        .describe("Target coordinate — presence of this field switches the call to a write."),
      unit: z.enum(["mm", "inch"]).optional().describe("Read: coordinate unit (default mm)."),
    },
    async (args) => {
      if (args.position) {
        const { unit: _unit, ...params } = args;
        void _unit;
        return formatKicadResult(
          await callKicadScript("set_origin", { ...params, type: params.type ?? "drill" }),
        );
      }
      const { position: _p, ...params } = args;
      void _p;
      return formatKicadResult(await callKicadScript("get_origin", params));
    },
  );

  server.tool(
    "title_block",
    "Read or partial-update the board's title block (IPC-only) shown on plotted PDF / drawing-sheet output. No parameters = read current values. On update, omitted fields keep their value; pass an explicit empty string to clear.",
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
    async (args) => {
      const isWrite = Object.values(args).some((v) => v !== undefined);
      const command = isWrite ? "set_title_block_info" : "get_title_block_info";
      return formatKicadResult(await callKicadScript(command, isWrite ? args : {}));
    },
  );
}
