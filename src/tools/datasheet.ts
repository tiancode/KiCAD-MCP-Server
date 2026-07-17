/**
 * Datasheet tools for KiCAD MCP server
 *
 * Enriches KiCAD schematic symbols with LCSC datasheet URLs.
 * URL schema: https://www.lcsc.com/datasheet/<LCSC#>.pdf (no API key required)
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { CommandFunction, failureResult, formatKicadResult, textResult } from "./tool-response.js";

export function registerDatasheetTools(server: McpServer, callKicadScript: CommandFunction) {
  server.tool(
    "enrich_datasheets",
    "Fill in missing Datasheet URLs: every symbol with an LCSC property and an empty/'~' Datasheet field gets " +
      "https://www.lcsc.com/datasheet/<LCSC>.pdf (constructed, no network). For a single part's URL just " +
      "build it from the LCSC number with that same pattern — no lookup needed.",
    {
      schematic_path: z.string().describe("Path to the .kicad_sch file to enrich"),
      dry_run: z
        .boolean()
        .optional()
        .default(false)
        .describe("Preview changes without writing to disk"),
    },
    async (args: { schematic_path: string; dry_run?: boolean }) => {
      const result = await callKicadScript("enrich_datasheets", args);
      if (result.success) {
        const lines: string[] = [];

        if (args.dry_run) {
          lines.push(`[DRY RUN] Schematic: ${result.schematic}\n`);
        } else {
          lines.push(`Schematic: ${result.schematic}\n`);
        }

        lines.push(`✓ Updated:         ${result.updated}`);
        lines.push(`  Already set:     ${result.already_set}`);
        lines.push(`  No LCSC number:  ${result.no_lcsc}`);
        lines.push(`  No field:        ${result.no_datasheet_field}`);

        if (result.details && result.details.length > 0) {
          lines.push("\nComponents updated:");
          for (const d of result.details) {
            // lib_symbol-sourced fills carry no LCSC number
            const ref = String(d.reference ?? "?");
            const lcsc = String(d.lcsc ?? "(lib)");
            lines.push(`  ${ref.padEnd(6)} ${lcsc.padEnd(12)} → ${d.url}`);
          }
        }

        if (result.updated === 0 && !args.dry_run) {
          lines.push("\nNo changes needed – all LCSC components already have a Datasheet URL.");
        }

        return textResult(lines.join("\n"));
      }
      return failureResult("Failed to enrich datasheets", result);
    },
  );

  server.tool(
    "get_datasheet_url",
    "Return the canonical LCSC datasheet + product URLs for an LCSC part number " +
      "(e.g. C25804). Constructed, no network call. Use to look up a single part's " +
      "datasheet link; use enrich_datasheets to fill a whole schematic.",
    {
      lcsc: z.string().describe("LCSC part number, e.g. C25804 (leading 'C' optional)"),
    },
    async (args: { lcsc: string }) => {
      return formatKicadResult(await callKicadScript("get_datasheet_url", args));
    },
  );
}
