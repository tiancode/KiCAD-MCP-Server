/**
 * Schematic View, region analysis, text, and sheets tools for KiCAD MCP server.
 * Split out of the former monolithic schematic.ts.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import {
  CommandFunction,
  formatKicadResult,
  toXyObject,
  toXyTuple,
  XY_POINT_FORMS,
  xyPointSchema,
  XyPointInput,
} from "../tool-response.js";

export function registerSchematicViewTools(server: McpServer, callKicadScript: CommandFunction) {
  // Get schematic view (rasterized image)
  server.tool(
    "get_schematic_view",
    "Render the schematic as an image (PNG by default, or SVG), optionally cropped to a region in schematic mm. Use for visual feedback after placing/wiring, or to zoom into an area.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      format: z.enum(["png", "svg"]).optional().describe("Output format (default: png)"),
      width: z.number().optional().describe("Image width in pixels (default: 1200)"),
      height: z.number().optional().describe("Image height in pixels (default: 900)"),
      region: z
        .object({
          x1: z.number().describe("Left X in mm"),
          y1: z.number().describe("Top Y in mm"),
          x2: z.number().describe("Right X in mm"),
          y2: z.number().describe("Bottom Y in mm"),
        })
        .optional()
        .describe("Crop to this bounding box in schematic mm instead of the full page"),
    },
    async (args: {
      schematicPath: string;
      format?: "png" | "svg";
      width?: number;
      height?: number;
      region?: { x1: number; y1: number; x2: number; y2: number };
    }) => {
      const { region, ...rest } = args;
      const result = region
        ? await callKicadScript("get_schematic_view_region", { ...rest, ...region })
        : await callKicadScript("get_schematic_view", rest);
      if (result.success) {
        if (result.format === "svg") {
          const parts: { type: "text"; text: string }[] = [];
          if (result.message) {
            parts.push({ type: "text", text: result.message });
          }
          parts.push({
            type: "text",
            text: result.imageData || "",
          });
          return { content: parts };
        }
        // PNG — return as base64 image
        return {
          content: [
            {
              type: "image" as const,
              data: result.imageData,
              mimeType: "image/png",
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text",
            text: `Failed to get schematic view: ${result.message || "Unknown error"}`,
          },
        ],
        isError: true,
      };
    },
  );

  // ============================================================
  // Schematic Analysis Tools (read-only)
  // ============================================================
  // (find_overlapping_elements / find_wires_crossing_symbols /
  //  list_floating_labels / find_orphaned_wires are exposed through the
  //  merged check_schematic_layout tool in query.ts.)

  // Get elements in a region
  server.tool(
    "get_elements_in_region",
    "List all symbols, wires, and labels within a rectangular region of the schematic. Useful before modifying an area.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch schematic file"),
      x1: z.number().describe("Left X coordinate of the region in mm"),
      y1: z.number().describe("Top Y coordinate of the region in mm"),
      x2: z.number().describe("Right X coordinate of the region in mm"),
      y2: z.number().describe("Bottom Y coordinate of the region in mm"),
    },
    async (args: { schematicPath: string; x1: number; y1: number; x2: number; y2: number }) => {
      const result = await callKicadScript("get_elements_in_region", args);
      if (result.success) {
        const c = result.counts;
        const lines = [
          `Region (${args.x1},${args.y1})→(${args.x2},${args.y2}): ${c.symbols} symbols, ${c.wires} wires, ${c.labels} labels`,
        ];
        const syms: any[] = result.symbols || [];
        if (syms.length) {
          lines.push("\nSymbols:");
          syms.forEach((s: any) => {
            const pinCount = s.pins ? Object.keys(s.pins).length : 0;
            lines.push(
              `  ${s.reference} (${s.libId}) @ (${s.position.x}, ${s.position.y}) [${pinCount} pins]`,
            );
          });
        }
        const wires: any[] = result.wires || [];
        if (wires.length) {
          lines.push(`\nWires (${wires.length}):`);
          wires.slice(0, 30).forEach((w: any) => {
            lines.push(`  (${w.start.x},${w.start.y}) → (${w.end.x},${w.end.y})`);
          });
          if (wires.length > 30) lines.push(`  ... and ${wires.length - 30} more`);
        }
        const labels: any[] = result.labels || [];
        if (labels.length) {
          lines.push(`\nLabels (${labels.length}):`);
          labels.forEach((l: any) => {
            lines.push(`  "${l.name}" [${l.type}] @ (${l.position.x}, ${l.position.y})`);
          });
        }
        return { content: [{ type: "text", text: lines.join("\n") }] };
      }
      return {
        content: [{ type: "text", text: `Failed: ${result.message || "Unknown error"}` }],
        isError: true,
      };
    },
  );

  // Snap schematic elements to grid
  server.tool(
    "snap_to_grid",
    "Snap schematic element coordinates to the nearest grid point. KiCAD connectivity uses exact " +
      "integer matching, so off-grid coordinates make wires that look connected fail ERC. " +
      "Modifies the .kicad_sch in place.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch schematic file"),
      gridSize: z
        .number()
        .optional()
        .describe("Grid spacing in mm (default: 2.54 — standard KiCAD schematic grid)"),
      elements: z
        .array(z.enum(["wires", "junctions", "labels", "components"]))
        .optional()
        .describe(
          'Element types to snap (default: ["wires", "junctions", "labels"]). ' +
            '"components" is opt-in — moving a component without re-routing wires creates new mismatches.',
        ),
    },
    async (args: { schematicPath: string; gridSize?: number; elements?: string[] }) => {
      const result = await callKicadScript("snap_to_grid", args);
      if (result.success) {
        return { content: [{ type: "text", text: result.message }] };
      }
      return {
        content: [{ type: "text", text: `Failed: ${result.message || "Unknown error"}` }],
        isError: true,
      };
    },
  );

  server.tool(
    "get_net_at_point",
    "Return the net name at (x, y), or null if no net label or wire endpoint is at that position.",
    {
      schematicPath: z.string().describe("Path to the schematic file (.kicad_sch)"),
      x: z.number().describe("X coordinate in mm"),
      y: z.number().describe("Y coordinate in mm"),
    },
    async (args: { schematicPath: string; x: number; y: number }) => {
      const result = await callKicadScript("get_net_at_point", args);
      if (result.success) {
        const netName = result.net_name ?? null;
        const source = result.source ?? null;
        const pos = result.position;
        return {
          content: [
            {
              type: "text",
              text:
                `Net at (${pos?.x ?? args.x}, ${pos?.y ?? args.y}): ` +
                (netName !== null ? netName : "(none)") +
                (source ? ` [source: ${source}]` : ""),
            },
          ],
        };
      } else {
        return {
          content: [
            {
              type: "text",
              text: `Failed to get net at point: ${result.message || "Unknown error"}`,
            },
          ],
          isError: true,
        };
      }
    },
  );

  // Add free-form text annotation to schematic
  server.tool(
    "add_schematic_text",
    "Add a free-form text annotation (notes, section headings, docs) to the schematic canvas. " +
      "Unlike net labels, it has no electrical significance.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      text: z.string().describe("Text content to display"),
      position: xyPointSchema.describe(
        `Position in schematic mm coordinates. ${XY_POINT_FORMS}`,
      ),
      angle: z.number().optional().describe("Rotation angle in degrees (default: 0)"),
      fontSize: z.number().optional().describe("Font size in mm (default: 1.27)"),
      bold: z.boolean().optional().describe("Bold text (default: false)"),
      italic: z.boolean().optional().describe("Italic text (default: false)"),
      justify: z
        .enum(["left", "center", "right"])
        .optional()
        .describe("Horizontal text justification (default: left)"),
    },
    async (args: {
      schematicPath: string;
      text: string;
      position: XyPointInput;
      angle?: number;
      fontSize?: number;
      bold?: boolean;
      italic?: boolean;
      justify?: "left" | "center" | "right";
    }) => {
      // Accept both {x,y} and [x,y] for position (S12); Python expects [x,y].
      const result = await callKicadScript("add_schematic_text", {
        ...args,
        position: toXyTuple(args.position),
      });
      if (result.success) {
        return {
          content: [
            {
              type: "text" as const,
              text: result.message || "Text annotation added successfully",
            },
          ],
        };
      } else {
        return {
          content: [
            {
              type: "text" as const,
              text: `Failed to add text annotation: ${result.message || "Unknown error"}`,
            },
          ],
          isError: true,
        };
      }
    },
  );

  // Create a hierarchical sheet on the parent schematic
  server.tool(
    "create_hierarchical_sheet",
    "Create a hierarchical sheet in a parent schematic: inserts the sheet block and creates the child " +
      ".kicad_sch if missing. Optional pins are auto-stacked on the requested side, each writing a matching " +
      "hierarchical_label into the child (for explicit pin positions use add_sheet_pin). " +
      "pageNumber pins the child's page number and cannot be combined with pins. " +
      "Inter-sheet nets connect via same-named global labels on each page — sheet pins are optional.",
    {
      schematicPath: z.string().describe("Path to the parent .kicad_sch file"),
      sheetName: z.string().describe("Sheet name (must be unique in the parent)"),
      childFilename: z
        .string()
        .describe("Child filename relative to the parent's directory, e.g. 'power.kicad_sch'"),
      position: xyPointSchema
        .optional()
        .describe(`Sheet top-left position in mm (default 100, 50). ${XY_POINT_FORMS}`),
      size: z
        .object({ width: z.number(), height: z.number() })
        .optional()
        .describe("Sheet size in mm (default 50 x 40)"),
      createChild: z
        .boolean()
        .optional()
        .describe("Create the child file when it doesn't exist (default true)"),
      pins: z
        .array(
          z.object({
            name: z.string().describe("Pin / hierarchical label name"),
            shape: z
              .enum(["input", "output", "bidirectional", "tri_state", "passive"])
              .optional()
              .describe("Pin shape (default bidirectional)"),
            side: z
              .enum(["left", "right", "top", "bottom"])
              .optional()
              .describe("Sheet border to place the pin on (default left, auto-stacked)"),
            addChildLabel: z
              .boolean()
              .optional()
              .describe("Also add the matching hierarchical_label in the child (default true)"),
          }),
        )
        .optional()
        .describe("Sheet pins to author in the same call"),
      pageNumber: z
        .union([z.string(), z.number()])
        .optional()
        .describe("Explicit page number for the child sheet (default: smallest unused)."),
    },
    async (args) => {
      // Accept both {x,y} and [x,y] for position (S12); normalize once.
      const positionObj = args.position !== undefined ? toXyObject(args.position) : undefined;
      if (args.pageNumber === undefined) {
        const { pageNumber: _pn, position: _pos, ...params } = args;
        void _pn;
        void _pos;
        return formatKicadResult(
          await callKicadScript("create_hierarchical_sheet", {
            ...params,
            ...(positionObj !== undefined ? { position: positionObj } : {}),
          }),
        );
      }
      if (args.pins?.length) {
        return formatKicadResult({
          success: false,
          message: "pageNumber cannot be combined with pins",
          hint: "Create the sheet with pageNumber first, then add pins with add_sheet_pin.",
        });
      }
      const { schematicPath, sheetName, childFilename, size, createChild, pageNumber } = args;
      return formatKicadResult(
        await callKicadScript("add_schematic_sheet", {
          schematicPath,
          sheetName,
          sheetFile: childFilename,
          position: [positionObj?.x ?? 100, positionObj?.y ?? 50],
          ...(size ? { size: [size.width, size.height] } : {}),
          pageNumber,
          ...(createChild !== undefined ? { createSubSheet: createChild } : {}),
        }),
      );
    },
  );

  server.tool(
    "add_sheet_pin",
    "Add a pin to a sheet symbol block on the parent schematic — the parent-side connection point. " +
      "pinName must exactly match a hierarchical_label in the sub-sheet.",
    {
      schematicPath: z.string().describe("Path to the PARENT .kicad_sch file"),
      sheetName: z
        .string()
        .describe("Sheet name as it appears in the Sheetname property (e.g. 'Storage')"),
      pinName: z.string().describe("Pin name"),
      pinType: z
        .enum(["input", "output", "bidirectional"])
        .describe("Signal direction (should match the sub-sheet hierarchical label shape)"),
      position: xyPointSchema.describe(
        `Pin position in mm — must be on the sheet block boundary. ${XY_POINT_FORMS}`,
      ),
      orientation: z
        .number()
        .optional()
        .describe("Pin orientation: 0=right edge of sheet box, 180=left edge (default: 0)"),
    },
    async (args: {
      schematicPath: string;
      sheetName: string;
      pinName: string;
      pinType: "input" | "output" | "bidirectional";
      position: XyPointInput;
      orientation?: number;
    }) => {
      // Accept both {x,y} and [x,y] for position (S12); Python expects [x,y].
      const result = await callKicadScript("add_sheet_pin", {
        ...args,
        position: toXyTuple(args.position),
      });
      if (result.success) {
        return {
          content: [
            {
              type: "text" as const,
              text:
                result.message || `Added sheet pin '${args.pinName}' to sheet '${args.sheetName}'`,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text" as const,
            text: `Failed to add sheet pin: ${result.message || "Unknown error"}`,
          },
        ],
        isError: true,
      };
    },
  );
}
