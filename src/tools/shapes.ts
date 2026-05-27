/**
 * Generic drawing-primitive tools (IPC-only).
 *
 * Graphic shapes on any layer — silkscreen, fab notes, courtyard,
 * Edge.Cuts cutouts, User.* layers, etc.
 *
 * Naming distinction vs. routing:
 *   - add_segment / add_arc here are *graphic* shapes (no net binding).
 *   - For copper traces use route_trace / route_arc_trace (those bind a
 *     net and route through the autorouter primitives).
 *   - For copper fills use add_zone.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { logger } from "../logger.js";
import { passthroughCall } from "./tool-response.js";

export function registerShapesTools(server: McpServer, callKicadScript: Function) {
  const passthrough = (command: string) =>
    passthroughCall(callKicadScript as Parameters<typeof passthroughCall>[0], command);

  const xySchema = z.object({ x: z.number(), y: z.number() });

  const commonStrokeFields = {
    width: z
      .number()
      .optional()
      .describe("Stroke width in mm (default 0.15). Ignored when filled=true."),
    layer: z
      .string()
      .optional()
      .describe(
        "Layer name (default F.SilkS). Any KiCad layer works: F.Cu, B.Cu, F.SilkS, B.SilkS, F.Fab, B.Fab, F.CrtYd, B.CrtYd, Edge.Cuts, Cmts.User, Dwgs.User, User.1 … User.9.",
      ),
  };

  server.tool(
    "add_segment",
    "Draw a graphic line (no net) on any layer. For copper traces with a net, use route_trace instead. IPC-only.",
    {
      start: xySchema.describe("Start point in mm"),
      end: xySchema.describe("End point in mm"),
      ...commonStrokeFields,
    },
    passthrough("add_segment"),
  );

  server.tool(
    "add_arc",
    "Draw a graphic arc through three points (start → mid → end) on any layer. For copper arc traces with a net, use route_arc_trace instead. IPC-only.",
    {
      start: xySchema.describe("Start point in mm"),
      mid: xySchema.describe("Mid point in mm (defines curvature)"),
      end: xySchema.describe("End point in mm"),
      ...commonStrokeFields,
    },
    passthrough("add_arc"),
  );

  server.tool(
    "add_circle",
    "Draw a graphic circle on any layer. filled=true produces a solid disc; filled=false produces a stroked ring of the given width. IPC-only.",
    {
      center: xySchema.describe("Center point in mm"),
      radius: z.number().describe("Radius in mm"),
      filled: z
        .boolean()
        .optional()
        .describe("Fill interior solid (default false — stroked outline only)"),
      ...commonStrokeFields,
    },
    passthrough("add_circle"),
  );

  server.tool(
    "add_rectangle",
    "Draw an axis-aligned graphic rectangle on any layer. filled=true produces a solid box; filled=false produces a stroked outline. IPC-only.",
    {
      topLeft: xySchema.describe("Top-left corner in mm"),
      bottomRight: xySchema.describe("Bottom-right corner in mm"),
      filled: z
        .boolean()
        .optional()
        .describe("Fill interior solid (default false — stroked outline only)"),
      ...commonStrokeFields,
    },
    passthrough("add_rectangle"),
  );

  server.tool(
    "add_polygon",
    "Draw a closed graphic polygon on any layer from ≥3 points. filled=true produces a solid fill (great for logos / pads); filled=false produces a stroked outline. IPC-only.",
    {
      points: z
        .array(xySchema)
        .min(3)
        .describe("Polygon vertices in mm — minimum 3. Polygon is auto-closed."),
      filled: z
        .boolean()
        .optional()
        .describe("Fill interior solid (default false — stroked outline only)"),
      ...commonStrokeFields,
    },
    passthrough("add_polygon"),
  );

  logger.info("Generic drawing-primitive tools registered (5 shape tools)");
}
