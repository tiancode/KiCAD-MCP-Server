/**
 * Symbol Library tools for KiCAD MCP server
 * Provides search/browse access to local KiCad symbol libraries
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { paginationParams, truncationNote } from "./pagination-params.js";

export function registerSymbolLibraryTools(server: McpServer, callKicadScript: Function) {
  // List available symbol libraries
  server.tool(
    "list_symbol_libraries",
    "List the NAMES of all available SYMBOL libraries (nicknames from the global sym-lib-table, plus the project's when projectPath is supplied or a project is open). Names only — to see the symbols INSIDE one library use list_library_symbols; for the FOOTPRINT-library equivalent use list_libraries.",
    {
      projectPath: z
        .string()
        .optional()
        .describe(
          "Optional: project directory or .kicad_pro/.kicad_pcb/.kicad_sch path. Including this exposes project-scope sym-lib-table libraries.",
        ),
    },
    async (args: { projectPath?: string }) => {
      const result = await callKicadScript("list_symbol_libraries", args);
      if (result.success && result.libraries) {
        return {
          content: [
            {
              type: "text",
              text: `Found ${result.count} symbol libraries:\n${result.libraries.join("\n")}`,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text",
            text: `Failed to list symbol libraries: ${result.message || result.errorDetails || "(no message; check Python logs)"}`,
          },
        ],
      };
    },
  );

  // Search for symbols across all libraries
  server.tool(
    "search_symbols",
    `Search for symbols in local KiCAD symbol libraries.

Searches by: symbol name, LCSC ID, description, manufacturer, MPN, category.
Use this to find components already in your local libraries (e.g., JLCPCB-KiCad-Library).

Two query forms:
  - "Name"           — fuzzy match across all libraries (e.g. "LED", "ESP32", "C8734").
  - "Library:Name"   — restrict to libraries whose nickname contains 'Library'
                        (e.g. "Device:LED", "Device:R", "JLCPCB:STM32F103").

Exact-name matches always rank above description-substring matches, so
"LED" returns Device:LED first, not the 60-odd parts whose description
contains "led" as a substring of "settled" / "controlled".

Returns symbol references that can be used directly in schematics.`,
    {
      query: z
        .string()
        .describe(
          "Search query. Plain ('ESP32', 'C8734') or library-qualified ('Device:LED', 'Device:R').",
        ),
      library: z
        .string()
        .optional()
        .describe(
          "Optional: filter to a specific library name pattern (e.g. 'JLCPCB'). Takes precedence over an inline 'Library:' prefix in the query.",
        ),
      limit: z.number().optional().default(20).describe("Maximum number of results to return"),
      projectPath: z
        .string()
        .optional()
        .describe(
          "Optional: project directory or .kicad_pro/.kicad_pcb/.kicad_sch path so project-scope sym-lib-table libraries are searched too.",
        ),
    },
    async (args: { query: string; library?: string; limit?: number; projectPath?: string }) => {
      const result = await callKicadScript("search_symbols", args);
      if (result.success && result.symbols) {
        const interp = result.interpretation
          ? `(parsed as library=${result.interpretation.library!}, name=${result.interpretation.name!})\n`
          : "";
        const warning = result.warning ? `\n⚠ ${result.warning}` : "";

        if (result.symbols.length === 0) {
          return {
            content: [
              {
                type: "text",
                text: `No symbols found matching "${args.query}"${args.library ? ` in libraries matching "${args.library}"` : ""}\n${interp}${warning}`.trimEnd(),
              },
            ],
          };
        }

        const symbolList = result.symbols
          .map((s: any) => {
            const parts = [`${s.full_ref}`];
            if (s.lcsc_id) parts.push(`LCSC: ${s.lcsc_id}`);
            if (s.description) parts.push(s.description);
            else if (s.value) parts.push(s.value);
            return parts.join(" | ");
          })
          .join("\n");

        return {
          content: [
            {
              type: "text",
              text: `Found ${result.count} symbols matching "${args.query}":\n${interp}\n${symbolList}${warning}`,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text",
            text: `Failed to search symbols: ${result.message || result.errorDetails || "(no message; check Python logs)"}`,
          },
        ],
      };
    },
  );

  // List symbols in a specific library
  server.tool(
    "list_library_symbols",
    "List the SYMBOLS contained in ONE symbol library identified by its NICKNAME (e.g. 'Device'), resolved via the global/project sym-lib-table. To list the symbol libraries themselves use list_symbol_libraries; if you have a .kicad_sym FILE PATH rather than a nickname, use list_symbols_in_library.",
    {
      library: z.string().describe("Library name (e.g., 'Device', 'PCM_JLCPCB-MCUs')"),
      projectPath: z
        .string()
        .optional()
        .describe(
          "Optional: project directory or .kicad_pro/.kicad_pcb/.kicad_sch path to resolve project-scope libraries.",
        ),
      ...paginationParams,
    },
    async (args: { library: string; projectPath?: string; limit?: number; offset?: number }) => {
      const result = await callKicadScript("list_library_symbols", args);
      if (result.success && result.symbols) {
        const symbolList = result.symbols
          .map((s: any) => {
            const parts = [`  - ${s.name}`];
            if (s.lcsc_id) parts.push(`(LCSC: ${s.lcsc_id})`);
            return parts.join(" ");
          })
          .join("\n");

        return {
          content: [
            {
              type: "text",
              text: `Library "${args.library}" contains ${result.total ?? result.count} symbols:\n${symbolList}${truncationNote(result)}`,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text",
            text: `Failed to list symbols in library ${args.library}: ${result.message || result.errorDetails || "(no message; check Python logs)"}`,
          },
        ],
      };
    },
  );

  // Get detailed information about a specific symbol
  server.tool(
    "get_symbol_info",
    "Get details for a specific symbol (global, or project-scope with projectPath / an open project). Returns the pin list in the symbol's local frame (.pins[] number/name/x/y/angle/length/type) and the pin bounding box — lets you plan placement coordinates before add_schematic_component without a round-trip through get_schematic_pin_locations.",
    {
      symbol: z
        .string()
        .describe("Symbol specification (e.g., 'Device:R' or 'PCM_JLCPCB-MCUs:STM32F103C8T6')"),
      projectPath: z
        .string()
        .optional()
        .describe(
          "Optional: project directory or .kicad_pro/.kicad_pcb/.kicad_sch path so project-scope libraries are searched.",
        ),
    },
    async (args: { symbol: string; projectPath?: string }) => {
      const result = await callKicadScript("get_symbol_info", args);
      if (result.success && result.symbol_info) {
        const info = result.symbol_info;
        const details = [
          `Symbol: ${info.full_ref}`,
          info.value ? `Value: ${info.value}` : "",
          info.description ? `Description: ${info.description}` : "",
          info.lcsc_id ? `LCSC: ${info.lcsc_id}` : "",
          info.manufacturer ? `Manufacturer: ${info.manufacturer}` : "",
          info.mpn ? `MPN: ${info.mpn}` : "",
          info.footprint ? `Footprint: ${info.footprint}` : "",
          info.category ? `Category: ${info.category}` : "",
          info.lib_class ? `Class: ${info.lib_class}` : "",
          info.datasheet ? `Datasheet: ${info.datasheet}` : "",
          info.sim_pins ? `Sim.Pins: ${info.sim_pins}` : "",
          info.pin_count !== undefined ? `Pins: ${info.pin_count}` : "",
        ]
          .filter((line) => line)
          .join("\n");

        // Inline a compact pin table when present.
        const pins: any[] = info.pins || [];
        const pinTable =
          pins.length > 0
            ? "\nPins (local coords, mm):\n" +
              pins
                .map(
                  (p: any) =>
                    `  ${p.number}  ${p.name ?? ""}  @ (${p.x}, ${p.y})  angle=${p.angle ?? 0}  ${p.type ?? ""}`,
                )
                .join("\n")
            : "";
        const fullDetails = details + pinTable;

        return {
          content: [
            {
              type: "text",
              text: fullDetails,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text",
            text: `Failed to get symbol info: ${result.message || result.errorDetails || "(no message; check Python logs)"}`,
          },
        ],
      };
    },
  );

  // Force-refresh the symbol library index after editing sym-lib-table
  // outside the MCP process (e.g. from KiCad's GUI).  Mid-session edits to
  // the table are normally picked up automatically on the next list/search
  // call via an mtime check; this tool is the explicit escape hatch when
  // automatic detection doesn't fire (atime-only filesystems, table
  // rewrites that preserve mtime, manual cache invalidation).
  server.tool(
    "refresh_symbol_libraries",
    "Force-rebuild the symbol library index from sym-lib-table on disk. Use after editing the global or project sym-lib-table (e.g. to fix the Flatpak default that points to a sandbox-only path) when the automatic mtime-based refresh hasn't picked up the change.",
    {
      projectPath: z
        .string()
        .optional()
        .describe(
          "Optional: project directory or .kicad_pro/.kicad_pcb/.kicad_sch path. Defaults to the currently-open project's directory.",
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
    "Re-inject every embedded lib_symbols entry in a .kicad_sch from the on-disk .kicad_sym. Silences kicad-cli ERC lib_symbol_mismatch warnings from stale snapshots after a library upgrade or hand-edit. Returns refreshed/unchanged/missing lists by Library:Name. Unlike refresh_symbol_libraries (which only rebuilds the MCP index), this rewrites the schematic.",
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
      };
    },
  );
}
