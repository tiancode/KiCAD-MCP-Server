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
  .describe(
    "Which library kind to operate on: footprint (fp-lib-table / .pretty) or symbol (sym-lib-table / .kicad_sym)",
  );

export function registerLibraryTools(server: McpServer, callKicadScript: CommandFunction) {
  // ── list_libraries ────────────────────────────────────────────────────── //
  server.tool(
    "list_libraries",
    "List the NAMES (nicknames) of installed libraries. type=footprint reads the fp-lib-table; type=symbol reads the global sym-lib-table, plus the project's when projectPath is supplied or a project is open. Names only — to see the parts INSIDE one library use list_library_contents; to discover unregistered .pretty dirs on disk use list_footprint_libraries.",
    {
      type: typeParam,
      search_paths: z
        .array(z.string())
        .optional()
        .describe("footprint only: optional additional search paths for libraries"),
      projectPath: z
        .string()
        .optional()
        .describe(
          "symbol only: project directory or .kicad_pro/.kicad_pcb/.kicad_sch path. Including this exposes project-scope sym-lib-table libraries.",
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
    "Search for parts in local KiCAD libraries. type=footprint: pattern-match footprint names. type=symbol: match by name, LCSC ID, description, manufacturer, MPN or category; query may be plain ('ESP32', 'C8734') or 'Library:Name' to restrict to libraries whose nickname contains 'Library' (e.g. 'Device:LED'); exact-name matches rank above description-substring matches; returns symbol refs usable directly in schematics.",
    {
      type: typeParam,
      query: z
        .string()
        .describe(
          "Search term or pattern. footprint: matched against footprint names. symbol: plain ('ESP32', 'C8734') or library-qualified ('Device:LED', 'Device:R').",
        ),
      library: z
        .string()
        .optional()
        .describe(
          "Optional: restrict to a specific library (name pattern). symbol: takes precedence over an inline 'Library:' prefix in the query.",
        ),
      limit: z
        .number()
        .optional()
        .describe("Maximum number of results to return (default: 50 footprint, 20 symbol)"),
      projectPath: z
        .string()
        .optional()
        .describe(
          "symbol only: project directory or .kicad_pro/.kicad_pcb/.kicad_sch path so project-scope sym-lib-table libraries are searched too.",
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
    "List the parts contained in ONE library identified by its NICKNAME (e.g. footprint 'Resistor_SMD', symbol 'Device'), resolved via the fp-lib-table / global+project sym-lib-table. To list the libraries themselves use list_libraries; if you have a .kicad_sym FILE PATH rather than a nickname, use list_symbols_in_library.",
    {
      type: typeParam,
      library: z
        .string()
        .describe("Library nickname (e.g. 'Resistor_SMD' for footprints, 'Device' for symbols)"),
      filter: z
        .string()
        .optional()
        .describe("footprint only: optional filter pattern for footprint names"),
      projectPath: z
        .string()
        .optional()
        .describe(
          "symbol only: project directory or .kicad_pro/.kicad_pcb/.kicad_sch path to resolve project-scope libraries.",
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
    "Get details for one library part. type=footprint: description, keywords, pads (number/type/shape), layers, courtyard size, attributes. type=symbol: properties (value, LCSC, manufacturer, MPN, footprint, datasheet) plus the pin list in the symbol's local frame (.pins[] number/name/x/y/angle/length/type) and pin bounding box — lets you plan placement coordinates before add_schematic_component without a round-trip through get_schematic_pin_locations.",
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
          "symbol only: project directory or .kicad_pro/.kicad_pcb/.kicad_sch path so project-scope libraries are searched.",
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
    "Register a library in KiCAD's library table so its parts can be found: type=footprint adds a .pretty directory to the fp-lib-table, type=symbol adds a .kicad_sym file to the sym-lib-table. Run this after create_footprint / create_symbol when KiCAD shows 'library not found'.",
    {
      type: typeParam,
      libraryPath: z
        .string()
        .describe(
          "Full path to the library: a .pretty directory (footprint) or a .kicad_sym file (symbol)",
        ),
      libraryName: z
        .string()
        .optional()
        .describe(
          "Nickname for the library in KiCAD (default: file/directory name without extension)",
        ),
      description: z.string().optional().describe("Optional description"),
      scope: z
        .enum(["project", "global"])
        .optional()
        .describe(
          "project = writes the lib table next to the .kicad_pro file (default); global = writes to the user's global KiCAD config",
        ),
      projectPath: z
        .string()
        .optional()
        .describe(
          "Path to the .kicad_pro file or its directory (required for scope=project when the library is not in the project folder)",
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
