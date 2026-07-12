/**
 * Schematic Wires, net labels, and connections tools for KiCAD MCP server.
 * Split out of the former monolithic schematic.ts.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { CommandFunction, formatKicadResult } from "../tool-response.js";

export function registerSchematicWireTools(server: McpServer, callKicadScript: CommandFunction) {
  // Draw wire between coordinate waypoints with optional pin snapping
  server.tool(
    "add_schematic_wire",
    "Draw a wire between two or more points. Call get_schematic_pin_locations first for pin coordinates, then pass them as the first/last waypoints. snapToPins (default on) snaps endpoints to the nearest exact pin coordinate. Add intermediate waypoints to route around parts, e.g. [[x1,y1],[xMid,y1],[xMid,y2],[x2,y2]] goes horizontal then vertical; intermediate waypoints are never snapped.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      waypoints: z
        .array(z.array(z.number()).length(2))
        .min(2)
        .describe("Ordered list of [x, y] coordinates. Minimum 2 points."),
      snapToPins: z
        .boolean()
        .optional()
        .describe("Snap the first and last waypoints to the nearest pin (default: true)"),
      snapTolerance: z.number().optional().describe("Maximum snap distance in mm (default: 1.0)"),
    },
    async (args: {
      schematicPath: string;
      waypoints: number[][];
      snapToPins?: boolean;
      snapTolerance?: number;
    }) => {
      const result = await callKicadScript("add_schematic_wire", args);
      if (result.success) {
        return {
          content: [
            {
              type: "text" as const,
              text: result.message || "Wire added successfully",
            },
          ],
        };
      } else {
        return {
          content: [
            {
              type: "text" as const,
              text: `Failed to add wire: ${result.message || "Unknown error"}`,
            },
          ],
          isError: true,
        };
      }
    },
  );

  // Add net label
  server.tool(
    "add_schematic_net_label",
    "Add a net label. KiCad connects a label to a pin ONLY at the exact pin endpoint (0.01 mm off breaks it). " +
      "Modes: (1) PREFERRED componentRef + pinNumber — snaps to the pin; " +
      "(2) position [x, y] — auto-snaps to any pin within snapTolerance mm (default 0.05); " +
      "(3) snapTolerance: 0 — no snapping (labels between pins). " +
      "Response reports connected_to_pin = {ref, pin} | null; auto-snap adds snapped_to_pin.",
    {
      schematicPath: z.string().describe("Path to the schematic file"),
      netName: z.string().describe("Name of the net (e.g., VCC, GND, SIGNAL_1)"),
      position: z
        .array(z.number())
        .length(2)
        .optional()
        .describe(
          "Position [x, y] for the label. Required when componentRef/pinNumber are not given.",
        ),
      componentRef: z
        .string()
        .optional()
        .describe("Component reference to snap label to (e.g. U1, R1). Use with pinNumber."),
      pinNumber: z
        .union([z.string(), z.number()])
        .optional()
        .describe(
          "Pin number or name on componentRef to snap label to (e.g. '1', 'GND'). Use with componentRef.",
        ),
      labelType: z
        .enum(["label", "global_label", "hierarchical_label"])
        .optional()
        .describe("Label type (default: label)"),
      orientation: z.number().optional().describe("Rotation angle 0/90/180/270 (default: 0)"),
      snapTolerance: z
        .number()
        .optional()
        .describe(
          "Auto-snap radius in mm when a raw position is given (default 0.05 — only catches float near-misses). Pass 0 to disable.",
        ),
    },
    async (args: {
      schematicPath: string;
      netName: string;
      position?: number[];
      componentRef?: string;
      pinNumber?: string | number;
      labelType?: string;
      orientation?: number;
      snapTolerance?: number;
    }) => {
      const result = await callKicadScript("add_schematic_net_label", args);
      if (result.success) {
        return formatKicadResult(result);
      } else {
        return {
          content: [
            {
              type: "text",
              text: `Failed to add net label: ${result.message || "Unknown error"}`,
            },
          ],
          isError: true,
        };
      }
    },
  );

  // Add or remove a no-connect flag — replaces add_no_connect and
  // delete_no_connect. Dispatches to the original python commands.
  server.tool(
    "set_no_connect",
    "Add a no-connect flag (X marker) to a pin intentionally left unconnected, suppressing ERC 'Pin not connected' errors — or pass remove=true to delete an existing flag. " +
      "PREFERRED: supply componentRef + pinNumber to snap to the exact pin endpoint; alternatively supply position [x, y] in mm matching the pin endpoint exactly.",
    {
      schematicPath: z.string().describe("Path to the schematic file"),
      position: z
        .array(z.number())
        .length(2)
        .optional()
        .describe("Position [x, y] in mm. Required when componentRef/pinNumber are not given."),
      componentRef: z
        .string()
        .optional()
        .describe("Component reference to snap to (e.g. U1, R1). Use with pinNumber."),
      pinNumber: z
        .union([z.string(), z.number()])
        .optional()
        .describe("Pin number or name on componentRef (e.g. '1', 'GND'). Use with componentRef."),
      remove: z
        .boolean()
        .optional()
        .describe("true removes an existing no-connect flag instead of adding one."),
      tolerance: z
        .number()
        .optional()
        .describe("Only with remove=true: coordinate match tolerance in mm (default 0.5)."),
    },
    async (args: {
      schematicPath: string;
      position?: number[];
      componentRef?: string;
      pinNumber?: string | number;
      remove?: boolean;
      tolerance?: number;
    }) => {
      const { remove, ...params } = args;
      const command = remove === true ? "delete_no_connect" : "add_no_connect";
      return formatKicadResult(await callKicadScript(command, params));
    },
  );

  // Connect pin to net
  server.tool(
    "connect_to_net",
    "Connect a component pin to a named net by adding a wire stub and net label at the exact pin endpoint. " +
      "The response includes pin_location (exact pin coords), label_location (where the label was placed), " +
      "and wire_stub (the wire segment added) so you can confirm the placement.",
    {
      schematicPath: z.string().describe("Path to the schematic file"),
      componentRef: z.string().describe("Component reference (e.g., U1, R1)"),
      pinName: z.string().describe("Pin name/number to connect"),
      netName: z.string().describe("Name of the net to connect to"),
    },
    async (args: {
      schematicPath: string;
      componentRef: string;
      pinName: string;
      netName: string;
    }) => {
      const result = await callKicadScript("connect_to_net", args);
      if (result.success) {
        return formatKicadResult(result);
      } else {
        return {
          content: [
            {
              type: "text",
              text: `Failed to connect to net: ${result.message || "Unknown error"}`,
            },
          ],
          isError: true,
        };
      }
    },
  );

  // Get net connections
  server.tool(
    "get_net_connections",
    "Get all connections for a named net",
    {
      schematicPath: z.string().describe("Path to the schematic file"),
      netName: z.string().describe("Name of the net to query"),
    },
    async (args: { schematicPath: string; netName: string }) => {
      const result = await callKicadScript("get_net_connections", args);
      if (result.success && result.connections) {
        const connectionList = result.connections
          .map((conn: any) => `  - ${conn.component}/${conn.pin}`)
          .join("\n");
        return {
          content: [
            {
              type: "text",
              text: `Net '${args.netName}' connections:\n${connectionList}`,
            },
          ],
        };
      } else {
        return {
          content: [
            {
              type: "text",
              text: `Failed to get net connections: ${result.message || "Unknown error"}`,
            },
          ],
          isError: true,
        };
      }
    },
  );

  // Get wire connections
  server.tool(
    "get_wire_connections",
    "Return the net name plus all wires and pins connected at a point, given reference + pin OR x/y in mm. " +
      "net=null means an unnamed net. The point must be a wire endpoint or junction (midpoints don't match) — " +
      "get exact coordinates from get_schematic_pin_locations or list_schematic_items (kind=wires).",
    {
      schematicPath: z.string().describe("Path to the schematic file"),
      reference: z
        .string()
        .optional()
        .describe("Component reference (e.g. U1, R1). Pair with pin."),
      pin: z
        .string()
        .optional()
        .describe("Pin number or name (e.g. '3', 'SDA'). Pair with reference."),
      x: z.number().optional().describe("X coordinate of a wire endpoint in mm. Pair with y."),
      y: z.number().optional().describe("Y coordinate of a wire endpoint in mm. Pair with x."),
    },
    async (args: {
      schematicPath: string;
      reference?: string;
      pin?: string;
      x?: number;
      y?: number;
    }) => {
      const result = await callKicadScript("get_wire_connections", args);
      if (result.success) {
        const netLabel = result.net ?? "(unnamed)";
        const pinList = (result.pins ?? [])
          .map((p: any) => `  - ${p.component}/${p.pin}`)
          .join("\n");
        const wireList = (result.wires ?? [])
          .map((w: any) => `  - (${w.start.x},${w.start.y}) → (${w.end.x},${w.end.y})`)
          .join("\n");
        const qp = result.query_point;
        return {
          content: [
            {
              type: "text",
              text:
                `Net: ${netLabel}\n` +
                `Query point: (${qp?.x ?? args.x}, ${qp?.y ?? args.y})\n` +
                `Connected pins:\n${pinList || "  (none found)"}\n\nWire segments:\n${wireList || "  (none)"}`,
            },
          ],
        };
      } else {
        return {
          content: [
            {
              type: "text",
              text: `Failed to get wire connections: ${result.message || "Unknown error"}`,
            },
          ],
          isError: true,
        };
      }
    },
  );

  // Get pin locations for a schematic component
  server.tool(
    "get_schematic_pin_locations",
    "Returns the exact x/y coordinates of every pin on a schematic component. Use this before add_schematic_net_label to place labels correctly on pin endpoints.",
    {
      schematicPath: z.string().describe("Path to the schematic file"),
      reference: z.string().describe("Component reference designator (e.g. U1, R1, J2)"),
    },
    async (args: { schematicPath: string; reference: string }) => {
      const result = await callKicadScript("get_schematic_pin_locations", args);
      if (result.success && result.pins) {
        const lines = Object.entries(result.pins as Record<string, any>).map(
          ([pinNum, data]: [string, any]) =>
            `  Pin ${pinNum} (${data.name || pinNum}): x=${data.x}, y=${data.y}, angle=${data.angle ?? 0}°` +
            // Multi-unit parts place each unit separately — surface the unit so
            // an agent labelling by pin number lands on the right channel.
            (data.unit != null ? ` [unit ${data.unit}]` : ""),
        );
        return {
          content: [
            {
              type: "text",
              text: `Pin locations for ${args.reference}:\n${lines.join("\n")}`,
            },
          ],
        };
      } else {
        return {
          content: [
            {
              type: "text",
              text: `Failed to get pin locations: ${result.message || "Unknown error"}`,
            },
          ],
          isError: true,
        };
      }
    },
  );

  // Connect all pins of source connector to matching pins of target connector (passthrough)
  server.tool(
    "connect_passthrough",
    "Connects all pins of a source connector (e.g. J1) to matching pins of a target connector (e.g. J2) via shared net labels — pin N gets net '{netPrefix}_{N}'. Use this for FFC/ribbon cable passthrough adapters instead of calling connect_to_net for every pin.",
    {
      schematicPath: z.string().describe("Path to the schematic file"),
      sourceRef: z.string().describe("Source connector reference (e.g. J1)"),
      targetRef: z.string().describe("Target connector reference (e.g. J2)"),
      netPrefix: z
        .string()
        .optional()
        .describe("Net name prefix, e.g. 'CSI' → CSI_1, CSI_2 (default: PIN)"),
      pinOffset: z
        .number()
        .optional()
        .describe("Add to pin number when building net name (default: 0)"),
    },
    async (args: {
      schematicPath: string;
      sourceRef: string;
      targetRef: string;
      netPrefix?: string;
      pinOffset?: number;
    }) => {
      const result = await callKicadScript("connect_passthrough", args);
      if (result.success !== false || (result.connected && result.connected.length > 0)) {
        const lines: string[] = [];
        if (result.connected?.length)
          lines.push(
            `Connected (${result.connected.length}): ${result.connected.slice(0, 5).join(", ")}${result.connected.length > 5 ? " ..." : ""}`,
          );
        if (result.failed?.length)
          lines.push(`Failed (${result.failed.length}): ${result.failed.join(", ")}`);
        return {
          content: [{ type: "text", text: result.message + "\n" + lines.join("\n") }],
        };
      } else {
        return {
          content: [
            {
              type: "text",
              text: `Passthrough failed: ${result.message || "Unknown error"}`,
            },
          ],
          isError: true,
        };
      }
    },
  );

  // Delete wire from schematic
  server.tool(
    "delete_schematic_wire",
    "Remove a wire from the schematic by start and end coordinates.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      start: z.object({ x: z.number(), y: z.number() }).describe("Wire start position"),
      end: z.object({ x: z.number(), y: z.number() }).describe("Wire end position"),
    },
    async (args: {
      schematicPath: string;
      start: { x: number; y: number };
      end: { x: number; y: number };
    }) => {
      const result = await callKicadScript("delete_schematic_wire", args);
      if (result.success) {
        return {
          content: [
            {
              type: "text",
              text: `Deleted wire from (${args.start.x}, ${args.start.y}) to (${args.end.x}, ${args.end.y})`,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text",
            text: `Failed to delete wire: ${result.message || "Unknown error"}`,
          },
        ],
        isError: true,
      };
    },
  );

  // Edit, move, or delete an existing net label — replaces the former
  // edit_schematic_net_label / move_schematic_net_label /
  // delete_schematic_net_label tools. Dispatches to the original python
  // commands based on `action`.
  const LABEL_COMMANDS = {
    edit: "edit_schematic_net_label",
    move: "move_schematic_net_label",
    delete: "delete_schematic_net_label",
  } as const;

  server.tool(
    "edit_schematic_net_label",
    "Edit, move, or delete an existing net label. action='edit' changes type (label <-> global_label <-> hierarchical_label) and/or text in place (pass newLabelType and/or newName); action='move' repositions it (pass newPosition); action='delete' removes it. Disambiguate duplicates with currentPosition (edit/move), position (delete), or labelType (edit/move).",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      action: z
        .enum(["edit", "move", "delete"])
        .describe("What to do with the label: edit its type/text, move it, or delete it"),
      netName: z.string().describe("Current text of the net label to target"),
      newLabelType: z
        .enum(["label", "global_label", "hierarchical_label"])
        .optional()
        .describe(
          "Only for action='edit': new label type. 'label' = page-local, 'global_label' = cross-page, 'hierarchical_label' = sheet boundary. Omit to keep the current type (rename only).",
        ),
      newName: z
        .string()
        .optional()
        .describe("Only for action='edit': new label text. Omit to keep the current text."),
      newPosition: z
        .object({ x: z.number(), y: z.number() })
        .optional()
        .describe("Required for action='move': target position in mm."),
      currentPosition: z
        .object({ x: z.number(), y: z.number() })
        .optional()
        .describe(
          "For action='edit'/'move': current position to disambiguate when multiple labels share the same name.",
        ),
      position: z
        .object({ x: z.number(), y: z.number() })
        .optional()
        .describe(
          "For action='delete': position to disambiguate when multiple labels share the same name.",
        ),
      labelType: z
        .enum(["label", "global_label", "hierarchical_label"])
        .optional()
        .describe("For action='edit'/'move': restrict the search to a specific label type."),
    },
    async (args: {
      schematicPath: string;
      action: "edit" | "move" | "delete";
      netName: string;
      newLabelType?: "label" | "global_label" | "hierarchical_label";
      newName?: string;
      newPosition?: { x: number; y: number };
      currentPosition?: { x: number; y: number };
      position?: { x: number; y: number };
      labelType?: "label" | "global_label" | "hierarchical_label";
    }) => {
      const { action, ...params } = args;
      return formatKicadResult(await callKicadScript(LABEL_COMMANDS[action], params));
    },
  );

  // Add hierarchical label to a sub-sheet
  server.tool(
    "add_schematic_hierarchical_label",
    "Add a hierarchical label (sheet interface port) to a sub-sheet schematic. " +
      "Hierarchical labels are the connection points that link a sub-sheet to its " +
      "parent via sheet pins. The label text must exactly match the corresponding " +
      "sheet pin name.",
    {
      schematicPath: z.string().describe("Path to the sub-sheet .kicad_sch file"),
      text: z.string().describe("Label text (e.g. 'SD_CLK') — must match the sheet pin name"),
      position: z.array(z.number()).length(2).describe("Position [x, y] in mm"),
      shape: z
        .enum(["input", "output", "bidirectional"])
        .describe("Signal direction from the sub-sheet's perspective"),
      orientation: z
        .number()
        .optional()
        .describe("Rotation in degrees: 0=label points right, 180=label points left (default: 0)"),
    },
    async (args: {
      schematicPath: string;
      text: string;
      position: number[];
      shape: "input" | "output" | "bidirectional";
      orientation?: number;
    }) => {
      const result = await callKicadScript("add_schematic_hierarchical_label", args);
      if (result.success) {
        return {
          content: [
            {
              type: "text" as const,
              text: result.message || `Added hierarchical label '${args.text}'`,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text" as const,
            text: `Failed to add hierarchical label: ${result.message || "Unknown error"}`,
          },
        ],
        isError: true,
      };
    },
  );
}
