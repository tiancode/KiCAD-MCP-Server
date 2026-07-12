/**
 * Component management tools for KiCAD MCP server
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { logger } from "../logger.js";
import { paginationParams } from "./pagination-params.js";
import { CommandFunction, formatKicadResult, makePassthrough } from "./tool-response.js";

/**
 * Register component management tools with the MCP server
 *
 * @param server MCP server instance
 * @param callKicadScript Function to call KiCAD script commands
 */
export function registerComponentTools(server: McpServer, callKicadScript: CommandFunction): void {
  const passthrough = makePassthrough(callKicadScript);
  logger.info("Registering component management tools");

  // ------------------------------------------------------
  // Place Component Tool
  // ------------------------------------------------------
  const placeComponentSchema = {
    componentId: z
      .string()
      .describe("Footprint library ID, e.g. 'Package_DIP:DIP-8_W7.62mm' or 'R_0603_10k'."),
    position: z
      .object({
        x: z.number(),
        y: z.number(),
        unit: z.enum(["mm", "inch", "mil"]),
      })
      .describe("Position coordinates and unit"),
    reference: z.string().optional().describe("Optional desired reference (e.g., 'R5')"),
    value: z.string().optional().describe("Optional component value (e.g., '10k')"),
    footprint: z.string().optional().describe("Optional specific footprint name"),
    rotation: z.number().optional().describe("Optional rotation in degrees"),
    layer: z.string().optional().describe("Optional layer (e.g., 'F.Cu', 'B.SilkS')"),
    boardPath: z
      .string()
      .optional()
      .describe(
        "Path to the .kicad_pcb file – required when using project-local footprint libraries",
      ),
  } as const;

  type PlaceComponentArgs = {
    componentId: string;
    position: { x: number; y: number; unit: "mm" | "inch" | "mil" };
    reference?: string;
    value?: string;
    footprint?: string;
    rotation?: number;
    layer?: string;
    boardPath?: string;
  };

  const placeComponentHandler = async (args: PlaceComponentArgs) => {
    logger.debug(
      `Placing component: ${args.componentId} at ${args.position.x},${args.position.y} ${args.position.unit}`,
    );
    const result = await callKicadScript("place_component", args);
    return formatKicadResult(result);
  };

  server.tool(
    "place_component",
    "Add a NEW footprint instance to the PCB at the given position. Errors if the reference already exists — use move_component to relocate an existing part. Optionally set reference, value, footprint, rotation and layer.",
    placeComponentSchema,
    placeComponentHandler,
  );

  // ------------------------------------------------------
  // Move Component Tool
  // ------------------------------------------------------
  server.tool(
    "move_component",
    "Move a PCB component to a new position. Optionally update rotation or flip to a different copper layer.",
    {
      reference: z.string().describe("Reference designator of the component (e.g., 'R5')"),
      position: z
        .object({
          x: z.number(),
          y: z.number(),
          unit: z.enum(["mm", "inch", "mil"]),
        })
        .describe("New position coordinates and unit"),
      rotation: z.number().optional().describe("Optional new rotation in degrees"),
      layer: z
        .string()
        .optional()
        .describe("Optional target layer (e.g., 'F.Cu', 'B.Cu') - flips component if needed"),
    },
    async ({ reference, position, rotation, layer }) => {
      logger.debug(
        `Moving component: ${reference} to ${position.x},${position.y} ${position.unit}`,
      );
      const result = await callKicadScript("move_component", {
        reference,
        position,
        rotation,
        layer,
      });

      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Rotate Component Tool
  // ------------------------------------------------------
  server.tool(
    "rotate_component",
    "Rotate a PCB component to an absolute angle in degrees.",
    {
      reference: z.string().describe("Reference designator of the component (e.g., 'R5')"),
      angle: z.number().describe("Rotation angle in degrees (absolute, not relative)"),
    },
    async ({ reference, angle }) => {
      logger.debug(`Rotating component: ${reference} to ${angle} degrees`);
      const result = await callKicadScript("rotate_component", {
        reference,
        angle,
      });

      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Delete Component Tool
  // ------------------------------------------------------
  server.tool(
    "delete_component",
    "Remove a component from the PCB by its reference designator.",
    {
      reference: z
        .string()
        .describe("Reference designator of the component to delete (e.g., 'R5')"),
    },
    async ({ reference }) => {
      logger.debug(`Deleting component: ${reference}`);
      const result = await callKicadScript("delete_component", { reference });

      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Edit Component Properties Tool
  // ------------------------------------------------------
  server.tool(
    "edit_component",
    "Edit properties of an existing PCB component (reference, value, footprint).",
    {
      reference: z.string().describe("Reference designator of the component (e.g., 'R5')"),
      newReference: z.string().optional().describe("Optional new reference designator"),
      value: z.string().optional().describe("Optional new component value"),
      footprint: z.string().optional().describe("Optional new footprint"),
    },
    async ({ reference, newReference, value, footprint }) => {
      logger.debug(`Editing component: ${reference}`);
      const result = await callKicadScript("edit_component", {
        reference,
        newReference,
        value,
        footprint,
      });

      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Find Component Tool
  // ------------------------------------------------------
  server.tool(
    "find_component",
    "Search for a PCB component by reference designator or value and return its position and properties.",
    {
      reference: z.string().optional().describe("Reference designator to search for"),
      value: z.string().optional().describe("Component value to search for"),
    },
    async ({ reference, value }) => {
      logger.debug(
        `Finding component with ${reference ? `reference: ${reference}` : `value: ${value}`}`,
      );
      const result = await callKicadScript("find_component", {
        reference,
        value,
      });

      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Get Component Properties Tool
  // ------------------------------------------------------
  server.tool(
    "get_component_properties",
    "Return all properties of a PCB component (position, rotation, layer, value, footprint).",
    {
      reference: z.string().describe("Reference designator of the component (e.g., 'R5')"),
    },
    async ({ reference }) => {
      logger.debug(`Getting properties for component: ${reference}`);
      const result = await callKicadScript("get_component_properties", {
        reference,
      });

      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Get Component Pads Tool
  // ------------------------------------------------------
  server.tool(
    "get_component_pads",
    "Return all pads of a PCB component with their exact positions, net assignments and sizes. Use this before routing to get accurate pad coordinates; pass pad to return just that one pad.",
    {
      reference: z.string().describe("Reference designator of the component (e.g., 'U1')"),
      pad: z
        .string()
        .optional()
        .describe("Return only this pad number/name (e.g. '1', 'A1') instead of all pads"),
      unit: z.enum(["mm", "mil", "inch"]).optional().describe("Unit for coordinates (default: mm)"),
    },
    async ({ reference, pad, unit }) => {
      logger.debug(`Getting pads for component: ${reference}`);
      if (pad !== undefined) {
        return formatKicadResult(
          await callKicadScript("get_pad_position", { reference, pad, unit: unit || "mm" }),
        );
      }
      const result = await callKicadScript("get_component_pads", {
        reference,
        unit: unit || "mm",
      });

      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Get Component List Tool
  // ------------------------------------------------------
  server.tool(
    "get_component_list",
    "Return a list of all components on the PCB, optionally filtered by layer or bounding box region.",
    {
      layer: z.string().optional().describe("Filter by layer (e.g., 'F.Cu', 'B.Cu')"),
      boundingBox: z
        .object({
          x1: z.number(),
          y1: z.number(),
          x2: z.number(),
          y2: z.number(),
          unit: z.enum(["mm", "inch", "mil"]).optional(),
        })
        .optional()
        .describe("Filter by bounding box region"),
      unit: z.enum(["mm", "mil", "inch"]).optional().describe("Unit for coordinates (default: mm)"),
      ...paginationParams,
    },
    async ({ layer, boundingBox, unit, limit, offset }) => {
      logger.debug("Getting component list");
      const result = await callKicadScript("get_component_list", {
        layer,
        boundingBox,
        unit: unit || "mm",
        limit,
        offset,
      });

      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Place Component Array Tool
  // ------------------------------------------------------
  server.tool(
    "place_component_array",
    "Place a rectangular grid array of identical components on the PCB with configurable row/column spacing.",
    {
      componentId: z.string().describe("Component identifier"),
      startPosition: z
        .object({
          x: z.number(),
          y: z.number(),
          unit: z.enum(["mm", "inch", "mil"]),
        })
        .describe("Starting position"),
      rows: z.number().describe("Number of rows"),
      columns: z.number().describe("Number of columns"),
      rowSpacing: z.number().describe("Spacing between rows"),
      columnSpacing: z.number().describe("Spacing between columns"),
      startReference: z.string().optional().describe("Starting reference (e.g., 'R1')"),
      footprint: z.string().optional().describe("Footprint name"),
      value: z.string().optional().describe("Component value"),
      rotation: z.number().optional().describe("Rotation in degrees"),
    },
    async ({
      componentId,
      startPosition,
      rows,
      columns,
      rowSpacing,
      columnSpacing,
      startReference,
      footprint,
      value,
      rotation,
    }) => {
      logger.debug(`Placing component array: ${rows}x${columns} of ${componentId}`);
      const result = await callKicadScript("place_component_array", {
        componentId,
        startPosition,
        rows,
        columns,
        rowSpacing,
        columnSpacing,
        startReference,
        footprint,
        value,
        rotation,
      });

      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Align Components Tool
  // ------------------------------------------------------
  server.tool(
    "align_components",
    "Align multiple PCB components horizontally, vertically or on a grid with optional spacing.",
    {
      references: z.array(z.string()).describe("Array of component references to align"),
      alignmentType: z.enum(["horizontal", "vertical", "grid"]).describe("Type of alignment"),
      spacing: z.number().optional().describe("Spacing between components in mm"),
      referenceComponent: z.string().optional().describe("Reference component for alignment"),
    },
    async ({ references, alignmentType, spacing, referenceComponent }) => {
      logger.debug(`Aligning components: ${references.join(", ")}`);
      const result = await callKicadScript("align_components", {
        references,
        alignmentType,
        spacing,
        referenceComponent,
      });

      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Check Courtyard Overlaps Tool
  //
  // Lets the caller validate a placement plan before committing it. The
  // `positions` parameter accepts hypothetical {ref: [x, y]} or
  // [x, y, rotation_degrees] entries; the board file is not modified.
  //
  // Approach ported from morningfire-pcb-automation
  //   https://github.com/NiNjA-CodE/morningfire-pcb-automation
  //   (scripts/placement/check_overlaps.py)
  // ------------------------------------------------------
  server.tool(
    "check_courtyard_overlaps",
    "Detect courtyard overlaps between footprints, and optionally flag courtyards past the board outline. Accepts a `positions` dict of hypothetical placements to validate a proposed move/place before committing. Returns overlap pairs with intersection extents (mm) and per-component boundary violations.",
    {
      positions: z
        .record(z.string(), z.array(z.number()).min(2).max(3))
        .optional()
        .describe(
          "Virtual placements: map of reference designator to [x, y] or [x, y, rotation_degrees] in mm. Each listed ref is checked AS IF it were at the given coordinates. Unspecified refs use their current board position.",
        ),
      refs: z
        .array(z.string())
        .optional()
        .describe("Limit the check to these refs (default: every footprint on the board)."),
      margin: z
        .number()
        .optional()
        .describe(
          "Extra clearance in mm added around every courtyard (default 0). Useful to enforce a manufacturing keepout wider than the symbol's declared courtyard.",
        ),
      include_boundary: z
        .boolean()
        .optional()
        .describe("Also flag courtyards that extend past the board outline (default true)."),
      board_outline: z
        .object({
          x1: z.number(),
          y1: z.number(),
          x2: z.number(),
          y2: z.number(),
          unit: z.enum(["mm", "inch"]).optional(),
        })
        .optional()
        .describe("Optional board outline bbox override. Default: derived from Edge.Cuts."),
    },
    async (args) => {
      logger.debug(
        `Checking courtyard overlaps (virtual=${
          args.positions ? Object.keys(args.positions).length : 0
        })`,
      );
      const result = await callKicadScript("check_courtyard_overlaps", args);
      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Duplicate Component Tool
  // ------------------------------------------------------
  server.tool(
    "duplicate_component",
    "Duplicate an existing PCB component at an offset position, optionally with a new reference designator.",
    {
      reference: z.string().describe("Reference of component to duplicate"),
      offset: z
        .object({
          x: z.number(),
          y: z.number(),
          unit: z.enum(["mm", "inch", "mil"]).optional(),
        })
        .describe("Offset from original position"),
      newReference: z.string().optional().describe("New reference designator"),
      count: z.number().optional().describe("Number of duplicates (default: 1)"),
    },
    async ({ reference, offset, newReference, count }) => {
      logger.debug(`Duplicating component: ${reference}`);
      const result = await callKicadScript("duplicate_component", {
        reference,
        offset,
        newReference,
        count,
      });

      return formatKicadResult(result);
    },
  );

  logger.info("Component management tools registered");
  // Auto-place components by connectivity
  server.tool(
    "auto_place_components",
    "Auto-place components with a connectivity-driven greedy heuristic: strongly-connected parts cluster together, " +
      "decoupling capacitors hug their IC, courtyards keep the given spacing, and positions snap to the grid. " +
      "Power nets (GND/VCC/...) are ignored for affinity so they don't collapse the layout. Returns HPWL wirelength " +
      "stats; use dryRun to preview placements without moving anything. A starting point for placement, not a finished " +
      "layout — review with get_board_2d_view and refine with move_component.",
    {
      components: z
        .array(z.string())
        .optional()
        .describe(
          "References to place (default: every unlocked footprint); others stay fixed and attract",
        ),
      fixedRefs: z
        .array(z.string())
        .optional()
        .describe("References to hold in place (they still exert affinity)"),
      spacing: z
        .number()
        .optional()
        .describe("Minimum courtyard-to-courtyard gap in mm (default 1.0)"),
      grid: z.number().optional().describe("Placement grid in mm (default 0.5)"),
      area: z
        .object({ x1: z.number(), y1: z.number(), x2: z.number(), y2: z.number() })
        .optional()
        .describe("Placement area in mm (default: the board outline)"),
      dryRun: z
        .boolean()
        .optional()
        .describe("Compute placements without moving footprints (default false)"),
    },
    passthrough("auto_place_components"),
  );
}
