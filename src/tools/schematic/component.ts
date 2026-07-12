/**
 * Schematic Component CRUD and properties tools for KiCAD MCP server.
 * Split out of the former monolithic schematic.ts.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { CommandFunction, makePassthrough } from "../tool-response.js";

export function registerSchematicComponentTools(
  server: McpServer,
  callKicadScript: CommandFunction,
) {
  const passthrough = makePassthrough(callKicadScript);
  // Create schematic tool
  server.tool(
    "create_schematic",
    "Create a new schematic",
    {
      name: z.string().describe("Schematic name"),
      path: z.string().optional().describe("Optional path"),
      overwrite: z
        .boolean()
        .optional()
        .describe(
          "Replace an existing file. Default false: refuses (errorCode SCHEMATIC_EXISTS) if target exists.",
        ),
    },
    passthrough("create_schematic"),
  );

  // Add component to schematic
  server.tool(
    "add_schematic_component",
    "Add a component to the schematic. Symbol format is 'Library:SymbolName' (e.g. 'Device:R'). " +
      "Coordinates snap to the 1.27 mm grid by default (off-grid pins trigger ERC warnings); " +
      "response reports the landing position (.position) and a .snap delta when moved.",
    {
      schematicPath: z.string().describe("Path to the schematic file"),
      symbol: z.string().describe("Symbol library:name reference (e.g. Device:R)"),
      reference: z.string().describe("Component reference (e.g., R1, U1)"),
      value: z.string().optional().describe("Component value"),
      footprint: z
        .string()
        .optional()
        .describe("KiCAD footprint (e.g. Resistor_SMD:R_0603_1608Metric)"),
      position: z
        .object({
          x: z.number(),
          y: z.number(),
        })
        .optional()
        .describe("Position on schematic"),
      unit: z
        .number()
        .int()
        .min(1)
        .optional()
        .describe("Unit number for multi-unit symbols (1=A, 2=B, …). Default 1."),
      placeAllUnits: z
        .boolean()
        .optional()
        .describe(
          "Default false. true places EVERY unit of a multi-unit symbol in one call (stacked " +
            "vertically, sharing the reference); response reports each unit's position. Use this " +
            "for parts like MCUs whose power pins live on a separate unit.",
        ),
      snapToGrid: z
        .boolean()
        .optional()
        .describe("Default true. Pass false only for intentional sub-grid placement."),
      snapGridMm: z
        .number()
        .positive()
        .optional()
        .describe("Snap grid in mm (default 1.27; 2.54 = 100 mil is common for power rails)."),
    },
    async (args: {
      schematicPath: string;
      symbol: string;
      reference: string;
      value?: string;
      footprint?: string;
      position?: { x: number; y: number };
      unit?: number;
      placeAllUnits?: boolean;
      snapToGrid?: boolean;
      snapGridMm?: number;
    }) => {
      // Transform to what Python backend expects
      const [library, symbolName] = args.symbol.includes(":")
        ? args.symbol.split(":")
        : ["Device", args.symbol];

      const transformed = {
        schematicPath: args.schematicPath,
        snapToGrid: args.snapToGrid,
        snapGridMm: args.snapGridMm,
        placeAllUnits: args.placeAllUnits,
        component: {
          library,
          type: symbolName,
          reference: args.reference,
          value: args.value,
          footprint: args.footprint ?? "",
          // Python expects flat x, y not nested position
          x: args.position?.x ?? 0,
          y: args.position?.y ?? 0,
          unit: args.unit ?? 1,
          placeAllUnits: args.placeAllUnits,
        },
      };

      const result = await callKicadScript("add_schematic_component", transformed);
      if (result.success) {
        const pos = result.position;
        let text = `Successfully added ${args.reference} (${args.symbol}) to schematic`;
        if (pos) text += ` at (${pos.x}, ${pos.y})`;
        // Contract: report the .snap delta whenever coordinates moved so an
        // agent aiming at an exact point isn't silently relocated.
        if (result.snap?.applied) {
          const req = result.snap.requested;
          text += ` [snapped to ${result.snap.gridMm} mm grid from (${req?.x}, ${req?.y})]`;
        }
        // Multi-unit symbols (F1): surface the unit situation so the agent
        // never assumes one placement covered the whole part. Pins on an
        // unplaced unit have no location and can't be labeled/connected.
        if (result.units) {
          const u = result.units;
          text += `\nUnits: ${u.total} total, placed ${JSON.stringify(u.placed)}`;
          if (u.unplaced?.length) text += `, UNPLACED ${JSON.stringify(u.unplaced)}`;
          if (result.unitPositions)
            text += `\nUnit positions: ${JSON.stringify(result.unitPositions)}`;
          if (result.warning) text += `\nWARNING: ${result.warning}`;
          if (result.next) text += `\nNext: ${result.next}`;
        }
        // Append the raw position/snap blocks so structured consumers get them.
        text += `\n${JSON.stringify({ position: pos ?? null, snap: result.snap ?? null })}`;
        return {
          content: [{ type: "text" as const, text }],
          structuredContent: result,
        };
      } else {
        return {
          content: [
            {
              type: "text",
              text: `Failed to add component: ${result.message || JSON.stringify(result)}`,
            },
          ],
          isError: true,
        };
      }
    },
  );

  // Delete component from schematic
  server.tool(
    "delete_schematic_component",
    "Remove a placed symbol from a .kicad_sch schematic (keeps its lib_symbols definition). " +
      "To remove a PCB footprint use delete_component instead.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      reference: z
        .string()
        .describe("Reference designator of the component to remove (e.g. R1, U3)"),
    },
    async (args: { schematicPath: string; reference: string }) => {
      const result = await callKicadScript("delete_schematic_component", args);
      if (result.success) {
        return {
          content: [
            {
              type: "text",
              text: `Successfully removed ${args.reference} from schematic`,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text",
            text: `Failed to remove component: ${result.message || "Unknown error"}`,
          },
        ],
        isError: true,
      };
    },
  );

  // Edit component properties in schematic (footprint, value, reference, custom fields)
  server.tool(
    "edit_schematic_component",
    "Update a placed schematic symbol in place (keeps position and UUID): footprint, value, " +
      "reference, field-label positions, and custom properties (BOM/sourcing data like MPN, LCSC — " +
      "exported by export_bom). Batch multiple changes in one call. " +
      ".kicad_sch only — for a PCB footprint use edit_component.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      reference: z.string().describe("Current reference designator of the component (e.g. R1, U3)"),
      footprint: z
        .string()
        .optional()
        .describe("New KiCAD footprint string (e.g. Resistor_SMD:R_0603_1608Metric)"),
      value: z.string().optional().describe("New value string (e.g. 10k, 100nF)"),
      newReference: z
        .string()
        .optional()
        .describe("Rename the reference designator (e.g. R1 → R10)"),
      fieldPositions: z
        .record(
          z.object({
            x: z.number(),
            y: z.number(),
            angle: z.number().optional().default(0),
          }),
        )
        .optional()
        .describe(
          'Reposition field labels: map of field name to {x, y, angle} (e.g. {"Reference": {"x": 12.5, "y": 17.0}})',
        ),
      properties: z
        .record(
          z.union([
            z.string(),
            z.object({
              value: z.string().describe("Property value to write"),
              x: z.number().optional().describe("Label X position in mm (default: component X)"),
              y: z.number().optional().describe("Label Y position in mm (default: component Y)"),
              angle: z.number().optional().describe("Label rotation in degrees (default: 0)"),
              hide: z
                .boolean()
                .optional()
                .describe("Hide the label text (default true for new custom properties)"),
              fontSize: z
                .number()
                .optional()
                .describe("Font size in mm for the label (default: 1.27)"),
            }),
          ]),
        )
        .optional()
        .describe(
          "Add/update properties: map of name → string value or {value, x?, y?, angle?, hide?, fontSize?} " +
            'spec. E.g. {"MPN": "RC0603FR-0710KL", "Tolerance": "1%"}. Built-in fields work too.',
        ),
      removeProperties: z
        .array(z.string())
        .optional()
        .describe(
          "Custom property names to delete. Built-ins (Reference/Value/Footprint/Datasheet) " +
            'cannot be removed — set their value to "" instead.',
        ),
    },
    async (args: {
      schematicPath: string;
      reference: string;
      footprint?: string;
      value?: string;
      newReference?: string;
      fieldPositions?: Record<string, { x: number; y: number; angle?: number }>;
      properties?: Record<
        string,
        | string
        | {
            value: string;
            x?: number;
            y?: number;
            angle?: number;
            hide?: boolean;
            fontSize?: number;
          }
      >;
      removeProperties?: string[];
    }) => {
      const result = await callKicadScript("edit_schematic_component", args);
      if (result.success) {
        const updated = result.updated ?? {};
        const summaryParts: string[] = [];
        const simpleKeys = ["footprint", "value", "reference"] as const;
        for (const k of simpleKeys) {
          if (updated[k] !== undefined) summaryParts.push(`${k}=${updated[k]}`);
        }
        if (updated.fieldPositions)
          summaryParts.push(`fieldPositions=${Object.keys(updated.fieldPositions).join(",")}`);
        if (updated.propertiesAdded)
          summaryParts.push(`added=${Object.keys(updated.propertiesAdded).join(",")}`);
        if (updated.propertiesUpdated)
          summaryParts.push(`updated=${Object.keys(updated.propertiesUpdated).join(",")}`);
        if (updated.propertiesRemoved)
          summaryParts.push(`removed=${updated.propertiesRemoved.join(",")}`);
        return {
          content: [
            {
              type: "text" as const,
              text: `Successfully updated ${args.reference}: ${summaryParts.join("; ") || "(no-op)"}`,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text" as const,
            text: `Failed to edit component: ${result.message || "Unknown error"}`,
          },
        ],
        isError: true,
      };
    },
  );

  // Get component properties and field positions from schematic
  server.tool(
    "get_schematic_component",
    "Get a component's position plus every field's value and label position (built-in and " +
      "custom properties). Use before edit_schematic_component to inspect current state.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      reference: z.string().describe("Component reference designator (e.g. R1, U1)"),
    },
    async (args: { schematicPath: string; reference: string }) => {
      const result = await callKicadScript("get_schematic_component", args);
      if (result.success) {
        const pos = result.position
          ? `(${result.position.x}, ${result.position.y}, angle=${result.position.angle}°)`
          : "unknown";
        const fieldLines = Object.entries(result.fields ?? {}).map(
          ([name, f]: [string, any]) =>
            `  ${name}: "${f.value}" @ (${f.x}, ${f.y}, angle=${f.angle}°)`,
        );
        return {
          content: [
            {
              type: "text",
              text: `Component ${result.reference} at ${pos}\nFields:\n${fieldLines.join("\n")}`,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text",
            text: `Failed to get component: ${result.message || "Unknown error"}`,
          },
        ],
        isError: true,
      };
    },
  );

  // Move a placed symbol, dragging connected wires
  server.tool(
    "move_schematic_component",
    "Move a placed symbol. Wire endpoints touching its pins follow by default (preserveWires). " +
      "Coordinates snap to the 1.27 mm grid by default — off-grid placement triggers 'pin off-grid' ERC warnings.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      reference: z.string().describe("Reference designator (e.g., R1, U1)"),
      position: z
        .object({ x: z.number(), y: z.number() })
        .describe("New position in schematic mm coordinates"),
      preserveWires: z
        .boolean()
        .optional()
        .describe("Stretch connected wire endpoints to follow the move (default true)"),
      snapToGrid: z
        .boolean()
        .optional()
        .describe("Default true. Pass false only for intentional sub-grid placement."),
      snapGridMm: z.number().positive().optional().describe("Snap grid in mm (default 1.27)."),
    },
    async (args: {
      schematicPath: string;
      reference: string;
      position: { x: number; y: number };
      preserveWires?: boolean;
      snapToGrid?: boolean;
      snapGridMm?: number;
    }) => {
      const result = await callKicadScript("move_schematic_component", args);
      if (result.success) {
        const moved = result.wiresMoved ?? 0;
        const removed = result.wiresRemoved ?? 0;
        let text =
          `Moved ${args.reference} from (${result.oldPosition.x}, ${result.oldPosition.y}) ` +
          `to (${result.newPosition.x}, ${result.newPosition.y})` +
          (moved > 0 ? `, ${moved} wire endpoint(s) updated` : "") +
          (removed > 0 ? `, ${removed} zero-length wire(s) removed` : "");
        // Contract: surface the .snap block when the target coordinates moved.
        if (result.snap?.applied) {
          const req = result.snap.requested;
          text += ` [snapped to ${result.snap.gridMm} mm grid from (${req?.x}, ${req?.y})]`;
        }
        return {
          content: [{ type: "text" as const, text }],
          structuredContent: result,
        };
      }
      return {
        content: [
          {
            type: "text",
            text: `Failed to move component: ${result.message || "Unknown error"}`,
          },
        ],
        isError: true,
      };
    },
  );

  // Rotate schematic component
  server.tool(
    "rotate_schematic_component",
    "Rotate a placed symbol in the schematic.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      reference: z.string().describe("Reference designator (e.g., R1, U1)"),
      angle: z.number().describe("Rotation angle in degrees (0, 90, 180, 270)"),
      mirror: z.enum(["x", "y"]).optional().describe("Optional mirror axis"),
    },
    async (args: {
      schematicPath: string;
      reference: string;
      angle: number;
      mirror?: "x" | "y";
    }) => {
      const result = await callKicadScript("rotate_schematic_component", args);
      if (result.success) {
        return {
          content: [
            {
              type: "text",
              text: `Rotated ${args.reference} to ${args.angle}°${args.mirror ? ` (mirrored ${args.mirror})` : ""}`,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text",
            text: `Failed to rotate component: ${result.message || "Unknown error"}`,
          },
        ],
        isError: true,
      };
    },
  );

  // Annotate schematic
  server.tool(
    "annotate_schematic",
    "Assign reference designators to unannotated components (R? → R1, R2, ...). " +
      "No-op when all references are concrete (annotated: [], noop: true) — safe to call defensively.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
    },
    async (args: { schematicPath: string }) => {
      const result = await callKicadScript("annotate_schematic", args);
      if (result.success) {
        const annotated = result.annotated || [];
        if (annotated.length === 0) {
          return {
            content: [
              {
                type: "text",
                text:
                  "No components needed annotation — every symbol already has a " +
                  "concrete reference (no '?' placeholders found). This is the " +
                  "expected state when add_schematic_component was called with " +
                  "explicit references; you can drop annotate_schematic from this " +
                  "flow.",
              },
            ],
          };
        }
        const lines = annotated.map((a: any) => `  ${a.oldReference} → ${a.newReference}`);
        return {
          content: [
            {
              type: "text",
              text: `Annotated ${annotated.length} component(s):\n${lines.join("\n")}`,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text",
            text: `Failed to annotate: ${result.message || "Unknown error"}`,
          },
        ],
        isError: true,
      };
    },
  );
}
