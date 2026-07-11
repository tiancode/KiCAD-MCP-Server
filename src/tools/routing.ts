/**
 * Routing tools for KiCAD MCP server
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { paginationParams } from "./pagination-params.js";
import { CommandFunction, makePassthrough } from "./tool-response.js";

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

  // Route trace tool
  server.tool(
    "route_trace",
    "Route a trace segment between two XY points on a fixed layer. WARNING: Does NOT handle layer changes — if start and end are on different copper layers, use route_pad_to_pad instead, which automatically inserts a via.",
    {
      start: z
        .object({
          x: z.number(),
          y: z.number(),
          unit: z.string().optional(),
        })
        .describe("Start position"),
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
    passthrough("route_trace"),
  );

  // Route arc trace tool
  server.tool(
    "route_arc_trace",
    "Route a copper arc trace defined by start/mid/end points. Uses true PCB arc primitives when available.",
    {
      start: z
        .object({
          x: z.number(),
          y: z.number(),
          unit: z.string().optional(),
        })
        .describe("Arc start position"),
      mid: z
        .object({
          x: z.number(),
          y: z.number(),
          unit: z.string().optional(),
        })
        .describe("A point on arc midpoint"),
      end: z
        .object({
          x: z.number(),
          y: z.number(),
          unit: z.string().optional(),
        })
        .describe("Arc end position"),
      layer: z.string().describe("PCB layer"),
      width: z.number().describe("Trace width in mm"),
      net: z.string().optional().describe("Net name"),
    },
    passthrough("route_arc_trace"),
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

  // Add copper pour tool
  server.tool(
    "add_copper_pour",
    "Add a copper pour (ground/power plane) to the PCB. By default refills zones immediately so gerber export captures the fill — pass autoRefill=false to skip and call refill_zones explicitly later.",
    {
      layer: z.string().describe("PCB layer"),
      net: z.string().describe("Net name"),
      clearance: z.number().optional().describe("Clearance in mm"),
      minWidth: z.number().optional().describe("Minimum fill width in mm (default 0.2)"),
      outline: z
        .array(z.object({ x: z.number(), y: z.number() }))
        .optional()
        .describe(
          "Array of {x, y} points defining the pour boundary. If omitted, the board outline is used.",
        ),
      autoRefill: z
        .boolean()
        .optional()
        .describe(
          "Run refill_zones after creating the pour (default true). Set false for batch mode — multiple add_copper_pour calls followed by a single refill_zones at the end.",
        ),
    },
    passthrough("add_copper_pour"),
  );

  // Edit copper pour tool
  server.tool(
    "edit_copper_pour",
    "Edit an existing copper pour (zone): pad connection style, clearance, outline, net, layer, priority, fill type, thermal relief settings. Select the zone by uuid (from query_zones) or by net/layer filters matching exactly one zone. The fill is marked stale — call refill_zones afterwards (or let KiCad refill on open).",
    {
      uuid: z.string().optional().describe("Zone uuid from query_zones (preferred selector)"),
      net: z.string().optional().describe("Selector: match zones on this net"),
      layer: z.string().optional().describe("Selector: match zones on this layer (e.g. F.Cu)"),
      newNet: z.string().optional().describe("Reassign the zone to this net"),
      newLayer: z.string().optional().describe("Move the zone to this layer"),
      clearance: z.number().optional().describe("New clearance in mm"),
      minWidth: z.number().optional().describe("New minimum fill width in mm"),
      priority: z.number().optional().describe("New zone priority (higher fills first)"),
      fillType: z.enum(["solid", "hatched"]).optional().describe("New fill style"),
      padConnection: z
        .enum(["solid", "thermal", "none", "thru_hole_only"])
        .optional()
        .describe(
          "Pad connection style: solid (direct copper), thermal (relief spokes), none, or thru_hole_only (thermal on THT, solid on SMD)",
        ),
      thermalGap: z.number().optional().describe("Thermal relief gap in mm"),
      thermalBridgeWidth: z.number().optional().describe("Thermal relief spoke width in mm"),
      outline: z
        .array(z.object({ x: z.number(), y: z.number() }))
        .optional()
        .describe("Replace the zone boundary with these {x, y} points (mm, min 3)"),
    },
    passthrough("edit_copper_pour"),
  );

  // Delete copper pour tool
  server.tool(
    "delete_copper_pour",
    "Delete copper pour(s) from the PCB. Select by uuid (from query_zones) or by net/layer filters; when the filters match several zones, pass all=true to delete every match (otherwise the call is refused with the candidate list).",
    {
      uuid: z.string().optional().describe("Zone uuid from query_zones (preferred selector)"),
      net: z.string().optional().describe("Selector: match zones on this net"),
      layer: z.string().optional().describe("Selector: match zones on this layer (e.g. F.Cu)"),
      all: z
        .boolean()
        .optional()
        .describe("Delete every zone the selectors match (default false: refuse on multiple)"),
    },
    passthrough("delete_copper_pour"),
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

  // Query traces tool
  server.tool(
    "query_traces",
    "Query traces on the board with optional filters by net, layer, or bounding box.",
    {
      net: z.string().optional().describe("Filter by net name"),
      layer: z.string().optional().describe("Filter by layer name"),
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
      unit: z.enum(["mm", "inch", "mil"]).optional().describe("Unit for coordinates"),
      includeVias: z.boolean().optional().describe("Also return vias (default false)"),
      ...paginationParams,
    },
    passthrough("query_traces"),
  );

  // Query zones tool
  server.tool(
    "query_zones",
    "Query copper zones (filled pours) on the board with optional filters by net, layer, or bounding box. Returns zone net, layers, priority, fill state, and bounding box. Useful for auditing power planes and GND pours that query_traces does not include.",
    {
      net: z.string().optional().describe("Filter by net name"),
      layer: z
        .string()
        .optional()
        .describe("Filter by layer name (matches zones that include this layer)"),
      boundingBox: z
        .object({
          x1: z.number(),
          y1: z.number(),
          x2: z.number(),
          y2: z.number(),
          unit: z.enum(["mm", "inch"]).optional(),
        })
        .optional()
        .describe("Filter to zones whose bounding box overlaps this region"),
    },
    passthrough("query_zones"),
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
    "Drop GND stitching vias with collision checking against every non-GND segment/via/pad on all copper layers (PTH vias span the full stackup). Three combinable strategies: grid (regular interior grid), around_refs (densify around named ICs), in_zones (only inside a GND copper zone). dryRun previews placements without writing.",
    {
      gndNet: z
        .string()
        .optional()
        .describe("Name of the ground net (default: auto-detect GND / GROUND / VSS / /GND)."),
      strategies: z
        .array(z.enum(["grid", "around_refs", "in_zones"]))
        .optional()
        .describe(
          "Which placement strategies to combine (default: ['grid']). Pass ['grid', 'around_refs', 'in_zones'] for full coverage.",
        ),
      viaSize: z.number().optional().describe("Via pad diameter in mm (default 0.6)."),
      viaDrill: z
        .number()
        .optional()
        .describe("Via drill diameter in mm (default 0.3). Must be smaller than viaSize."),
      clearance: z
        .number()
        .optional()
        .describe(
          "Extra clearance beyond required between each new via and existing copper, in mm (default 0.2).",
        ),
      spacing: z
        .number()
        .optional()
        .describe("Grid spacing in mm for `grid` and `around_refs` strategies (default 5.0)."),
      densifyRefs: z
        .array(z.string())
        .optional()
        .describe(
          "Reference designators to densify ground around (used by `around_refs`). Targets: MCUs, switching regulators, RF parts.",
        ),
      densifyRadius: z
        .number()
        .int()
        .optional()
        .describe(
          "How many grid cells around each ref to try (default 2 = 5x5 candidate field per ref).",
        ),
      edgeMargin: z
        .number()
        .optional()
        .describe("Keep-out from the board edge in mm (default 0.5)."),
      maxVias: z
        .number()
        .int()
        .optional()
        .describe("Cap on total placements across all strategies (default unlimited)."),
      dryRun: z
        .boolean()
        .optional()
        .describe(
          "If true, return the placements that would be made but don't modify the board (default false).",
        ),
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
    "Create (or update) a net class with custom design rules and persist it to the .kicad_pro project file. In KiCad 9/10 net classes live in the project JSON, not the board, so this writes there. Optionally assign nets directly or by wildcard pattern.",
    {
      name: z.string().describe("Net class name"),
      traceWidth: z.number().optional().describe("Default trace width in mm"),
      clearance: z.number().optional().describe("Clearance in mm"),
      viaDiameter: z.number().optional().describe("Via diameter in mm"),
      viaDrill: z.number().optional().describe("Via drill size in mm"),
      nets: z
        .array(z.string())
        .optional()
        .describe("Exact net names to assign to this class (netclass_assignments)"),
      patterns: z
        .array(z.string())
        .optional()
        .describe(
          "Wildcard membership patterns (netclass_patterns). '*' = any, '?' = one char. Matches the full hierarchical net name, so a leading '*' is often needed (e.g. '*VLV?_DRAIN').",
        ),
    },
    passthrough("create_netclass"),
  );

  // Assign netclass pattern tool
  server.tool(
    "assign_netclass_pattern",
    "Append a wildcard pattern -> net-class rule to the .kicad_pro (net_settings.netclass_patterns). '*' = any, '?' = one char. Patterns match the full hierarchical net name (e.g. '/5_Valve_Drive/VLV1_DRAIN'), so a leading '*' is often needed.",
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

  // Refill zones tool
  server.tool(
    "refill_zones",
    "Refill all copper zones via the IPC fast-path when KiCad is running. Without IPC the SWIG path is REFUSED by default " +
      "(pcbnew.ZONE_FILLER segfaults/mis-fills outside KiCad — let KiCad fill on open with B instead); " +
      "force=true opts into the subprocess-isolated SWIG fill anyway (response carries a warning).",
    {
      force: z
        .boolean()
        .optional()
        .describe(
          "Opt into the SWIG fill when IPC isn't available (default false → refused with requires_ipc:true). Only for headless flows that need a filled .kicad_pcb on disk; verify the result with run_drc.",
        ),
    },
    passthrough("refill_zones"),
  );

  // Route pad to pad tool
  server.tool(
    "route_pad_to_pad",
    "Insert ONE STRAIGHT trace segment between two pads (auto-detects net; adds a via when layers differ). " +
      "NOT an autorouter — no obstacle avoidance (use autoroute for that). " +
      "REFUSES if the line would cross a third pad (hasObstacles: true + obstacle list); " +
      "route around with route_trace segments, or pass force: true. " +
      "The gate only checks other pads — still run_drc to catch trace/zone/edge crossings.",
    {
      fromRef: z.string().describe("Reference of the source component (e.g. 'U2')"),
      fromPad: z
        .union([z.string(), z.number()])
        .describe("Pad number on the source component (e.g. '6' or 6)"),
      toRef: z.string().describe("Reference of the target component (e.g. 'U1')"),
      toPad: z
        .union([z.string(), z.number()])
        .describe("Pad number on the target component (e.g. '15' or 15)"),
      layer: z.string().optional().describe("PCB layer (default: F.Cu)"),
      width: z
        .number()
        .optional()
        .describe(
          "Trace width in mm (default: netclass of the source net's track width, then board default).",
        ),
      net: z.string().optional().describe("Net name override (default: auto-detected from pad)"),
      force: z
        .boolean()
        .optional()
        .describe(
          "Insert the straight segment even when it crosses other pads (default false — the call refuses and returns obstaclesCrossed). Use only when you've decided to accept the resulting DRC errors.",
        ),
    },
    passthrough("route_pad_to_pad"),
  );

  // Smart obstacle-avoiding router
  server.tool(
    "route_smart",
    "Route between two pads (or two points) with grid A* OBSTACLE AVOIDANCE — routes around other pads/traces/vias and " +
      "can change layers through a via when two copper layers are given. Slower than route_pad_to_pad but succeeds where " +
      "a straight segment is blocked. Still run_drc afterwards; on dense boards increase gridMm if no path is found.",
    {
      fromRef: z.string().optional().describe("Source component reference (e.g. 'U1')"),
      fromPad: z.union([z.string(), z.number()]).optional().describe("Source pad number"),
      toRef: z.string().optional().describe("Target component reference"),
      toPad: z.union([z.string(), z.number()]).optional().describe("Target pad number"),
      start: z
        .object({ x: z.number(), y: z.number() })
        .optional()
        .describe("Alternative to fromRef/fromPad: start point in mm"),
      end: z
        .object({ x: z.number(), y: z.number() })
        .optional()
        .describe("Alternative to toRef/toPad: end point in mm"),
      layers: z
        .array(z.string())
        .min(1)
        .max(2)
        .optional()
        .describe(
          "1 or 2 copper layers to route on (default ['F.Cu']); 2 layers enable via layer changes",
        ),
      width: z.number().optional().describe("Trace width in mm (default: netclass width)"),
      net: z
        .string()
        .optional()
        .describe("Net name override (default: auto-detected from the source pad)"),
      gridMm: z
        .number()
        .optional()
        .describe("Routing grid pitch in mm (default 0.25); coarser = faster"),
      clearance: z
        .number()
        .optional()
        .describe("Keep-out clearance around obstacles in mm (default 0.2)"),
      viaCost: z
        .number()
        .optional()
        .describe("Extra cost per layer change, in grid steps (default 20)"),
      maxNodes: z
        .number()
        .int()
        .optional()
        .describe("Search budget before giving up (default 200000)"),
    },
    passthrough("route_smart"),
  );

  // Net length report tool
  server.tool(
    "report_net_lengths",
    "Report total routed copper length per net (mm), segment/via counts and layers, plus max skew across the selected " +
      "group — the read-only basis for length matching. Via barrel length is excluded (viaCount is returned so you can " +
      "budget it). Select nets explicitly, by wildcard pattern, or omit both for all routed nets.",
    {
      nets: z
        .array(z.string())
        .optional()
        .describe("Exact net names to report (skew is computed across them)"),
      pattern: z
        .string()
        .optional()
        .describe(
          "Wildcard net-name pattern, e.g. 'DDR_DQ*' ('*' any, '?' one char); unioned with nets",
        ),
    },
    passthrough("report_net_lengths"),
  );

  // Copy routing pattern tool
  server.tool(
    "copy_routing_pattern",
    "Copy routing pattern (traces and vias) from a group of source components to a matching group of target components. The offset is calculated automatically from the position difference between the first source and first target component. Useful for replicating routing between identical circuit blocks.",
    {
      sourceRefs: z
        .array(z.string())
        .describe("References of the source components (e.g. ['U1', 'R1', 'C1'])"),
      targetRefs: z
        .array(z.string())
        .describe(
          "References of the target components in same order as sourceRefs (e.g. ['U2', 'R2', 'C2'])",
        ),
      includeVias: z.boolean().optional().describe("Also copy vias (default: true)"),
      traceWidth: z
        .number()
        .optional()
        .describe("Override trace width in mm (default: keep original width)"),
    },
    passthrough("copy_routing_pattern"),
  );
}
