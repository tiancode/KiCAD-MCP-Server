/**
 * Schematic Listing and overview queries tools for KiCAD MCP server.
 * Split out of the former monolithic schematic.ts.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { paginationParams } from "../pagination-params.js";
import { CommandFunction, formatKicadResult, makePassthrough } from "../tool-response.js";

export function registerSchematicQueryTools(server: McpServer, callKicadScript: CommandFunction) {
  const passthrough = makePassthrough(callKicadScript);
  // One-shot schematic snapshot — components + wires + labels + nets in a
  // single response. Cuts 3 MCP round-trips out of basic schematic inspection.
  server.tool(
    "get_schematic_overview",
    "One-shot snapshot of a schematic: components, wires, labels, and nets in a single response. Use this instead of several list_schematic_items calls.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
    },
    passthrough("get_schematic_overview"),
  );

  // Unified listing tool — replaces list_schematic_components / _nets /
  // _wires / _labels / _texts. Dispatches to the original python commands.
  const LIST_COMMANDS = {
    components: "list_schematic_components",
    nets: "list_schematic_nets",
    wires: "list_schematic_wires",
    labels: "list_schematic_labels",
    texts: "list_schematic_texts",
  } as const;

  server.tool(
    "list_schematic_items",
    "List one kind of schematic item: components, nets (with connections), wires, labels, or texts. Kind-specific filters: filter (components), netName/labelType (labels), text (texts). Paginated.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      kind: z
        .enum(["components", "nets", "wires", "labels", "texts"])
        .describe("Which kind of schematic item to list"),
      filter: z
        .object({
          libId: z.string().optional().describe("Filter by library ID (e.g., 'Device:R')"),
          referencePrefix: z
            .string()
            .optional()
            .describe("Filter by reference prefix (e.g., 'R', 'C', 'U')"),
        })
        .optional()
        .describe("kind='components' only: optional filters."),
      netName: z
        .string()
        .optional()
        .describe("kind='labels' only: exact, case-sensitive label name filter."),
      labelType: z
        .enum(["net", "global", "power"])
        .optional()
        .describe("kind='labels' only: net=local label, global=global label, power=power symbol."),
      text: z.string().optional().describe("kind='texts' only: case-insensitive substring filter."),
      ...paginationParams,
    },
    async (args: {
      schematicPath: string;
      kind: "components" | "nets" | "wires" | "labels" | "texts";
      filter?: { libId?: string; referencePrefix?: string };
      netName?: string;
      labelType?: "net" | "global" | "power";
      text?: string;
      limit?: number;
      offset?: number;
    }) => {
      const { kind, ...params } = args;
      return formatKicadResult(await callKicadScript(LIST_COMMANDS[kind], params));
    },
  );

  // Unified layout-check tool — replaces find_overlapping_elements,
  // find_wires_crossing_symbols, list_floating_labels, and
  // find_orphaned_wires. Runs each selected python command in sequence.
  const CHECK_COMMANDS = {
    overlaps: "find_overlapping_elements",
    wires_crossing_symbols: "find_wires_crossing_symbols",
    floating_labels: "list_floating_labels",
    orphaned_wires: "find_orphaned_wires",
  } as const;

  server.tool(
    "check_schematic_layout",
    "Run schematic layout sanity checks: overlaps (stacked symbols/labels, collinear wires), wires_crossing_symbols, floating_labels (not reaching any pin), orphaned_wires (dangling endpoints). All four by default. Returns per-check results.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch schematic file"),
      checks: z
        .array(z.enum(["overlaps", "wires_crossing_symbols", "floating_labels", "orphaned_wires"]))
        .optional()
        .describe("Which checks to run. Omit to run all four."),
      tolerance: z
        .number()
        .optional()
        .describe(
          "'overlaps' only: mm threshold for label proximity and wire collinearity (default 0.5).",
        ),
    },
    async (args: {
      schematicPath: string;
      checks?: ("overlaps" | "wires_crossing_symbols" | "floating_labels" | "orphaned_wires")[];
      tolerance?: number;
    }) => {
      const { checks, ...params } = args;
      const selected =
        checks && checks.length > 0
          ? checks
          : (Object.keys(CHECK_COMMANDS) as (keyof typeof CHECK_COMMANDS)[]);
      const results: Record<string, unknown> = {};
      let success = true;
      for (const check of selected) {
        const result = await callKicadScript(CHECK_COMMANDS[check], params);
        results[check] = result;
        if (result?.success === false) success = false;
      }
      return formatKicadResult({ success, checks: results });
    },
  );
}
