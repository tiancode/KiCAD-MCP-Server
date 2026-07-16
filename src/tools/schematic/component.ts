/**
 * Schematic Component CRUD and properties tools for KiCAD MCP server.
 * Split out of the former monolithic schematic.ts.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import {
  CommandFunction,
  errorResult,
  failureResult,
  makePassthrough,
  textResult,
  toXyObject,
  XY_POINT_FORMS,
  xyPointSchema,
  XyPointInput,
} from "../tool-response.js";

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
      path: z
        .string()
        .optional()
        .describe(
          "Optional: containing directory OR full <name>.kicad_sch file path (basename must match name, else errorCode SCHEMATIC_NAME_CONFLICT).",
        ),
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
      position: xyPointSchema.optional().describe(`Position on schematic in mm. ${XY_POINT_FORMS}`),
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
      autoAssign: z
        .boolean()
        .optional()
        .describe(
          "Default false. When the reference is empty or already used, refuse " +
            "(INVALID_REFERENCE / REFERENCE_EXISTS). Set true to instead auto-number " +
            "the next free reference of the same prefix (e.g. R3 when R1/R2 exist).",
        ),
    },
    async (args: {
      schematicPath: string;
      symbol: string;
      reference: string;
      value?: string;
      footprint?: string;
      position?: XyPointInput;
      unit?: number;
      placeAllUnits?: boolean;
      snapToGrid?: boolean;
      snapGridMm?: number;
      autoAssign?: boolean;
    }) => {
      // Transform to what Python backend expects
      const [library, symbolName] = args.symbol.includes(":")
        ? args.symbol.split(":")
        : ["Device", args.symbol];

      // Accept both {x,y} and [x,y] for position (S12).
      const pos = args.position ? toXyObject(args.position) : undefined;
      const transformed = {
        schematicPath: args.schematicPath,
        snapToGrid: args.snapToGrid,
        snapGridMm: args.snapGridMm,
        placeAllUnits: args.placeAllUnits,
        autoAssign: args.autoAssign,
        component: {
          library,
          type: symbolName,
          reference: args.reference,
          value: args.value,
          footprint: args.footprint ?? "",
          // Python expects flat x, y not nested position
          x: pos?.x ?? 0,
          y: pos?.y ?? 0,
          unit: args.unit ?? 1,
          placeAllUnits: args.placeAllUnits,
        },
      };

      const result = await callKicadScript("add_schematic_component", transformed);
      if (result.success) {
        const pos = result.position;
        // The backend may auto-assign a different reference (A6/A11) when the
        // requested one was empty or already taken and autoAssign was set.
        const landedRef = result.component_reference ?? args.reference;
        let text = `Successfully added ${landedRef} (${args.symbol}) to schematic`;
        if (result.autoAssignedReference && result.requestedReference !== undefined)
          text += ` [auto-assigned; requested "${result.requestedReference}"]`;
        if (pos) text += ` at (${pos.x}, ${pos.y})`;
        // Contract: report the .snap delta whenever coordinates moved so an
        // agent aiming at an exact point isn't silently relocated.
        if (result.snap?.applied) {
          const req = result.snap.requested;
          text += ` [snapped to ${result.snap.gridMm} mm grid from (${req?.x}, ${req?.y})]`;
        }
        // Footprint (S14): report the footprint the symbol landed with — an
        // inherited library default is easy to miss, and "no footprint set"
        // matters because sync_schematic_to_board skips footprint-less symbols.
        if (result.footprint) {
          text += `\nFootprint: ${result.footprint}${
            result.footprintSource === "library" ? " (inherited from library symbol)" : ""
          }`;
        } else if (result.footprintNote) {
          text += `\n${result.footprintNote}`;
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
        // Page-awareness (S2/S9): surface an off-page landing so an agent
        // aiming inside the sheet knows the symbol (or a unit) hung over the
        // border. offPageUnits lists the multi-unit offenders when present.
        if (result.offPageWarning) {
          text += `\nOFF-PAGE: ${result.offPageWarning}`;
          if (result.offPageUnits) text += ` (units ${JSON.stringify(result.offPageUnits)})`;
        }
        // Append the raw position/snap blocks so structured consumers get them.
        text += `\n${JSON.stringify({ position: pos ?? null, snap: result.snap ?? null })}`;
        return textResult(text, result);
      } else {
        return errorResult(`Failed to add component: ${result.message || JSON.stringify(result)}`);
      }
    },
  );

  // Delete component from schematic
  server.tool(
    "delete_schematic_component",
    "Remove a placed symbol from a .kicad_sch schematic (keeps its lib_symbols definition). " +
      "Reports any wire stubs and net labels that were attached to the symbol's pins; by default " +
      "these are LEFT behind as orphans (matching a KiCad-GUI delete). Pass removeDanglingWires=true " +
      "to also clean them up. To remove a PCB footprint use delete_component instead.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      reference: z
        .string()
        .describe("Reference designator of the component to remove (e.g. R1, U3)"),
      removeDanglingWires: z
        .boolean()
        .optional()
        .describe(
          "Default false. true also removes the wire stubs and net labels attached to the " +
            "deleted symbol's pins, so no orphans are left behind.",
        ),
      removeStubs: z
        .boolean()
        .optional()
        .describe(
          "Deprecated alias for removeDanglingWires — kept so an older name still works. " +
            "If either flag is true the stubs and labels are removed.",
        ),
    },
    async (args: {
      schematicPath: string;
      reference: string;
      removeDanglingWires?: boolean;
      removeStubs?: boolean;
    }) => {
      // Honor removeStubs as a deprecated alias (A5): OR it into the real flag
      // before the passthrough, since the Python handler only reads
      // removeDanglingWires and Zod would otherwise strip the alias.
      const result = await callKicadScript("delete_schematic_component", {
        schematicPath: args.schematicPath,
        reference: args.reference,
        removeDanglingWires: Boolean(args.removeDanglingWires || args.removeStubs),
      });
      if (result.success) {
        const d = result.dangling ?? {};
        const wireCount = d.wireCount ?? 0;
        const labelCount = d.labelCount ?? 0;
        let text = `Successfully removed ${args.reference} from schematic`;
        if (wireCount || labelCount) {
          const verb = d.removed ? "Removed" : "Left behind";
          text += `\n${verb} ${d.removed ? (d.wiresRemoved ?? wireCount) : wireCount} attached wire stub(s) and ${
            d.removed ? (d.labelsRemoved ?? labelCount) : labelCount
          } net label(s).`;
          const wireCoords = (d.wires ?? [])
            .map((w: any) => `(${w.start.x},${w.start.y})->(${w.end.x},${w.end.y})`)
            .join(", ");
          if (wireCoords) text += `\n  wires: ${wireCoords}`;
          const labelCoords = (d.labels ?? [])
            .map((l: any) => `"${l.name}"@(${l.position.x},${l.position.y})`)
            .join(", ");
          if (labelCoords) text += `\n  labels: ${labelCoords}`;
          if (!d.removed) text += `\n  (pass removeDanglingWires=true to clean these up)`;
        } else {
          text += ` (no attached wire stubs or labels found)`;
        }
        return textResult(text, result);
      }
      return failureResult("Failed to remove component", result);
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
      allowUnresolvedFootprint: z
        .boolean()
        .optional()
        .describe(
          "Assign the footprint even if it does not resolve to a real library footprint (sync will then skip that component).",
        ),
    },
    async (args: {
      schematicPath: string;
      reference: string;
      footprint?: string;
      value?: string;
      newReference?: string;
      allowUnresolvedFootprint?: boolean;
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
        return textResult(
          `Successfully updated ${args.reference}: ${summaryParts.join("; ") || "(no-op)"}`,
        );
      }
      return failureResult("Failed to edit component", result);
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
        return textResult(
          `Component ${result.reference} at ${pos}\nFields:\n${fieldLines.join("\n")}`,
        );
      }
      return failureResult("Failed to get component", result);
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
      position: xyPointSchema.describe(
        `New position in schematic mm coordinates. ${XY_POINT_FORMS}`,
      ),
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
      position: XyPointInput;
      preserveWires?: boolean;
      snapToGrid?: boolean;
      snapGridMm?: number;
    }) => {
      // Accept both {x,y} and [x,y] for position (S12).
      const result = await callKicadScript("move_schematic_component", {
        ...args,
        position: toXyObject(args.position),
      });
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
        // Page-awareness (S9): flag a move that landed off the sheet.
        if (result.offPageWarning) text += `\nOFF-PAGE: ${result.offPageWarning}`;
        // A4: a move that detached a coincident foreign pin from a shared net.
        if (result.detachWarning) text += `\nDETACHED: ${result.detachWarning}`;
        return textResult(text, result);
      }
      return failureResult("Failed to move component", result);
    },
  );

  // Rotate schematic component
  server.tool(
    "rotate_schematic_component",
    "Rotate a placed symbol in the schematic. Only orthogonal angles are valid (0, 90, 180, 270); " +
      "any other multiple-of-90 (e.g. -90, 450) is normalized, and a non-multiple (e.g. 45) is rejected.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      reference: z.string().describe("Reference designator (e.g., R1, U1)"),
      angle: z
        .number()
        .describe(
          "Rotation angle in degrees. Must be a multiple of 90 (0, 90, 180, 270); negatives and " +
            "values ≥360 are normalized (e.g. -90 → 270). 45° and other non-orthogonal angles are rejected.",
        ),
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
        // result.angle is the normalized value (e.g. -90 → 270).
        const shown = result.angle ?? args.angle;
        const norm =
          result.requestedAngle !== undefined ? ` (normalized from ${result.requestedAngle}°)` : "";
        return textResult(
          `Rotated ${args.reference} to ${shown}°${norm}${args.mirror ? ` (mirrored ${args.mirror})` : ""}`,
        );
      }
      return failureResult("Failed to rotate component", result);
    },
  );

  // Duplicate a placed schematic symbol (S13)
  server.tool(
    "duplicate_schematic_component",
    "Clone a placed schematic symbol — same library symbol, value, footprint, custom properties " +
      "(MPN/LCSC/etc.), and unit structure — at an offset from the source (default {x:10,y:0} mm) or " +
      "an explicit position. Auto-assigns the next free reference of the same prefix (R3 when R1/R2 " +
      "exist) unless newReference is given. Returns the new reference and position.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      reference: z.string().describe("Reference of the existing symbol to clone (e.g. R1, U3)"),
      newReference: z
        .string()
        .optional()
        .describe(
          "Reference for the clone (e.g. R7). Omit to auto-assign the next free ref of the same prefix.",
        ),
      offset: xyPointSchema
        .optional()
        .describe(
          `Offset from the source position in mm (default {x:10, y:0}). Ignored if position is given. ${XY_POINT_FORMS}`,
        ),
      position: xyPointSchema
        .optional()
        .describe(`Explicit position in mm for the clone (overrides offset). ${XY_POINT_FORMS}`),
      snapToGrid: z
        .boolean()
        .optional()
        .describe(
          "Default true — the clone snaps to the 1.27 mm grid like add_schematic_component " +
            "so its pins stay on the connection grid. Pass false only for intentional sub-grid placement.",
        ),
      snapGridMm: z.number().positive().optional().describe("Snap grid in mm (default 1.27)."),
    },
    async (args: {
      schematicPath: string;
      reference: string;
      newReference?: string;
      offset?: XyPointInput;
      position?: XyPointInput;
      snapToGrid?: boolean;
      snapGridMm?: number;
    }) => {
      // Accept both {x,y} and [x,y] for offset/position (S12).
      const params: Record<string, unknown> = {
        schematicPath: args.schematicPath,
        reference: args.reference,
      };
      if (args.newReference !== undefined) params.newReference = args.newReference;
      if (args.offset !== undefined) params.offset = toXyObject(args.offset);
      if (args.position !== undefined) params.position = toXyObject(args.position);
      if (args.snapToGrid !== undefined) params.snapToGrid = args.snapToGrid;
      if (args.snapGridMm !== undefined) params.snapGridMm = args.snapGridMm;

      const result = await callKicadScript("duplicate_schematic_component", params);
      if (result.success) {
        const p = result.position ?? {};
        let text = `Duplicated ${args.reference} → ${result.reference} at (${p.x}, ${p.y})`;
        // A2: report the grid-snap delta so the caller sees the clone was
        // nudged onto the connection grid (matches add_schematic_component).
        if (result.snap?.applied) {
          const req = result.snap.requested;
          text += ` [snapped to ${result.snap.gridMm} mm grid from (${req?.x}, ${req?.y})]`;
        }
        if (result.copiedProperties?.length)
          text += `\nCopied properties: ${result.copiedProperties.join(", ")}`;
        if (result.footprint) text += `\nFootprint: ${result.footprint}`;
        if (result.units)
          text += `\nUnits: ${result.units.total} total, placed ${JSON.stringify(result.units.placed)}`;
        if (result.offPageWarning) text += `\nOFF-PAGE: ${result.offPageWarning}`;
        return textResult(text, result);
      }
      return failureResult("Failed to duplicate component", result);
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
          return textResult(
            "No components needed annotation — every symbol already has a " +
              "concrete reference (no '?' placeholders found). This is the " +
              "expected state when add_schematic_component was called with " +
              "explicit references; you can drop annotate_schematic from this " +
              "flow.",
          );
        }
        const lines = annotated.map((a: any) => `  ${a.oldReference} → ${a.newReference}`);
        return textResult(`Annotated ${annotated.length} component(s):\n${lines.join("\n")}`);
      }
      return failureResult("Failed to annotate", result);
    },
  );
}
