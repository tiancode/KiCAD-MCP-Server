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
          "Replace an existing schematic file. Defaults to false: if the target .kicad_sch already exists, the tool refuses (errorCode SCHEMATIC_EXISTS) instead of overwriting it. Set true only when you intend to replace it.",
        ),
    },
    passthrough("create_schematic"),
  );

  // Add component to schematic
  server.tool(
    "add_schematic_component",
    "Add a component to the schematic. Symbol format is 'Library:SymbolName' (e.g. 'Device:R'). " +
      "Coordinates SNAP to KiCad's 1.27 mm grid BY DEFAULT (off-grid pins trigger ERC alignment warnings); " +
      "the response reports the landing position under .position plus a .snap delta when coordinates moved. " +
      "Pass snapToGrid: false only to reproduce an existing sub-grid placement.",
    {
      schematicPath: z.string().describe("Path to the schematic file"),
      symbol: z
        .string()
        .describe("Symbol library:name reference (e.g., Device:R, EDA-MCP:ESP32-C3)"),
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
        .describe("Unit number for multi-unit symbols (1=A, 2=B, 3=C, …). Defaults to 1."),
      snapToGrid: z
        .boolean()
        .optional()
        .describe(
          "Round the anchor onto the 1.27 mm schematic grid before placement. **Default true** — pass false only when sub-grid placement is intentional (the response surfaces the actual landing position either way).",
        ),
      snapGridMm: z
        .number()
        .positive()
        .optional()
        .describe(
          "Override the snap grid in mm. Default 1.27 mm matches KiCad's stock schematic grid; common alternatives are 2.54 mm (100 mil) for power rails.",
        ),
    },
    async (args: {
      schematicPath: string;
      symbol: string;
      reference: string;
      value?: string;
      footprint?: string;
      position?: { x: number; y: number };
      unit?: number;
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
        },
      };

      const result = await callKicadScript("add_schematic_component", transformed);
      if (result.success) {
        return {
          content: [
            {
              type: "text",
              text: `Successfully added ${args.reference} (${args.symbol}) to schematic`,
            },
          ],
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
    `Remove a placed symbol from a KiCAD schematic (.kicad_sch).

This removes the symbol instance (the placed component) from the schematic.
It does NOT remove the symbol definition from lib_symbols.

Note: This tool operates on schematic files (.kicad_sch).
To remove a footprint from a PCB, use delete_component instead.`,
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
    "Update a placed schematic symbol in place (keeps position and UUID — better than delete + re-add): " +
      "footprint, value, reference, field-label positions, and custom properties " +
      "(MPN, LCSC, ... — exported by export_bom; new properties default to hidden). " +
      "Batch footprint/value/newReference/fieldPositions/properties/removeProperties in one call. " +
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
                .describe(
                  "Whether to hide the property text on the schematic. Defaults to true for newly-created custom properties (BOM/sourcing data is normally hidden).",
                ),
              fontSize: z
                .number()
                .optional()
                .describe("Font size in mm for the label (default: 1.27)"),
            }),
          ]),
        )
        .optional()
        .describe(
          "Add or update component properties. Map of property name to either a string value (sensible defaults) " +
            "or a full spec object {value, x?, y?, angle?, hide?, fontSize?}. Use this to attach BOM and sourcing " +
            "metadata such as MPN, Manufacturer, Distributor, DigiKey, LCSC, JLCPCB_PN, Voltage, Tolerance, " +
            "Dielectric, Power, etc. Built-in fields (Reference, Value, Footprint, Datasheet) can also be set " +
            "this way but the dedicated parameters above are clearer. Example: " +
            '{"MPN": "RC0603FR-0710KL", "Manufacturer": "Yageo", "Tolerance": "1%"}',
        ),
      removeProperties: z
        .array(z.string())
        .optional()
        .describe(
          "List of custom property names to delete from this component. The built-in fields " +
            "Reference, Value, Footprint, and Datasheet cannot be removed (clear them by setting " +
            'value to "" instead). Example: ["OldMPN", "Distributor_PN"]',
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

  // ------------------------------------------------------------------
  // Single-property convenience tools (delegate to edit_schematic_component)
  // ------------------------------------------------------------------

  // Set a single custom property on a placed symbol
  server.tool(
    "set_schematic_component_property",
    "Add or update ONE custom property on a placed schematic symbol (created if missing) — " +
      "e.g. MPN, Manufacturer, LCSC, Voltage, or any BOM field. " +
      "Written as a standard KiCad property record: survives ERC, exported by export_bom. " +
      "New properties default to hidden (set hide=false to show on canvas). " +
      "For several properties at once use edit_schematic_component with `properties`.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      reference: z.string().describe("Reference designator of the component (e.g. R1, U3)"),
      name: z
        .string()
        .describe(
          "Property name (e.g. 'MPN', 'Manufacturer', 'DigiKey_PN', 'Voltage', 'Dielectric')",
        ),
      value: z.string().describe("Property value to write (use empty string to clear)"),
      x: z.number().optional().describe("Label X position in mm (default: component X)"),
      y: z.number().optional().describe("Label Y position in mm (default: component Y)"),
      angle: z.number().optional().describe("Label rotation in degrees (default: 0)"),
      hide: z
        .boolean()
        .optional()
        .describe(
          "Hide the property text on the schematic canvas. Defaults to true for newly-created custom properties.",
        ),
      fontSize: z.number().optional().describe("Font size in mm for the label (default: 1.27)"),
    },
    async (args: {
      schematicPath: string;
      reference: string;
      name: string;
      value: string;
      x?: number;
      y?: number;
      angle?: number;
      hide?: boolean;
      fontSize?: number;
    }) => {
      const result = await callKicadScript("set_schematic_component_property", args);
      if (result.success) {
        const updated = result.updated ?? {};
        const action = updated.propertiesAdded?.[args.name] !== undefined ? "added" : "updated";
        return {
          content: [
            {
              type: "text" as const,
              text: `Successfully ${action} property ${args.name}="${args.value}" on ${args.reference}`,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text" as const,
            text: `Failed to set property '${args.name}' on ${args.reference}: ${result.message || "Unknown error"}`,
          },
        ],
        isError: true,
      };
    },
  );

  // Remove a single custom property from a placed symbol
  server.tool(
    "remove_schematic_component_property",
    `Remove a single custom property from a placed schematic symbol.

Built-in fields (Reference, Value, Footprint, Datasheet) cannot be removed —
KiCad requires them on every symbol. To clear a built-in field, use
edit_schematic_component and set its value to an empty string.`,
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      reference: z.string().describe("Reference designator of the component (e.g. R1, U3)"),
      name: z
        .string()
        .describe("Custom property name to remove (e.g. 'MPN', 'Distributor_PN', 'OldField')"),
    },
    async (args: { schematicPath: string; reference: string; name: string }) => {
      const result = await callKicadScript("remove_schematic_component_property", args);
      if (result.success) {
        const removed = result.updated?.propertiesRemoved ?? [];
        if (removed.includes(args.name)) {
          return {
            content: [
              {
                type: "text" as const,
                text: `Successfully removed property '${args.name}' from ${args.reference}`,
              },
            ],
          };
        }
        return {
          content: [
            {
              type: "text" as const,
              text: `Property '${args.name}' was not present on ${args.reference} (no change made)`,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text" as const,
            text: `Failed to remove property '${args.name}' from ${args.reference}: ${result.message || "Unknown error"}`,
          },
        ],
        isError: true,
      };
    },
  );

  // Get component properties and field positions from schematic
  server.tool(
    "get_schematic_component",
    "Get full component info from a schematic: position plus EVERY field's value and label position " +
      "(built-in Reference/Value/Footprint/Datasheet and all custom BOM/sourcing properties). " +
      "Use before edit_schematic_component / set_schematic_component_property to inspect current state.",
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
    "Move a placed symbol. preserveWires (default true) stretches wire endpoints touching its pins to follow. Coordinates snap to KiCad's 1.27 mm grid by default — off-grid placement triggers 'pin off-grid' ERC warnings; pass snapToGrid:false to keep exact coordinates.",
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
        .describe(
          "Round the destination onto the 1.27 mm schematic grid before writing. **Default true** — pass false only when sub-grid placement is intentional.",
        ),
      snapGridMm: z
        .number()
        .positive()
        .optional()
        .describe(
          "Override the snap grid in mm. Default 1.27 mm matches KiCad's stock schematic grid.",
        ),
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
        return {
          content: [
            {
              type: "text",
              text:
                `Moved ${args.reference} from (${result.oldPosition.x}, ${result.oldPosition.y}) ` +
                `to (${result.newPosition.x}, ${result.newPosition.y})` +
                (moved > 0 ? `, ${moved} wire endpoint(s) updated` : "") +
                (removed > 0 ? `, ${removed} zero-length wire(s) removed` : ""),
            },
          ],
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
    "Assign reference designators to UNANNOTATED components (placeholder refs ending in '?': R? → R1, R2, ...). " +
      "No-op when every component already has a concrete reference (response: annotated: [], noop: true) — safe to call defensively.",
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
