/**
 * Symbol Library tools for KiCAD MCP server
 *
 * Maintenance tools for the symbol-library index and embedded schematic
 * symbols. Browse/search of symbol libraries lives in the merged generic
 * library tools (list_libraries, search_library_parts, list_library_contents,
 * get_library_part_info with type=symbol) in library.ts.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { CommandFunction } from "./tool-response.js";

export function registerSymbolLibraryTools(server: McpServer, callKicadScript: CommandFunction) {
  // Force-refresh the symbol library index after editing sym-lib-table
  // outside the MCP process (e.g. from KiCad's GUI).  Mid-session edits to
  // the table are normally picked up automatically on the next list/search
  // call via an mtime check; this tool is the explicit escape hatch when
  // automatic detection doesn't fire (atime-only filesystems, table
  // rewrites that preserve mtime, manual cache invalidation).
  server.tool(
    "refresh_symbol_libraries",
    "Force-rebuild the symbol library index from sym-lib-table on disk. Use after an external edit to the global or project table (e.g. fixing the Flatpak sandbox-only default path) when the automatic mtime-based refresh hasn't picked it up.",
    {
      projectPath: z
        .string()
        .optional()
        .describe(
          "Project dir or .kicad_pro/.kicad_pcb/.kicad_sch path (default: open project's dir)",
        ),
    },
    async (args: { projectPath?: string }) => {
      const result = await callKicadScript("refresh_symbol_libraries", args);
      if (result.success) {
        const lines = [`Rebuilt symbol library index: ${result.count} libraries`];
        if (result.source === "directory_scan_fallback") {
          lines.push(
            `Note: sym-lib-table yielded 0 usable libraries; ${
              result.fallback_libraries?.length ?? 0
            } entries came from a directory scan and aren't addressable by sym-lib-table nickname yet.`,
          );
        }
        return { content: [{ type: "text", text: lines.join("\n") }] };
      }
      return {
        content: [
          {
            type: "text",
            text: `Failed to refresh symbol libraries: ${result.message || result.errorDetails || "(no message; check Python logs)"}`,
          },
        ],
        isError: true,
      };
    },
  );

  // ------------------------------------------------------------------
  // refresh_schematic_lib_symbols — re-inject embedded lib_symbols
  // ------------------------------------------------------------------
  // The .kicad_sch file embeds a snapshot of every used symbol in its
  // ``lib_symbols`` block.  When the system .kicad_sym library is
  // updated (KiCad upgrade, hand-edit) the snapshot becomes stale and
  // kicad-cli ERC fires ``lib_symbol_mismatch`` on every affected
  // symbol.  This tool re-extracts each entry from the current
  // ``.kicad_sym`` on disk and rewrites the embedded copy.
  server.tool(
    "refresh_schematic_lib_symbols",
    "Re-inject every embedded lib_symbols entry in a .kicad_sch from the on-disk .kicad_sym, silencing kicad-cli ERC lib_symbol_mismatch warnings from stale snapshots. Returns refreshed/unchanged/missing lists. Unlike refresh_symbol_libraries (index only), this REWRITES the schematic.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file to refresh"),
    },
    async (args: { schematicPath: string }) => {
      const result = await callKicadScript("refresh_schematic_lib_symbols", args);
      if (result.success) {
        const lines = [result.message ?? "refresh_schematic_lib_symbols completed."];
        if ((result.refreshed ?? []).length > 0) {
          lines.push(`Refreshed: ${result.refreshed.join(", ")}`);
        }
        if ((result.missing ?? []).length > 0) {
          lines.push(
            `Not found on disk: ${result.missing.join(", ")} — the library file may be missing or the symbol renamed.`,
          );
        }
        return { content: [{ type: "text", text: lines.join("\n") }] };
      }
      return {
        content: [
          {
            type: "text",
            text: `Failed to refresh schematic lib_symbols: ${result.message || result.errorDetails || "(no message; check Python logs)"}`,
          },
        ],
        isError: true,
      };
    },
  );
}
