/**
 * Schematic View, region analysis, text, and sheets tools for KiCAD MCP server.
 * Split out of the former monolithic schematic.ts.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

export function registerSchematicViewTools(server: McpServer, callKicadScript: Function) {
  // Get schematic view (rasterized image)
  server.tool(
    "get_schematic_view",
    "Return a rasterized image of the schematic (PNG by default, or SVG). Uses kicad-cli to export SVG, then converts to PNG via cairosvg. Use this for visual feedback after placing or wiring components.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      format: z.enum(["png", "svg"]).optional().describe("Output format (default: png)"),
      width: z.number().optional().describe("Image width in pixels (default: 1200)"),
      height: z.number().optional().describe("Image height in pixels (default: 900)"),
    },
    async (args: {
      schematicPath: string;
      format?: "png" | "svg";
      width?: number;
      height?: number;
    }) => {
      const result = await callKicadScript("get_schematic_view", args);
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

  // Get a zoomed view of a schematic region
  server.tool(
    "get_schematic_view_region",
    "Export a cropped region of the schematic as an image (PNG or SVG). Specify bounding box coordinates in schematic mm. Useful for zooming into a specific area to inspect wiring or layout.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch schematic file"),
      x1: z.number().describe("Left X coordinate of the region in mm"),
      y1: z.number().describe("Top Y coordinate of the region in mm"),
      x2: z.number().describe("Right X coordinate of the region in mm"),
      y2: z.number().describe("Bottom Y coordinate of the region in mm"),
      format: z.enum(["png", "svg"]).optional().describe("Output image format (default: png)"),
      width: z.number().optional().describe("Output image width in pixels (default: 800)"),
      height: z.number().optional().describe("Output image height in pixels (default: 600)"),
    },
    async (args: {
      schematicPath: string;
      x1: number;
      y1: number;
      x2: number;
      y2: number;
      format?: string;
      width?: number;
      height?: number;
    }) => {
      const result = await callKicadScript("get_schematic_view_region", args);
      if (result.success && result.imageData) {
        if (result.format === "svg") {
          return { content: [{ type: "text", text: result.imageData }] };
        }
        return {
          content: [
            {
              type: "image",
              data: result.imageData,
              mimeType: "image/png",
            },
          ],
        };
      }
      return {
        content: [{ type: "text", text: `Failed: ${result.message || "Unknown error"}` }],
        isError: true,
      };
    },
  );

  // Find overlapping elements
  server.tool(
    "find_overlapping_elements",
    "Detect spatially overlapping symbols, wires, and labels in the schematic. Finds duplicate power symbols at the same position, collinear overlapping wires, and labels stacked on top of each other.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch schematic file"),
      tolerance: z
        .number()
        .optional()
        .describe(
          "Distance threshold in mm for label proximity and wire collinearity checks. Symbol overlap uses bounding-box intersection. (default: 0.5)",
        ),
    },
    async (args: { schematicPath: string; tolerance?: number }) => {
      const result = await callKicadScript("find_overlapping_elements", args);
      if (result.success) {
        const lines = [`Found ${result.totalOverlaps} overlap(s):`];
        const syms: any[] = result.overlappingSymbols || [];
        const lbls: any[] = result.overlappingLabels || [];
        const wires: any[] = result.overlappingWires || [];
        if (syms.length) {
          lines.push(`\nOverlapping symbols (${syms.length}):`);
          syms.slice(0, 20).forEach((o: any) => {
            lines.push(
              `  ${o.element1.reference} ↔ ${o.element2.reference} (${o.distance}mm) [${o.type}]`,
            );
          });
        }
        if (lbls.length) {
          lines.push(`\nOverlapping labels (${lbls.length}):`);
          lbls.slice(0, 20).forEach((o: any) => {
            lines.push(`  "${o.element1.name}" ↔ "${o.element2.name}" (${o.distance}mm)`);
          });
        }
        if (wires.length) {
          lines.push(`\nOverlapping wires (${wires.length}):`);
          wires.slice(0, 20).forEach((o: any) => {
            lines.push(
              `  wire @ (${o.wire1.start.x},${o.wire1.start.y})→(${o.wire1.end.x},${o.wire1.end.y}) overlaps with another`,
            );
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

  // Get elements in a region
  server.tool(
    "get_elements_in_region",
    "List all symbols, wires, and labels within a rectangular region of the schematic. Useful for understanding what is in a specific area before modifying it.",
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

  // Find wires crossing symbols
  server.tool(
    "find_wires_crossing_symbols",
    "Find all wires that cross over component symbol bodies. Wires passing over symbols are unacceptable in schematics — they indicate routing mistakes where a wire was drawn across a component instead of around it.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch schematic file"),
    },
    async (args: { schematicPath: string }) => {
      const result = await callKicadScript("find_wires_crossing_symbols", args);
      if (result.success) {
        const collisions: any[] = result.collisions || [];
        const lines = [`Found ${collisions.length} wire(s) crossing symbols:`];
        collisions.slice(0, 30).forEach((c: any, i: number) => {
          lines.push(
            `  ${i + 1}. Wire (${c.wire.start.x},${c.wire.start.y})→(${c.wire.end.x},${c.wire.end.y}) crosses ${c.component.reference} (${c.component.libId})`,
          );
        });
        if (collisions.length > 30) lines.push(`  ... and ${collisions.length - 30} more`);
        return { content: [{ type: "text", text: lines.join("\n") }] };
      }
      return {
        content: [{ type: "text", text: `Failed: ${result.message || "Unknown error"}` }],
        isError: true,
      };
    },
  );

  // List floating net labels
  server.tool(
    "list_floating_labels",
    "Returns all net labels in the schematic that are not connected to any component pin. " +
      "A label is 'floating' when no component pin falls on the wire-network reachable from the " +
      "label's position. Floating labels indicate misplaced or off-grid labels that cause ERC errors. " +
      "Does not require the KiCAD UI to be running.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch schematic file"),
    },
    async (args: { schematicPath: string }) => {
      const result = await callKicadScript("list_floating_labels", args);
      if (result.success) {
        const labels: any[] = result.floating_labels || [];
        if (labels.length === 0) {
          return { content: [{ type: "text", text: "No floating labels found." }] };
        }
        const lines: string[] = [`Found ${labels.length} floating label(s):\n`];
        labels.slice(0, 50).forEach((lbl: any) => {
          lines.push(`  "${lbl.name}" (${lbl.type}) at (${lbl.x}, ${lbl.y})`);
        });
        if (labels.length > 50) {
          lines.push(`  ... and ${labels.length - 50} more`);
        }
        return { content: [{ type: "text", text: lines.join("\n") }] };
      }
      return {
        content: [{ type: "text", text: `Failed: ${result.message || "Unknown error"}` }],
        isError: true,
      };
    },
  );

  // Find orphaned wires
  server.tool(
    "find_orphaned_wires",
    "Find wire segments with at least one dangling endpoint — not connected to a component pin, " +
      "net label, or another wire. Orphaned wires cause ERC 'wire end unconnected' errors. " +
      "Does not require the KiCad UI to be running.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch schematic file"),
    },
    async (args: { schematicPath: string }) => {
      const result = await callKicadScript("find_orphaned_wires", args);
      if (result.success) {
        const wires: any[] = result.orphaned_wires || [];
        if (wires.length === 0) {
          return { content: [{ type: "text", text: "No orphaned wires found." }] };
        }
        const lines: string[] = [`Found ${wires.length} orphaned wire(s):\n`];
        wires.slice(0, 50).forEach((w: any) => {
          const dangling = w.dangling_ends.map((e: any) => `(${e.x}, ${e.y})`).join(", ");
          lines.push(
            `  wire (${w.start.x}, ${w.start.y})→(${w.end.x}, ${w.end.y})  dangling end(s): ${dangling}`,
          );
        });
        if (wires.length > 50) lines.push(`  ... and ${wires.length - 50} more`);
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
    "Snap schematic element coordinates to the nearest grid point. " +
      "KiCAD uses exact integer matching for connectivity, so off-grid coordinates cause wires " +
      "that look connected to fail ERC checks. " +
      "Modifies the .kicad_sch file in place. Does not require the KiCAD UI to be running.",
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
    "Returns the net name at a given (x, y) coordinate in a schematic, or null if no net label " +
      "or wire endpoint is present at that position. Faster than get_pin_net when you only need " +
      "the net name at a known coordinate and don't need pin traversal.",
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
    "Add a free-form text annotation to the schematic. " +
      "Use this to add notes, labels, section headings, or documentation strings " +
      "directly on the schematic canvas. Unlike net labels, text annotations have " +
      "no electrical significance.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      text: z.string().describe("Text content to display"),
      position: z
        .array(z.number())
        .length(2)
        .describe("Position [x, y] in schematic mm coordinates"),
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
      position: number[];
      angle?: number;
      fontSize?: number;
      bold?: boolean;
      italic?: boolean;
      justify?: "left" | "center" | "right";
    }) => {
      const result = await callKicadScript("add_schematic_text", args);
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
        };
      }
    },
  );

  // Add sheet pin to a sheet block on the parent schematic
  server.tool(
    "add_sheet_pin",
    "Add a pin to a sheet symbol block on the parent schematic. Sheet pins are the " +
      "parent-side connection points that correspond to hierarchical labels in the " +
      "sub-sheet. The pinName must exactly match a hierarchical_label in the sub-sheet.",
    {
      schematicPath: z.string().describe("Path to the PARENT .kicad_sch file"),
      sheetName: z
        .string()
        .describe("Sheet name as it appears in the Sheetname property (e.g. 'Storage')"),
      pinName: z.string().describe("Pin name — must match a hierarchical_label in the sub-sheet"),
      pinType: z
        .enum(["input", "output", "bidirectional"])
        .describe("Signal direction (should match the sub-sheet hierarchical label shape)"),
      position: z
        .array(z.number())
        .length(2)
        .describe("Pin position [x, y] in mm — must be on the sheet block boundary"),
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
      position: number[];
      orientation?: number;
    }) => {
      const result = await callKicadScript("add_sheet_pin", args);
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
      };
    },
  );

  // Create a hierarchical sheet box on the parent (root) schematic
  server.tool(
    "add_schematic_sheet",
    "Place a hierarchical sheet box on the parent (root) schematic referencing a sub-sheet .kicad_sch — " +
      "this is what makes a multi-page design exist (ERC/netlist/kicad-cli follow the Sheetfile reference). " +
      "Creates an empty sub-sheet file from the template if missing. " +
      "Inter-sheet connectivity is by same-named global labels on each page (add_schematic_net_label labelType=global_label); sheet pins are optional.",
    {
      schematicPath: z.string().describe("Path to the PARENT (root) .kicad_sch file"),
      sheetName: z.string().describe("Sheet name shown on the box (Sheetname property)"),
      sheetFile: z
        .string()
        .describe(
          "Sub-sheet .kicad_sch filename, relative to the parent's directory " +
            "(an absolute path is converted to relative). The Sheetfile reference.",
        ),
      position: z.array(z.number()).length(2).describe("Top-left corner [x, y] of the box in mm"),
      size: z
        .array(z.number())
        .length(2)
        .optional()
        .describe("Box [width, height] in mm (default [25.4, 25.4])"),
      pageNumber: z
        .union([z.string(), z.number()])
        .optional()
        .describe("Page number for this sheet (default: smallest unused page)"),
      createSubSheet: z
        .boolean()
        .optional()
        .describe(
          "Create an empty sub-sheet file from the template if it doesn't exist (default: true)",
        ),
    },
    async (args: {
      schematicPath: string;
      sheetName: string;
      sheetFile: string;
      position: number[];
      size?: number[];
      pageNumber?: string | number;
      createSubSheet?: boolean;
    }) => {
      const result = await callKicadScript("add_schematic_sheet", args);
      if (result.success) {
        return {
          content: [
            {
              type: "text" as const,
              text: result.message || `Added sheet '${args.sheetName}' -> ${args.sheetFile}`,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text" as const,
            text: `Failed to add sheet: ${result.message || "Unknown error"}`,
          },
        ],
      };
    },
  );
}
