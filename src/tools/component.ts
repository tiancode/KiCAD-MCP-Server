/**
 * Component management tools for KiCAD MCP server
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { logger } from "../logger.js";
import { boundingBoxFilter, paginationParams } from "./pagination-params.js";
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

  const placeComponentSchema = {
    componentId: z
      .string()
      .describe("Footprint library ID, e.g. 'Package_DIP:DIP-8_W7.62mm' or 'R_0603_10k'."),
    position: z
      .object({
        x: z.number(),
        y: z.number(),
        unit: z.enum(["mm", "inch", "mil"]).optional().describe("Unit (default mm)"),
      })
      .describe("Position coordinates (unit defaults to mm)"),
    reference: z.string().optional().describe("Optional desired reference (e.g., 'R5')"),
    value: z.string().optional().describe("Optional component value (e.g., '10k')"),
    footprint: z.string().optional().describe("Optional specific footprint name"),
    rotation: z.number().optional().describe("Optional rotation in degrees"),
    layer: z.string().optional().describe("Optional layer (e.g., 'F.Cu', 'B.SilkS')"),
    boardPath: z
      .string()
      .optional()
      .describe("Path to the .kicad_pcb — required for project-local footprint libraries"),
  } as const;

  type PlaceComponentArgs = {
    componentId: string;
    position: { x: number; y: number; unit?: "mm" | "inch" | "mil" };
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
    "Add a NEW footprint instance to the PCB. Errors if the reference already exists — use move_component to relocate an existing part.",
    placeComponentSchema,
    placeComponentHandler,
  );

  server.tool(
    "move_component",
    "Move a PCB component to a new position. Optionally update rotation or flip to a different copper layer.",
    {
      reference: z.string().describe("Reference designator of the component (e.g., 'R5')"),
      position: z
        .object({
          x: z.number(),
          y: z.number(),
          unit: z.enum(["mm", "inch", "mil"]).optional().describe("Unit (default mm)"),
        })
        .describe("New position coordinates (unit defaults to mm)"),
      rotation: z.number().optional().describe("Optional new rotation in degrees"),
      layer: z
        .string()
        .optional()
        .describe("Optional target layer (e.g., 'F.Cu', 'B.Cu') - flips component if needed"),
      allowOffBoard: z
        .boolean()
        .optional()
        .describe(
          "Allow moving the component outside the board outline. Off-board targets are refused by default (errorCode POSITION_OFF_BOARD); set true to place a part off the board intentionally.",
        ),
    },
    async ({ reference, position, rotation, layer, allowOffBoard }) => {
      logger.debug(
        `Moving component: ${reference} to ${position.x},${position.y} ${position.unit}`,
      );
      const result = await callKicadScript("move_component", {
        reference,
        position,
        rotation,
        layer,
        allowOffBoard,
      });

      return formatKicadResult(result);
    },
  );

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

  server.tool(
    "find_component",
    "Search components on the loaded PCB (board, not schematic). `query` is a case-insensitive substring matched across reference, value AND footprint-id; the targeted filters narrow further. All supplied criteria combine with AND. Returns position and properties.",
    {
      query: z
        .string()
        .optional()
        .describe("Free-text substring matched across reference, value and footprint-id"),
      reference: z.string().optional().describe("Reference designator substring to match"),
      value: z.string().optional().describe("Component value substring to match"),
      footprint: z.string().optional().describe("Footprint-id substring to match"),
    },
    async ({ query, reference, value, footprint }) => {
      logger.debug(`Finding component (query=${query ?? ""} ref=${reference ?? ""})`);
      const result = await callKicadScript("find_component", {
        query,
        reference,
        value,
        footprint,
      });

      return formatKicadResult(result);
    },
  );

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

  server.tool(
    "add_component_annotation",
    "Add a text annotation/comment near a PCB component: places a PCB_TEXT at the component's position (plus optional offset) on a silkscreen or comments layer. Use offset to sit the label beside the part instead of on top of it.",
    {
      reference: z
        .string()
        .describe("Reference designator of the component to annotate (e.g., 'R5')"),
      text: z.string().describe("Annotation / comment text"),
      layer: z
        .string()
        .optional()
        .describe(
          "Target layer (default 'F.Silkscreen'); e.g. 'B.Silkscreen', 'User.Comments', 'F.Fab'. Legacy short names ('F.SilkS') are also accepted.",
        ),
      offset: z
        .object({
          x: z.number(),
          y: z.number(),
          unit: z.enum(["mm", "inch", "mil"]).optional(),
        })
        .optional()
        .describe("Offset from the component origin (default {0,0} mm — text lands at the part)"),
      size: z.number().optional().describe("Text height in mm (default 1.0)"),
    },
    passthrough("add_component_annotation"),
  );

  server.tool(
    "group_components",
    "Group PCB components into a named PCB_GROUP for easier selection. Refuses (creating nothing) if any reference is unknown. A component already in another group is moved into the new one; a group left empty by that move is removed — both reported.",
    {
      references: z
        .array(z.string())
        .describe("Reference designators to group (e.g., ['R1','R2'])"),
      groupName: z.string().describe("Name for the new group"),
    },
    passthrough("group_components"),
  );

  // ------------------------------------------------------
  // Replace Component Tool (DESTRUCTIVE)
  // ------------------------------------------------------
  server.tool(
    "replace_component",
    "Swap a placed footprint for a different library footprint. DESTRUCTIVE: deletes the old part and adds the new one, preserving reference, position, rotation and board side, and transferring pad nets by pad number. Pads that don't match on either side are reported.",
    {
      reference: z
        .string()
        .describe("Reference designator of the component to replace (e.g., 'U1')"),
      newFootprint: z
        .string()
        .describe("New footprint library id ('Library:Footprint' or bare 'Footprint')"),
      newValue: z
        .string()
        .optional()
        .describe("Optional new component value (defaults to the old value)"),
    },
    passthrough("replace_component"),
  );

  server.tool(
    "get_component_pads",
    "Return all pads of a component with exact positions, nets and sizes — use before routing; pass pad for just one.",
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
  // Edit Component Pad Tool
  //
  // Repairs pads on a PLACED footprint — broken library footprints
  // (e.g. easyeda BAT-SMD_CR1220-2: thru-hole pads with EMPTY numbers
  // and copper diameter == drill diameter) are otherwise unfixable:
  // edit_footprint_pad only patches .kicad_mod files and only numbered
  // pads. Targets by number OR zero-based index; refuses zero/negative
  // annular ring unless forced.
  // ------------------------------------------------------
  const padSizeSchema = z.union([
    z.number().describe("Uniform size (round/square)"),
    z.object({ x: z.number(), y: z.number() }),
    z.object({ w: z.number(), h: z.number() }),
  ]);
  server.tool(
    "edit_component_pad",
    "Edit pads of a PLACED footprint on the PCB (fix broken library footprints): copper size, drill, shape, pad number, pad type. Target by padNumber ('' matches unnumbered pads) or zero-based padIndex; several matches need all=true. Refuses a resulting annular ring <= 0 (copper<=drill) unless force=true. For .kicad_mod library files use edit_footprint_pad.",
    {
      reference: z.string().describe("Reference designator of the placed component (e.g. 'BT1')"),
      padNumber: z
        .union([z.string(), z.number()])
        .optional()
        .describe("Pad number to match; pass '' to target pads with empty numbers"),
      padIndex: z
        .number()
        .int()
        .optional()
        .describe("Zero-based pad index in get_component_pads order (works for unnumbered pads)"),
      padType: z
        .enum(["smd", "through_hole", "thru_hole", "npth", "connector"])
        .optional()
        .describe("Filter matches by pad type (or sole selector)"),
      size: padSizeSchema.optional().describe("New copper size in mm (number, or {x,y} / {w,h})"),
      drill: padSizeSchema
        .optional()
        .describe("New drill in mm (number = round; {x,y} or {w,h} = oval)"),
      shape: z.enum(["circle", "rect", "oval", "roundrect"]).optional().describe("New pad shape"),
      newPadNumber: z
        .union([z.string(), z.number()])
        .optional()
        .describe("Assign a pad number (fixes empty-number pads)"),
      newPadType: z
        .enum(["smd", "thru_hole", "npth", "connector"])
        .optional()
        .describe("Convert pad type"),
      all: z.boolean().optional().describe("Edit every matching pad (default: refuse multi-match)"),
      force: z.boolean().optional().describe("Allow zero/negative annular ring result"),
      unit: z.enum(["mm", "mil", "inch"]).optional().describe("Unit for sizes (default mm)"),
    },
    passthrough("edit_component_pad"),
  );

  server.tool(
    "get_component_list",
    "Return a list of all components on the PCB, optionally filtered by layer or bounding box region.",
    {
      layer: z.string().optional().describe("Filter by layer (e.g., 'F.Cu', 'B.Cu')"),
      boundingBox: boundingBoxFilter,
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

  server.tool(
    "place_component_array",
    "Place a rectangular grid array of identical components on the PCB with configurable row/column spacing.",
    {
      componentId: z.string().describe("Component identifier"),
      startPosition: z
        .object({
          x: z.number(),
          y: z.number(),
          unit: z.enum(["mm", "inch", "mil"]).optional().describe("Unit (default mm)"),
        })
        .describe("Starting (top-left) position"),
      rows: z.number().describe("Number of rows"),
      columns: z.number().describe("Number of columns"),
      rowSpacing: z.number().describe("Spacing between rows (mm)"),
      columnSpacing: z.number().describe("Spacing between columns (mm)"),
      startReference: z
        .string()
        .optional()
        .describe("Starting reference; a trailing number is the first index (e.g. 'C5' → C5,C6,…)"),
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

  server.tool(
    "align_components",
    "Align multiple PCB components onto a common line: 'horizontal' shares one Y, 'vertical' shares one X, 'edge' snaps to a board edge. With `spacing` the parts are also evenly spaced (no overlap); with `referenceComponent` that part stays fixed and both the shared axis and the spacing sequence are anchored to it.",
    {
      references: z.array(z.string()).describe("Array of component references to align"),
      alignmentType: z
        .enum(["horizontal", "vertical", "edge"])
        .describe("Type of alignment: horizontal (shared Y), vertical (shared X), or edge"),
      spacing: z
        .number()
        .optional()
        .describe("Even spacing between adjacent components in mm (prevents overlap)"),
      referenceComponent: z
        .string()
        .optional()
        .describe("Anchor component: it stays fixed; the shared axis and spacing start from it"),
      edge: z
        .enum(["left", "right", "top", "bottom"])
        .optional()
        .describe("Board edge to align to (required when alignmentType is 'edge')"),
    },
    async ({ references, alignmentType, spacing, referenceComponent, edge }) => {
      logger.debug(`Aligning components: ${references.join(", ")}`);
      const result = await callKicadScript("align_components", {
        references,
        alignmentType,
        spacing,
        referenceComponent,
        edge,
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
    "Detect courtyard overlaps between footprints (read-only). Pass `positions` to validate hypothetical placements before committing. Returns overlap pairs with intersection extents (mm) and per-component boundary violations.",
    {
      positions: z
        .record(z.string(), z.array(z.number()).min(2).max(3))
        .optional()
        .describe(
          "Map ref -> [x, y] or [x, y, rotation_deg] in mm; listed refs are checked AS IF there, others use their board position",
        ),
      refs: z
        .array(z.string())
        .optional()
        .describe("Limit the check to these refs (default: every footprint on the board)."),
      margin: z
        .number()
        .optional()
        .describe(
          "Extra clearance in mm around every courtyard (default 0), e.g. a manufacturing keepout",
        ),
      include_boundary: z
        .boolean()
        .optional()
        .describe("Also flag courtyards past the board outline (default true)."),
      board_outline: z
        .object({
          x1: z.number(),
          y1: z.number(),
          x2: z.number(),
          y2: z.number(),
          unit: z.enum(["mm", "inch"]).optional(),
        })
        .optional()
        .describe("Board outline bbox override (default: derived from Edge.Cuts)."),
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
  server.tool(
    "auto_place_components",
    "Auto-place components with a connectivity-driven greedy heuristic: connected parts cluster, decoupling caps " +
      "hug their IC, courtyards keep spacing, positions snap to grid; power nets ignored for affinity. Returns HPWL " +
      "wirelength stats. A starting point, not a finished layout — review with get_board_2d_view and refine.",
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
      includeMechanical: z
        .boolean()
        .optional()
        .describe(
          "Also relocate netless mechanical footprints (mounting holes H/MH, fiducials FID, test points TP). " +
            "Default false: they stay fixed and are reported in skipped_mechanical",
        ),
    },
    passthrough("auto_place_components"),
  );
}
