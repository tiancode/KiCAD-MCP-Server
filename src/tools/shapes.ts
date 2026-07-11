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
 *   - For copper fills use add_copper_pour.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { logger } from "../logger.js";
import { CommandFunction, makePassthrough } from "./tool-response.js";

export function registerShapesTools(server: McpServer, callKicadScript: CommandFunction) {
  const passthrough = makePassthrough(callKicadScript);

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

  const bboxSchema = z
    .object({ x1: z.number(), y1: z.number(), x2: z.number(), y2: z.number() })
    .describe("Bounding box in mm — matches shapes whose extents overlap it");

  const kindSchema = z
    .enum(["segment", "arc", "circle", "rectangle", "polygon"])
    .describe("Shape kind filter");

  server.tool(
    "list_shapes",
    "List graphic shapes on the board (id, kind, layer, width, filled, bounding box) with optional layer / kind / boundingBox filters. Use this to find shape ids for delete_shape / edit_shape. IPC-only.",
    {
      layer: z.string().optional().describe("Filter by layer name (e.g. F.SilkS)"),
      kind: kindSchema.optional(),
      boundingBox: bboxSchema.optional(),
    },
    passthrough("list_shapes"),
  );

  server.tool(
    "delete_shape",
    "Delete graphic shape(s). Select by id/ids (from list_shapes) or by layer / kind / boundingBox filters; when filters match several shapes, pass all=true to delete every match (otherwise the call is refused with the candidate list). IPC-only.",
    {
      id: z.string().optional().describe("Single shape id (from list_shapes)"),
      ids: z.array(z.string()).optional().describe("Multiple shape ids"),
      layer: z.string().optional().describe("Filter: shapes on this layer"),
      kind: kindSchema.optional(),
      boundingBox: bboxSchema.optional(),
      all: z
        .boolean()
        .optional()
        .describe("Delete every filter match (default false: refuse on multiple)"),
    },
    passthrough("delete_shape"),
  );

  server.tool(
    "edit_shape",
    "Edit one graphic shape (by id from list_shapes): move it by dx/dy, change layer, stroke width, or fill. IPC-only.",
    {
      id: z.string().describe("Shape id (from list_shapes)"),
      newLayer: z.string().optional().describe("Move the shape to this layer"),
      width: z.number().optional().describe("New stroke width in mm"),
      filled: z.boolean().optional().describe("New fill state"),
      move: z
        .object({ dx: z.number(), dy: z.number() })
        .optional()
        .describe("Translate the shape by dx/dy mm"),
    },
    passthrough("edit_shape"),
  );

  logger.info("Generic drawing-primitive tools registered (8 shape tools)");
}
