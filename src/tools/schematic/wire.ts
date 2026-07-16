/**
 * Schematic Wires, net labels, and connections tools for KiCAD MCP server.
 * Split out of the former monolithic schematic.ts.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import {
  CommandFunction,
  failureResult,
  formatKicadResult,
  textResult,
  toXyObject,
  toXyTuple,
  XY_POINT_FORMS,
  xyPointSchema,
  XyPointInput,
} from "../tool-response.js";

export function registerSchematicWireTools(server: McpServer, callKicadScript: CommandFunction) {
  // Draw wire between coordinate waypoints with optional pin snapping
  server.tool(
    "add_schematic_wire",
    "Draw a wire through 2+ waypoints. Get pin coordinates from get_schematic_pin_locations and use them as first/last waypoints. snapToPins (default on) snaps only endpoints to the nearest exact pin; intermediate waypoints route around parts (e.g. [[x1,y1],[xMid,y1],[xMid,y2],[x2,y2]]) and are never snapped.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      waypoints: z
        .array(xyPointSchema)
        .min(2)
        .describe(`Ordered list of points. Minimum 2. Each point ${XY_POINT_FORMS}`),
      snapToPins: z
        .boolean()
        .optional()
        .describe("Snap the first and last waypoints to the nearest pin (default: true)"),
      snapTolerance: z.number().optional().describe("Maximum snap distance in mm (default: 1.0)"),
    },
    async (args: {
      schematicPath: string;
      waypoints: XyPointInput[];
      snapToPins?: boolean;
      snapTolerance?: number;
    }) => {
      // Accept both {x,y} and [x,y] per waypoint (S12); Python expects [x,y].
      const result = await callKicadScript("add_schematic_wire", {
        ...args,
        waypoints: args.waypoints.map(toXyTuple),
      });
      if (result.success) {
        return textResult(result.message || "Wire added successfully");
      } else {
        return failureResult("Failed to add wire", result);
      }
    },
  );

  // Add net label
  server.tool(
    "add_schematic_net_label",
    "Add a net label. KiCad connects a label to a pin ONLY at the exact pin endpoint (0.01 mm off breaks it). " +
      "Prefer componentRef + pinNumber (snaps to the pin); or position [x, y], auto-snapped to a pin " +
      "within snapTolerance. Response reports connected_to_pin = {ref, pin} | null. " +
      "For dense 2-pin passives, connect_to_net (label on a short stub) reads cleaner.",
    {
      schematicPath: z.string().describe("Path to the schematic file"),
      netName: z.string().describe("Name of the net (e.g., VCC, GND, SIGNAL_1)"),
      position: xyPointSchema
        .optional()
        .describe(
          `Position for the label. Required when componentRef/pinNumber are not given. ${XY_POINT_FORMS}`,
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
      orientation: z
        .number()
        .optional()
        .describe(
          "Rotation angle 0/90/180/270. Default: derived from the snapped pin's outward direction so text extends away from the symbol, else 0.",
        ),
      snapTolerance: z
        .number()
        .optional()
        .describe("Auto-snap radius in mm for raw positions (default 0.05). 0 disables snapping."),
    },
    async (args: {
      schematicPath: string;
      netName: string;
      position?: XyPointInput;
      componentRef?: string;
      pinNumber?: string | number;
      labelType?: string;
      orientation?: number;
      snapTolerance?: number;
    }) => {
      // Accept both {x,y} and [x,y] for position (S12); Python expects [x,y].
      const result = await callKicadScript("add_schematic_net_label", {
        ...args,
        ...(args.position !== undefined ? { position: toXyTuple(args.position) } : {}),
      });
      if (result.success) {
        return formatKicadResult(result);
      } else {
        return failureResult("Failed to add net label", result);
      }
    },
  );

  // Add or remove a no-connect flag — replaces add_no_connect and
  // delete_no_connect. Dispatches to the original python commands.
  server.tool(
    "set_no_connect",
    "Add a no-connect flag (X) to an intentionally unconnected pin, suppressing ERC 'Pin not connected' errors; remove=true deletes an existing flag. " +
      "Prefer componentRef + pinNumber; a raw position [x, y] (mm) must match the pin endpoint exactly.",
    {
      schematicPath: z.string().describe("Path to the schematic file"),
      position: xyPointSchema
        .optional()
        .describe(
          `Position in mm. Required when componentRef/pinNumber are not given. ${XY_POINT_FORMS}`,
        ),
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
      position?: XyPointInput;
      componentRef?: string;
      pinNumber?: string | number;
      remove?: boolean;
      tolerance?: number;
    }) => {
      const { remove, position, ...rest } = args;
      const command = remove === true ? "delete_no_connect" : "add_no_connect";
      // Accept both {x,y} and [x,y] for position (S12); Python expects [x,y].
      const params = position !== undefined ? { ...rest, position: toXyTuple(position) } : rest;
      return formatKicadResult(await callKicadScript(command, params));
    },
  );

  // Connect pin to net
  server.tool(
    "connect_to_net",
    "Connect a component pin to a named net via a wire stub + net label at the pin endpoint. " +
      "Auto-relocates the stub if the chosen point would merge into a DIFFERENT net; if no free " +
      "direction exists it refuses with label_collision:{point,existing_net}. A floating power-symbol " +
      "pin gets a stub wire (no label). Response: pin_location, label_location, wire_stub.",
    {
      schematicPath: z.string().describe("Path to the schematic file"),
      componentRef: z.string().describe("Component reference (e.g., U1, R1)"),
      pinName: z.string().describe("Pin name/number to connect"),
      netName: z.string().describe("Name of the net to connect to"),
      allowCoincidentPin: z
        .boolean()
        .optional()
        .describe(
          "connect even if a DIFFERENT component's pin sits at exactly the same coordinate — default refuses with kind:'coincident_pin'",
        ),
    },
    async (args: {
      schematicPath: string;
      componentRef: string;
      pinName: string;
      netName: string;
      allowCoincidentPin?: boolean;
    }) => {
      const result = await callKicadScript("connect_to_net", args);
      if (result.success) {
        return formatKicadResult(result);
      } else {
        return failureResult("Failed to connect to net", result);
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
      if (result.success) {
        const connectionList = (result.connections ?? [])
          .map((conn: any) => `  - ${conn.component}/${conn.pin}`)
          .join("\n");
        const lines = [`Net '${args.netName}' connections:`, connectionList || "  (none)"];
        // Power-symbol / PWR_FLAG attachment side-channels (Python side-channel
        // added for F3). Surface them so callers can verify power connectivity
        // without re-reading the raw .kicad_sch.
        const powerSymbols = result.power_symbols ?? [];
        if (powerSymbols.length) {
          lines.push("Power symbols:");
          for (const p of powerSymbols) lines.push(`  - ${p.ref}/${p.pin} (${p.value})`);
        }
        const powerFlags = result.power_flags ?? [];
        if (powerFlags.length) {
          lines.push("Power flags (PWR_FLAG):");
          for (const p of powerFlags) lines.push(`  - ${p.ref}/${p.pin} [${p.attachment}]`);
        }
        return textResult(lines.join("\n"));
      } else {
        return failureResult("Failed to get net connections", result);
      }
    },
  );

  // Get wire connections
  server.tool(
    "get_wire_connections",
    "Return the net plus all wires and pins connected at a point (reference + pin, OR x/y in mm). " +
      "net=null means unnamed; 'via' tells how it attaches ('wire' | 'label'). The point must be an " +
      "exact wire endpoint/junction OR a net label placed on the pin (midpoints don't match) — " +
      "get coordinates from get_schematic_pin_locations.",
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
        return textResult(
          `Net: ${netLabel}\n` +
            `Via: ${result.via ?? "—"}\n` +
            `Query point: (${qp?.x ?? args.x}, ${qp?.y ?? args.y})\n` +
            `Connected pins:\n${pinList || "  (none found)"}\n\nWire segments:\n${wireList || "  (none)"}`,
        );
      } else {
        return failureResult("Failed to get wire connections", result);
      }
    },
  );

  // Get pin locations for a schematic component
  server.tool(
    "get_schematic_pin_locations",
    "Return the exact x/y coordinates of every pin on a schematic component. Use before add_schematic_net_label or add_schematic_wire.",
    {
      schematicPath: z.string().describe("Path to the schematic file"),
      reference: z.string().describe("Component reference designator (e.g. U1, R1, J2)"),
    },
    async (args: { schematicPath: string; reference: string }) => {
      const result = await callKicadScript("get_schematic_pin_locations", args);
      if (result.success && result.pins) {
        const lines = Object.entries(result.pins as Record<string, any>).map(
          ([pinNum, data]: [string, any]) => {
            const unitTag = data.unit != null ? ` [unit ${data.unit}]` : "";
            // A pin whose multi-unit unit isn't placed has no real location —
            // show it as NOT PLACED (never a fabricated coordinate) so an agent
            // doesn't try to label/connect a phantom pin (F1).
            if (data.placed === false) {
              return `  Pin ${pinNum} (${data.name || pinNum}): NOT PLACED${unitTag}`;
            }
            return (
              `  Pin ${pinNum} (${data.name || pinNum}): x=${data.x}, y=${data.y}, angle=${data.angle ?? 0}°` +
              unitTag
            );
          },
        );
        let text = `Pin locations for ${args.reference}:\n${lines.join("\n")}`;
        if (result.warning) text += `\nWARNING: ${result.warning}`;
        return textResult(text);
      } else {
        return failureResult("Failed to get pin locations", result);
      }
    },
  );

  // Connect all pins of source connector to matching pins of target connector (passthrough)
  server.tool(
    "connect_passthrough",
    "Connect all pins of a source connector to matching pins of a target connector via shared net labels — pin N gets net '{netPrefix}_{N}'. For FFC/ribbon passthrough adapters, instead of per-pin connect_to_net.",
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
        return textResult(result.message + "\n" + lines.join("\n"));
      } else {
        return failureResult("Passthrough failed", result);
      }
    },
  );

  // Delete wire from schematic
  server.tool(
    "delete_schematic_wire",
    "Remove a wire from the schematic by start and end coordinates.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      start: xyPointSchema.describe(`Wire start position. ${XY_POINT_FORMS}`),
      end: xyPointSchema.describe(`Wire end position. ${XY_POINT_FORMS}`),
    },
    async (args: { schematicPath: string; start: XyPointInput; end: XyPointInput }) => {
      // Accept both {x,y} and [x,y] for start/end (S12); Python expects {x,y}.
      const start = toXyObject(args.start);
      const end = toXyObject(args.end);
      const result = await callKicadScript("delete_schematic_wire", {
        schematicPath: args.schematicPath,
        start,
        end,
      });
      if (result.success) {
        return textResult(`Deleted wire from (${start.x}, ${start.y}) to (${end.x}, ${end.y})`);
      }
      return failureResult("Failed to delete wire", result);
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
    "Edit, move, or delete an existing net label. edit: change type and/or text (newLabelType/newName); move: reposition (newPosition); delete: remove. Disambiguate duplicate names with currentPosition (edit/move), position (delete), or labelType.",
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
          "edit only: new type — label=page-local, global_label=cross-page, hierarchical_label=sheet boundary. Omit to keep.",
        ),
      newName: z.string().optional().describe("edit only: new label text. Omit to keep."),
      newPosition: xyPointSchema
        .optional()
        .describe(`Required for action='move': target position in mm. ${XY_POINT_FORMS}`),
      currentPosition: xyPointSchema
        .optional()
        .describe(
          `edit/move: current position, to disambiguate same-named labels. ${XY_POINT_FORMS}`,
        ),
      position: xyPointSchema
        .optional()
        .describe(`delete: position, to disambiguate same-named labels. ${XY_POINT_FORMS}`),
      labelType: z
        .enum(["label", "global_label", "hierarchical_label"])
        .optional()
        .describe("edit/move: restrict the search to a specific label type."),
    },
    async (args: {
      schematicPath: string;
      action: "edit" | "move" | "delete";
      netName: string;
      newLabelType?: "label" | "global_label" | "hierarchical_label";
      newName?: string;
      newPosition?: XyPointInput;
      currentPosition?: XyPointInput;
      position?: XyPointInput;
      labelType?: "label" | "global_label" | "hierarchical_label";
    }) => {
      const { action, newPosition, currentPosition, position, ...rest } = args;
      // Accept both {x,y} and [x,y] for each point (S12); Python expects {x,y}.
      const params: Record<string, unknown> = { ...rest };
      if (newPosition !== undefined) params.newPosition = toXyObject(newPosition);
      if (currentPosition !== undefined) params.currentPosition = toXyObject(currentPosition);
      if (position !== undefined) params.position = toXyObject(position);
      return formatKicadResult(await callKicadScript(LABEL_COMMANDS[action], params));
    },
  );

  // Add hierarchical label to a sub-sheet
  server.tool(
    "add_schematic_hierarchical_label",
    "Add a hierarchical label (sheet interface port) to a sub-sheet schematic; it links " +
      "the sub-sheet to its parent via a sheet pin whose name must exactly match the label text.",
    {
      schematicPath: z.string().describe("Path to the sub-sheet .kicad_sch file"),
      text: z.string().describe("Label text (e.g. 'SD_CLK')"),
      position: xyPointSchema.describe(`Position in mm. ${XY_POINT_FORMS}`),
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
      position: XyPointInput;
      shape: "input" | "output" | "bidirectional";
      orientation?: number;
    }) => {
      // Accept both {x,y} and [x,y] for position (S12); Python expects [x,y].
      const result = await callKicadScript("add_schematic_hierarchical_label", {
        ...args,
        position: toXyTuple(args.position),
      });
      if (result.success) {
        return textResult(result.message || `Added hierarchical label '${args.text}'`);
      }
      return failureResult("Failed to add hierarchical label", result);
    },
  );
}
