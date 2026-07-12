/**
 * Routing tools for KiCAD MCP server
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { paginationParams } from "./pagination-params.js";
import { CommandFunction, formatKicadResult, makePassthrough } from "./tool-response.js";

export function registerRoutingTools(server: McpServer, callKicadScript: CommandFunction) {
  const passthrough = makePassthrough(callKicadScript);
  // Add net tool
  server.tool(
    "add_net",
    "Create a new net on the PCB",
    {
      name: z.string().describe("Net name"),
      netClass: z.string().optional().describe("Net class name"),
    },
    passthrough("add_net"),
  );

  // Route trace tool (straight segment, or arc when mid is given)
  server.tool(
    "route_trace",
    "Route a copper trace between two points on one layer: straight, or an arc when mid is given. Does NOT change layers — for cross-layer routes use route_smart (inserts a via).",
    {
      start: z
        .object({
          x: z.number(),
          y: z.number(),
          unit: z.string().optional(),
        })
        .describe("Start position"),
      mid: z
        .object({
          x: z.number(),
          y: z.number(),
          unit: z.string().optional(),
        })
        .optional()
        .describe("Arc midpoint — when given, routes an arc through it"),
      end: z
        .object({
          x: z.number(),
          y: z.number(),
          unit: z.string().optional(),
        })
        .describe("End position"),
      layer: z.string().describe("PCB layer"),
      width: z.number().describe("Trace width in mm"),
      net: z.string().describe("Net name"),
    },
    async (args) => {
      const command = args.mid ? "route_arc_trace" : "route_trace";
      return formatKicadResult(await callKicadScript(command, args));
    },
  );

  // Add via tool
  server.tool(
    "add_via",
    "Add a via to the PCB",
    {
      position: z
        .object({
          x: z.number(),
          y: z.number(),
          unit: z.string().optional(),
        })
        .describe("Via position"),
      net: z.string().describe("Net name"),
      viaType: z.string().optional().describe("Via type (through, blind, buried)"),
    },
    passthrough("add_via"),
  );

  // Copper pour tool (add / edit / delete / refill in one)
  server.tool(
    "copper_pour",
    "Manage copper pours (zones). add: create a pour (layer+net required; auto-refills). " +
      "edit: modify one zone selected by uuid or net/layer (fill marked stale — refill afterwards). " +
      "delete: remove matching zone(s). " +
      "refill: refill ALL zones via IPC; the SWIG fallback is subprocess-isolated — verify with run_drc.",
    {
      action: z.enum(["add", "edit", "delete", "refill"]).describe("What to do"),
      layer: z
        .string()
        .optional()
        .describe("add: pour layer (required). edit/delete: zone selector"),
      net: z.string().optional().describe("add: pour net (required). edit/delete: zone selector"),
      uuid: z
        .string()
        .optional()
        .describe("edit/delete: zone uuid from query_copper (preferred selector)"),
      clearance: z.number().optional().describe("add/edit: clearance in mm"),
      minWidth: z.number().optional().describe("add/edit: minimum fill width in mm (default 0.2)"),
      outline: z
        .array(z.object({ x: z.number(), y: z.number() }))
        .optional()
        .describe("add/edit: boundary points in mm (add default: board outline)"),
      autoRefill: z
        .boolean()
        .optional()
        .describe("add: refill after creating (default true); false for batch adds + one refill"),
      newNet: z.string().optional().describe("edit: reassign the zone to this net"),
      newLayer: z.string().optional().describe("edit: move the zone to this layer"),
      priority: z.number().optional().describe("edit: zone priority (higher fills first)"),
      fillType: z.enum(["solid", "hatched"]).optional().describe("edit: fill style"),
      padConnection: z
        .enum(["solid", "thermal", "none", "thru_hole_only"])
        .optional()
        .describe("edit: pad connection style (thermal = relief spokes)"),
      thermalGap: z.number().optional().describe("edit: thermal relief gap in mm"),
      thermalBridgeWidth: z.number().optional().describe("edit: thermal relief spoke width in mm"),
      all: z
        .boolean()
        .optional()
        .describe("delete: remove every selector match (default false: refuse on multiple)"),
      force: z
        .boolean()
        .optional()
        .describe(
          "refill: allow SWIG fill when IPC unavailable (default false: refused, requires_ipc:true)",
        ),
    },
    async (args) => {
      const { action, ...params } = args;
      const command = {
        add: "add_copper_pour",
        edit: "edit_copper_pour",
        delete: "delete_copper_pour",
        refill: "refill_zones",
      }[action];
      return formatKicadResult(await callKicadScript(command, params));
    },
  );

  // Delete trace tool
  server.tool(
    "delete_trace",
    "Delete traces from the PCB. Can delete by UUID, position, or bulk-delete all traces on a net.",
    {
      traceUuid: z.string().optional().describe("UUID of a specific trace to delete"),
      position: z
        .object({
          x: z.number(),
          y: z.number(),
          unit: z.enum(["mm", "inch", "mil"]).optional(),
        })
        .optional()
        .describe("Delete trace nearest to this position"),
      net: z.string().optional().describe("Delete all traces on this net (bulk delete)"),
      layer: z.string().optional().describe("Filter by layer when using net-based deletion"),
      includeVias: z.boolean().optional().describe("Include vias in net-based deletion"),
    },
    passthrough("delete_trace"),
  );

  // Query copper tool (traces or zones)
  server.tool(
    "query_copper",
    "Query copper: kind=traces returns trace segments (paginated, optionally vias); kind=zones returns zones/pours with net, layers, priority, fill state, bbox. Filter by net, layer, or boundingBox.",
    {
      kind: z.enum(["traces", "zones"]).describe("What to query"),
      net: z.string().optional().describe("Filter by net name"),
      layer: z
        .string()
        .optional()
        .describe("Filter by layer name (zones: matches zones that include this layer)"),
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
      unit: z.enum(["mm", "inch", "mil"]).optional().describe("traces only: unit for coordinates"),
      includeVias: z.boolean().optional().describe("traces only: also return vias (default false)"),
      ...paginationParams,
    },
    async (args) => {
      const { kind, ...params } = args;
      const command = kind === "zones" ? "query_zones" : "query_traces";
      return formatKicadResult(await callKicadScript(command, params));
    },
  );

  // ------------------------------------------------------
  // Add GND Stitching Vias Tool
  //
  // Drops GND stitching vias across the board with full-stackup
  // collision detection: every non-GND segment, via, and pad on every
  // copper layer is checked, because a PTH via penetrates the whole
  // board. Three combinable strategies: regular grid, around named
  // refs (densify under MCUs / regulators / RF parts), and in-zones
  // only (vias land on actual GND copper, not silkscreen). Supports
  // dryRun to preview placements without writing.
  //
  // Approach ported from morningfire-pcb-automation:
  //   https://github.com/NiNjA-CodE/morningfire-pcb-automation
  //   (scripts/ground/add_gnd_vias.py)
  // ------------------------------------------------------
  server.tool(
    "add_gnd_stitching_vias",
    "Drop GND stitching vias with collision checks against all non-GND copper on every layer (PTH vias span the stackup). Combinable strategies: grid, around_refs (densify around named ICs), in_zones (only inside GND zones).",
    {
      gndNet: z
        .string()
        .optional()
        .describe("Ground net name (default: auto-detect GND / GROUND / VSS / /GND)."),
      strategies: z
        .array(z.enum(["grid", "around_refs", "in_zones"]))
        .optional()
        .describe("Placement strategies to combine (default ['grid'])."),
      viaSize: z.number().optional().describe("Via pad diameter in mm (default 0.6)."),
      viaDrill: z
        .number()
        .optional()
        .describe("Drill diameter in mm (default 0.3); must be < viaSize."),
      clearance: z
        .number()
        .optional()
        .describe("Extra clearance to existing copper in mm (default 0.2)."),
      spacing: z
        .number()
        .optional()
        .describe("Grid spacing in mm for grid/around_refs (default 5.0)."),
      densifyRefs: z
        .array(z.string())
        .optional()
        .describe("around_refs: refs to densify around (e.g. MCUs, regulators, RF parts)."),
      densifyRadius: z
        .number()
        .int()
        .optional()
        .describe("Grid cells around each ref (default 2 = 5x5 field per ref)."),
      edgeMargin: z
        .number()
        .optional()
        .describe("Keep-out from the board edge in mm (default 0.5)."),
      maxVias: z.number().int().optional().describe("Cap on total placements (default unlimited)."),
      dryRun: z
        .boolean()
        .optional()
        .describe("Preview placements without modifying the board (default false)."),
    },
    passthrough("add_gnd_stitching_vias"),
  );

  // Get nets list tool
  server.tool(
    "get_nets_list",
    "Get a list of all nets in the PCB with optional statistics.",
    {
      includeStats: z
        .boolean()
        .optional()
        .describe("Include statistics (track count, total length, etc.)"),
      unit: z.enum(["mm", "mil", "inch"]).optional().describe("Unit for length measurements"),
      ...paginationParams,
    },
    passthrough("get_nets_list"),
  );

  // Modify trace tool
  server.tool(
    "modify_trace",
    "Modify an existing trace (change width, layer, or net).",
    {
      traceUuid: z.string().describe("UUID of the trace to modify"),
      width: z.number().optional().describe("New trace width in mm"),
      layer: z.string().optional().describe("New layer name"),
      net: z.string().optional().describe("New net name"),
    },
    passthrough("modify_trace"),
  );

  // Create netclass tool
  server.tool(
    "create_netclass",
    "Create or update a net class with custom design rules, persisted to the .kicad_pro (KiCad 9/10 store net classes in project JSON, not the board). Optionally assign nets by name or wildcard pattern.",
    {
      name: z.string().describe("Net class name"),
      traceWidth: z.number().optional().describe("Default trace width in mm"),
      clearance: z.number().optional().describe("Clearance in mm"),
      viaDiameter: z.number().optional().describe("Via diameter in mm"),
      viaDrill: z.number().optional().describe("Via drill size in mm"),
      nets: z
        .array(z.string())
        .optional()
        .describe("Exact net names to assign (netclass_assignments)"),
      patterns: z
        .array(z.string())
        .optional()
        .describe(
          "Wildcard patterns ('*' any, '?' one char) vs full hierarchical net name — leading '*' often needed",
        ),
    },
    passthrough("create_netclass"),
  );

  // Assign netclass pattern tool
  server.tool(
    "assign_netclass_pattern",
    "Append a wildcard pattern -> net-class rule to the .kicad_pro (netclass_patterns). '*' = any, '?' = one char; matches the full hierarchical net name, so a leading '*' is often needed.",
    {
      netClass: z.string().describe("Name of the (existing) net class to assign nets to"),
      pattern: z.string().describe("Wildcard pattern, e.g. '+24V_*' or '*VLV?_DRAIN'"),
    },
    passthrough("assign_netclass_pattern"),
  );

  // Route differential pair tool
  server.tool(
    "route_differential_pair",
    "Route a differential pair between two sets of points.",
    {
      positivePad: z
        .object({
          reference: z.string(),
          pad: z.string(),
        })
        .describe("Positive pad (component and pad number)"),
      negativePad: z
        .object({
          reference: z.string(),
          pad: z.string(),
        })
        .describe("Negative pad (component and pad number)"),
      layer: z.string().describe("PCB layer"),
      width: z.number().describe("Trace width in mm"),
      gap: z.number().describe("Gap between traces in mm"),
      positiveNet: z.string().describe("Positive net name"),
      negativeNet: z.string().describe("Negative net name"),
    },
    passthrough("route_differential_pair"),
  );

  // Smart router: A* obstacle avoidance, or a direct straight segment
  server.tool(
    "route_smart",
    "Route between two pads (or two points). strategy=astar (default): grid A* OBSTACLE AVOIDANCE around other " +
      "pads/traces/vias. strategy=direct: ONE straight segment between two pads, no avoidance — refuses if it " +
      "would cross a third pad unless force=true. Still run_drc afterwards.",
    {
      strategy: z.enum(["astar", "direct"]).optional().describe("Routing strategy (default astar)"),
      fromRef: z.string().optional().describe("Source component reference (e.g. 'U1')"),
      fromPad: z.union([z.string(), z.number()]).optional().describe("Source pad number"),
      toRef: z.string().optional().describe("Target component reference"),
      toPad: z.union([z.string(), z.number()]).optional().describe("Target pad number"),
      start: z
        .object({ x: z.number(), y: z.number() })
        .optional()
        .describe("astar: start point in mm (alternative to fromRef/fromPad)"),
      end: z
        .object({ x: z.number(), y: z.number() })
        .optional()
        .describe("astar: end point in mm (alternative to toRef/toPad)"),
      layers: z
        .array(z.string())
        .min(1)
        .max(2)
        .optional()
        .describe(
          "1-2 copper layers (default ['F.Cu']); 2 enable via layer changes; direct uses the first",
        ),
      width: z.number().optional().describe("Trace width in mm (default: netclass width)"),
      net: z
        .string()
        .optional()
        .describe("Net name override (default: auto-detected from the source pad)"),
      gridMm: z
        .number()
        .optional()
        .describe("astar: grid pitch in mm (default 0.25); increase on dense boards if no path"),
      clearance: z
        .number()
        .optional()
        .describe("astar: keep-out clearance around obstacles in mm (default 0.2)"),
      viaCost: z
        .number()
        .optional()
        .describe("astar: extra cost per layer change, in grid steps (default 20)"),
      maxNodes: z
        .number()
        .int()
        .optional()
        .describe("astar: search budget before giving up (default 200000)"),
      force: z
        .boolean()
        .optional()
        .describe(
          "direct: route even across other pads (default false: refused with obstacle list)",
        ),
    },
    async (args) => {
      const { strategy, ...params } = args;
      if (strategy === "direct") {
        const { fromRef, fromPad, toRef, toPad, width, net, force } = params;
        const layer = params.layers?.[0];
        return formatKicadResult(
          await callKicadScript("route_pad_to_pad", {
            fromRef,
            fromPad,
            toRef,
            toPad,
            width,
            net,
            force,
            ...(layer ? { layer } : {}),
          }),
        );
      }
      const { force: _force, ...astar } = params;
      void _force;
      return formatKicadResult(await callKicadScript("route_smart", astar));
    },
  );

  // Net length report tool
  server.tool(
    "report_net_lengths",
    "Report routed copper length per net (mm), segment/via counts, layers, and max skew across the selected group — " +
      "read-only basis for length matching. Via barrel length excluded (viaCount returned). " +
      "Omit nets and pattern for all routed nets.",
    {
      nets: z
        .array(z.string())
        .optional()
        .describe("Exact net names to report (skew is computed across them)"),
      pattern: z
        .string()
        .optional()
        .describe("Wildcard net-name pattern ('*' any, '?' one char); unioned with nets"),
    },
    passthrough("report_net_lengths"),
  );

  // Copy routing pattern tool
  server.tool(
    "copy_routing_pattern",
    "Copy routing (traces and vias) from a group of source components to a matching target group; offset is auto-computed from the first source/target pair. For replicating identical circuit blocks.",
    {
      sourceRefs: z.array(z.string()).describe("Source component references"),
      targetRefs: z.array(z.string()).describe("Target references, same order as sourceRefs"),
      includeVias: z.boolean().optional().describe("Also copy vias (default: true)"),
      traceWidth: z
        .number()
        .optional()
        .describe("Override trace width in mm (default: keep original)"),
    },
    passthrough("copy_routing_pattern"),
  );
}
