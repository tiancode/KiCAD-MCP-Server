/**
 * Datasheet tools for KiCAD MCP server
 *
 * Enriches KiCAD schematic symbols with LCSC datasheet URLs.
 * URL schema: https://www.lcsc.com/datasheet/<LCSC#>.pdf (no API key required)
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { CommandFunction } from "./tool-response.js";

export function registerDatasheetTools(server: McpServer, callKicadScript: CommandFunction) {
  // ── enrich_datasheets ──────────────────────────────────────────────────────
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
            lines.push(`  ${d.reference.padEnd(6)} ${d.lcsc.padEnd(12)} → ${d.url}`);
          }
        }

        if (result.updated === 0 && !args.dry_run) {
          lines.push("\nNo changes needed – all LCSC components already have a Datasheet URL.");
        }

        return { content: [{ type: "text", text: lines.join("\n") }] };
      }
      return {
        content: [
          {
            type: "text",
            text: `Failed to enrich datasheets: ${result.message || "Unknown error"}`,
          },
        ],
        isError: true,
      };
    },
  );
}
