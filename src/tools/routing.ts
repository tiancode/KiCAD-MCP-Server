/**
 * Routing tools for KiCAD MCP server
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { paginationParams } from "./pagination-params.js";
import { formatKicadResult } from "./tool-response.js";

export function registerRoutingTools(server: McpServer, callKicadScript: Function) {
  // Add net tool
  server.tool(
    "add_net",
    "Create a new net on the PCB",
    {
      name: z.string().describe("Net name"),
      netClass: z.string().optional().describe("Net class name"),
    },
    async (args: { name: string; netClass?: string }) => {
      const result = await callKicadScript("add_net", args);
      return formatKicadResult(result);
    },
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
    async (args: any) => {
      const result = await callKicadScript("route_trace", args);
      return formatKicadResult(result);
    },
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
    async (args: any) => {
      const result = await callKicadScript("route_arc_trace", args);
      return formatKicadResult(result);
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
    async (args: any) => {
      const result = await callKicadScript("add_via", args);
      return formatKicadResult(result);
    },
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
    async (args: any) => {
      const result = await callKicadScript("add_copper_pour", args);
      return formatKicadResult(result);
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
    async (args: any) => {
      const result = await callKicadScript("delete_trace", args);
      return formatKicadResult(result);
    },
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
    async (args: any) => {
      const result = await callKicadScript("query_traces", args);
      return formatKicadResult(result);
    },
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
    async (args: any) => {
      const result = await callKicadScript("query_zones", args);
      return formatKicadResult(result);
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
    async (args: any) => {
      const result = await callKicadScript("add_gnd_stitching_vias", args);
      return formatKicadResult(result);
    },
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
    async (args: any) => {
      const result = await callKicadScript("get_nets_list", args);
      return formatKicadResult(result);
    },
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
    async (args: any) => {
      const result = await callKicadScript("modify_trace", args);
      return formatKicadResult(result);
    },
  );

  // Create netclass tool
  server.tool(
    "create_netclass",
    "Create a new net class with custom design rules.",
    {
      name: z.string().describe("Net class name"),
      traceWidth: z.number().optional().describe("Default trace width in mm"),
      clearance: z.number().optional().describe("Clearance in mm"),
      viaDiameter: z.number().optional().describe("Via diameter in mm"),
      viaDrill: z.number().optional().describe("Via drill size in mm"),
    },
    async (args: any) => {
      const result = await callKicadScript("create_netclass", args);
      return formatKicadResult(result);
    },
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
    async (args: any) => {
      const result = await callKicadScript("route_differential_pair", args);
      return formatKicadResult(result);
    },
  );

  // Refill zones tool
  server.tool(
    "refill_zones",
    "Refill all copper zones. Uses the IPC fast-path when KiCad runs with the IPC API server. Without IPC the SWIG path is refused by default (pcbnew.ZONE_FILLER segfaults / mis-fills outside KiCad); instead let KiCad fill on open (press B) — zones are already on disk and gerber export only needs the fill at export time. Pass force=true to opt into the subprocess-isolated SWIG fill anyway (the response then carries a warning).",
    {
      force: z
        .boolean()
        .optional()
        .describe(
          "Opt into the SWIG subprocess-isolated fill when IPC isn't available.  Default false — refused with success:false, requires_ipc:true and a recovery hint.  Use only when headless flows really need a filled .kicad_pcb on disk and you accept that the fill may be subtly wrong (verify with run_drc / open the gerber).",
        ),
    },
    async (args: any) => {
      const result = await callKicadScript("refill_zones", args);
      return formatKicadResult(result);
    },
  );

  // Route pad to pad tool
  server.tool(
    "route_pad_to_pad",
    `Insert ONE STRAIGHT trace segment between two component pads (plus a via if they're on different copper layers). This is NOT an autorouter — there is no obstacle avoidance, no layer switching mid-trace, no rip-up-and-retry. Despite the "route_" prefix, the tool just commits a single straight line.

If the line between the two pads crosses a third pad, the call NOW REFUSES by default (success: false, hasObstacles: true) with the obstacle list. Plan the path yourself as multiple route_trace segments that go around the obstacles, or pass force: true to override and accept the resulting DRC errors.

Looks up pad positions, detects the net from the source pad, and inserts a via if the pads are on different copper layers. Default trace width comes from the source net's netclass (falling back to the board's current track width). Still call run_drc after a batch of these to catch crossings against existing traces / zones / board edge (the gate only catches crossings against OTHER PADS).`,
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
    async (args: any) => {
      const result = await callKicadScript("route_pad_to_pad", args);
      return formatKicadResult(result);
    },
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
    async (args: any) => {
      const result = await callKicadScript("copy_routing_pattern", args);
      return formatKicadResult(result);
    },
  );
}
