/**
 * Generic drawing-primitive tools (IPC-only).
 *
 * Graphic shapes on any layer — silkscreen, fab notes, courtyard,
 * Edge.Cuts cutouts, User.* layers, etc.
 *
 * Naming distinction vs. routing:
 *   - add_shape (kind=segment/arc/…) draws *graphic* shapes (no net binding).
 *   - For copper traces use route_trace (those bind a
 *     net and route through the autorouter primitives).
 *   - For copper fills use add_copper_pour.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { logger } from "../logger.js";
import { CommandFunction, formatKicadResult, makePassthrough } from "./tool-response.js";

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
      .describe("Any KiCad layer name (default F.SilkS), e.g. F.Cu, F.Fab, Edge.Cuts, User.1."),
  };

  server.tool(
    "add_shape",
    "Draw a graphic shape (no net) on any layer: segment, arc, circle, rectangle, or polygon. For copper traces use route_trace instead. IPC-only.",
    {
      kind: z
        .enum(["segment", "arc", "circle", "rectangle", "polygon"])
        .describe(
          "kind: required fields — segment: start,end; arc: start,mid,end; circle: center,radius; rectangle: topLeft,bottomRight; polygon: points",
        ),
      start: xySchema.optional().describe("Start point in mm (segment, arc)"),
      end: xySchema.optional().describe("End point in mm (segment, arc)"),
      mid: xySchema.optional().describe("Mid point in mm — defines curvature (arc)"),
      center: xySchema.optional().describe("Center point in mm (circle)"),
      radius: z.number().optional().describe("Radius in mm (circle)"),
      topLeft: xySchema.optional().describe("Top-left corner in mm (rectangle)"),
      bottomRight: xySchema.optional().describe("Bottom-right corner in mm (rectangle)"),
      points: z
        .array(xySchema)
        .min(3)
        .optional()
        .describe("Polygon vertices in mm (min 3, auto-closed)"),
      filled: z
        .boolean()
        .optional()
        .describe("Fill solid (default false: stroked outline); circle/rectangle/polygon only"),
      ...commonStrokeFields,
    },
    async (
      args: { kind: "segment" | "arc" | "circle" | "rectangle" | "polygon" } & Record<
        string,
        unknown
      >,
    ) => {
      const { kind, ...params } = args;
      // The python handlers substitute silent defaults (origin, 1mm radius)
      // for missing coordinates instead of erroring — enforce the per-kind
      // required fields here, as the pre-merge per-shape schemas did.
      const REQUIRED: Record<typeof kind, string[]> = {
        segment: ["start", "end"],
        arc: ["start", "mid", "end"],
        circle: ["center", "radius"],
        rectangle: ["topLeft", "bottomRight"],
        polygon: ["points"],
      };
      const missing = REQUIRED[kind].filter((field) => params[field] === undefined);
      if (missing.length > 0) {
        return formatKicadResult({
          success: false,
          message: `add_shape kind=${kind} requires: ${missing.join(", ")}`,
        });
      }
      const result = await callKicadScript(`add_${kind}`, params);
      return formatKicadResult(result);
    },
  );

  const bboxSchema = z
    .object({ x1: z.number(), y1: z.number(), x2: z.number(), y2: z.number() })
    .describe("Bounding box in mm — matches shapes whose extents overlap it");

  const kindSchema = z
    .enum(["segment", "arc", "circle", "rectangle", "polygon", "text", "textbox"])
    .describe("Shape/text kind filter");

  server.tool(
    "list_shapes",
    "List graphic shapes AND board text (id, kind, layer; text items add text/position/size) with optional layer/kind/boundingBox filters — source of ids for delete_shape/edit_shape. IPC-only.",
    {
      layer: z.string().optional().describe("Filter by layer name (e.g. F.SilkS)"),
      kind: kindSchema.optional(),
      boundingBox: bboxSchema.optional(),
    },
    passthrough("list_shapes"),
  );

  server.tool(
    "delete_shape",
    "Delete graphic shape(s) or board text by id/ids (from list_shapes) or by layer/kind/boundingBox filters. IPC-only.",
    {
      id: z.string().optional().describe("Single shape id (from list_shapes)"),
      ids: z.array(z.string()).optional().describe("Multiple shape ids"),
      layer: z.string().optional().describe("Filter: shapes on this layer"),
      kind: kindSchema.optional(),
      boundingBox: bboxSchema.optional(),
      all: z
        .boolean()
        .optional()
        .describe("Delete every filter match (default false: refused with the candidate list)"),
    },
    passthrough("delete_shape"),
  );

  server.tool(
    "edit_shape",
    "Edit one graphic shape or board text (by id from list_shapes): move by dx/dy, change layer; shapes: stroke width/fill; text: content/size. Inapplicable properties come back in `unsupported`. IPC-only.",
    {
      id: z.string().describe("Shape id (from list_shapes)"),
      newLayer: z.string().optional().describe("Move the shape to this layer"),
      width: z.number().optional().describe("New stroke width in mm"),
      filled: z.boolean().optional().describe("New fill state (shapes only)"),
      move: z
        .object({ dx: z.number(), dy: z.number() })
        .optional()
        .describe("Translate the shape by dx/dy mm"),
      text: z.string().optional().describe("New text content (text items only)"),
      size: z.number().optional().describe("New glyph size in mm (text items only)"),
    },
    passthrough("edit_shape"),
  );

  logger.info("Generic drawing-primitive tools registered (4 shape tools)");
}
