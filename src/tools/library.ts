/**
 * Library tools for KiCAD MCP server
 *
 * Generic library access, merged across footprint and symbol libraries.
 * Every tool takes a `type` discriminator ("footprint" | "symbol") and
 * dispatches to the matching Python command:
 *
 *   list_libraries        – list library nicknames
 *   search_library_parts  – search footprints / symbols across libraries
 *   list_library_contents – list the parts inside one library
 *   get_library_part_info – details for one footprint / symbol
 *   register_library      – add a library to fp-lib-table / sym-lib-table
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { paginationParams } from "./pagination-params.js";
import { CommandFunction, formatKicadResult } from "./tool-response.js";

const typeParam = z
  .enum(["footprint", "symbol"])
  .describe("footprint (fp-lib-table / .pretty) or symbol (sym-lib-table / .kicad_sym)");

export function registerLibraryTools(server: McpServer, callKicadScript: CommandFunction) {
  // ── list_libraries ────────────────────────────────────────────────────── //
  server.tool(
    "list_libraries",
    "List the nicknames of installed libraries: fp-lib-table (footprint) or global sym-lib-table plus the project's when projectPath is given or a project is open (symbol). Names only — use list_library_contents to see the parts inside one.",
    {
      type: typeParam,
      search_paths: z
        .array(z.string())
        .optional()
        .describe("footprint only: extra library search paths"),
      projectPath: z
        .string()
        .optional()
        .describe(
          "symbol only: project dir or .kicad_pro/.kicad_pcb/.kicad_sch path to include project-scope libraries",
        ),
    },
    async (args: {
      type: "footprint" | "symbol";
      search_paths?: string[];
      projectPath?: string;
    }) => {
      const { type, ...params } = args;
      const command = type === "footprint" ? "list_libraries" : "list_symbol_libraries";
      return formatKicadResult(await callKicadScript(command, params));
    },
  );

  // ── search_library_parts ──────────────────────────────────────────────── //
  server.tool(
    "search_library_parts",
    "Search parts in local KiCAD libraries. footprint: pattern-match footprint names. symbol: matches name, LCSC ID, description, manufacturer, MPN or category; exact-name matches rank above description matches; returns symbol refs usable directly in schematics.",
    {
      type: typeParam,
      query: z
        .string()
        .describe(
          "footprint: name pattern. symbol: plain ('ESP32', 'C8734') or 'Library:Name' where Library is a nickname substring (e.g. 'Device:LED').",
        ),
      library: z
        .string()
        .optional()
        .describe(
          "Restrict to a library (name pattern); symbol: overrides an inline 'Library:' prefix",
        ),
      limit: z.number().optional().describe("Max results (default: 50 footprint, 20 symbol)"),
      projectPath: z
        .string()
        .optional()
        .describe(
          "symbol only: project dir or .kicad_pro/.kicad_pcb/.kicad_sch path to search project-scope libraries too",
        ),
    },
    async (args: {
      type: "footprint" | "symbol";
      query: string;
      library?: string;
      limit?: number;
      projectPath?: string;
    }) => {
      const { type, query, library, limit, projectPath } = args;
      if (type === "footprint") {
        return formatKicadResult(
          await callKicadScript("search_footprints", {
            pattern: query,
            library,
            limit: limit ?? 50,
          }),
        );
      }
      return formatKicadResult(
        await callKicadScript("search_symbols", {
          query,
          library,
          limit: limit ?? 20,
          projectPath,
        }),
      );
    },
  );

  // ── list_library_contents ─────────────────────────────────────────────── //
  server.tool(
    "list_library_contents",
    "List the parts inside ONE library identified by its NICKNAME, resolved via the fp-lib-table / global+project sym-lib-table. If you have a .kicad_sym FILE PATH rather than a nickname, use list_symbols_in_library instead.",
    {
      type: typeParam,
      library: z
        .string()
        .describe("Library nickname (e.g. 'Resistor_SMD' for footprints, 'Device' for symbols)"),
      filter: z.string().optional().describe("footprint only: filter pattern for names"),
      projectPath: z
        .string()
        .optional()
        .describe(
          "symbol only: project dir or .kicad_pro/.kicad_pcb/.kicad_sch path to resolve project-scope libraries",
        ),
      ...paginationParams,
    },
    async (args: {
      type: "footprint" | "symbol";
      library: string;
      filter?: string;
      projectPath?: string;
      limit?: number;
      offset?: number;
    }) => {
      const { type, library, filter, projectPath, limit, offset } = args;
      if (type === "footprint") {
        return formatKicadResult(
          await callKicadScript("list_library_footprints", {
            library_name: library,
            filter,
            limit: limit ?? 100,
            offset,
          }),
        );
      }
      return formatKicadResult(
        await callKicadScript("list_library_symbols", { library, projectPath, limit, offset }),
      );
    },
  );

  // ── get_library_part_info ─────────────────────────────────────────────── //
  server.tool(
    "get_library_part_info",
    "Get details for one library part. footprint: description, keywords, pads, layers, courtyard size, attributes. symbol: properties (value, LCSC, MPN, footprint, datasheet) plus the pin list in the symbol's local frame and pin bounding box — enough to plan placement before add_schematic_component.",
    {
      type: typeParam,
      library: z
        .string()
        .describe("Library nickname containing the part (e.g. 'Resistor_SMD', 'Device')"),
      name: z
        .string()
        .describe("Part name within the library (e.g. 'R_0603_1608Metric', 'R', 'STM32F103C8T6')"),
      projectPath: z
        .string()
        .optional()
        .describe(
          "symbol only: project dir or .kicad_pro/.kicad_pcb/.kicad_sch path to search project-scope libraries",
        ),
    },
    async (args: {
      type: "footprint" | "symbol";
      library: string;
      name: string;
      projectPath?: string;
    }) => {
      const { type, library, name, projectPath } = args;
      if (type === "footprint") {
        return formatKicadResult(
          await callKicadScript("get_footprint_info", {
            library_name: library,
            footprint_name: name,
          }),
        );
      }
      return formatKicadResult(
        await callKicadScript("get_symbol_info", { symbol: `${library}:${name}`, projectPath }),
      );
    },
  );

  // ── register_library ──────────────────────────────────────────────────── //
  server.tool(
    "register_library",
    "Register a library in KiCAD's library table: footprint adds a .pretty directory to fp-lib-table, symbol adds a .kicad_sym file to sym-lib-table. Run after create_footprint / create_symbol when KiCAD shows 'library not found'.",
    {
      type: typeParam,
      libraryPath: z
        .string()
        .describe("Full path to the .pretty directory (footprint) or .kicad_sym file (symbol)"),
      libraryName: z
        .string()
        .optional()
        .describe("Nickname in KiCAD (default: file/directory name without extension)"),
      description: z.string().optional().describe("Optional description"),
      scope: z
        .enum(["project", "global"])
        .optional()
        .describe(
          "project (default) = lib table next to the .kicad_pro; global = user's KiCAD config",
        ),
      projectPath: z
        .string()
        .optional()
        .describe(
          "Path to .kicad_pro or its dir (required for scope=project when the library is outside the project folder)",
        ),
    },
    async (args: {
      type: "footprint" | "symbol";
      libraryPath: string;
      libraryName?: string;
      description?: string;
      scope?: "project" | "global";
      projectPath?: string;
    }) => {
      const { type, ...params } = args;
      const command =
        type === "footprint" ? "register_footprint_library" : "register_symbol_library";
      return formatKicadResult(await callKicadScript(command, params));
    },
  );
}
