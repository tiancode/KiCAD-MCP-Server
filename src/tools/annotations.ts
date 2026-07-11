/**
 * MCP tool annotations (behaviour hints) for every KiCAD tool.
 *
 * The registrar files call `server.tool(name, description, schema, handler)`
 * — the deprecated overload that carries no annotations.  Rather than rewrite
 * all ~165 call sites across 17 files, `withToolAnnotations(server)` returns a
 * Proxy whose `.tool()` trap forwards to `server.registerTool()` and injects
 * the hints classified here.  Resources and prompts keep the real server.
 *
 * The hints are exactly the five MCP `ToolAnnotations` fields:
 *   - readOnlyHint    — the tool does not modify the project / board / files.
 *   - destructiveHint — (only meaningful when not read-only) may delete or
 *                       overwrite existing data.
 *   - idempotentHint  — (only meaningful when not read-only) calling again
 *                       with the same args leaves the same end state.
 *   - openWorldHint   — interacts with external systems (network: JLCPCB /
 *                       LCSC catalogs, datasheet fetches).
 *
 * Annotations are HINTS, not guarantees (per the MCP spec) — clients use them
 * to decide auto-approval ("only-read tools run without a prompt") and to let
 * the model reason about safety / retryability.  Classification is by an
 * explicit override set first, then a name-prefix heuristic, so a new tool
 * gets a sane default the moment it is registered.
 */

import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import type { ToolAnnotations } from "@modelcontextprotocol/sdk/types.js";

/** Prefixes that mark a tool as read-only (pure query / inspection). */
const READ_ONLY_PREFIXES = ["get_", "list_", "search_", "query_", "find_", "check_"];

/**
 * Read-only tools whose name does not start with a read-only prefix.
 * `run_drc` / `run_erc` execute a rule check and return a report without
 * persisting board changes; `hit_test` probes geometry.
 */
const READ_ONLY_EXACT = new Set<string>(["run_drc", "run_erc", "hit_test"]);

/**
 * Tools that delete or overwrite existing data.  `delete_*` / `remove_*` are
 * caught by prefix; these are the destructive ops that aren't:
 *   - create_project / create_schematic write their target .kicad_pcb /
 *     .kicad_sch with NO existence guard (commands/project.py SaveBoard +
 *     commands/schematic.py shutil.copy), so a name collision silently
 *     clobbers an existing board / sheet. (create_footprint / create_symbol
 *     are NOT here — they default overwrite=False and refuse if present.)
 *   - autoroute imports the SES and overwrites all existing routing.
 */
const DESTRUCTIVE_EXACT = new Set<string>(["create_project", "create_schematic", "autoroute"]);

/**
 * Mutating tools that converge to a fixed end state — calling twice with the
 * same args is a no-op the second time.  (Only set for non-read-only tools;
 * read-only tools are idempotent by definition and don't need the hint.)
 */
const IDEMPOTENT_EXACT = new Set<string>([
  // Project lifecycle
  "open_project",
  "save_project",
  // Absolute-value setters
  "set_board_size",
  "set_active_layer",
  "set_origin",
  "set_title_block_info",
  "set_design_rules",
  "set_schematic_component_property",
  // Edits that assign fixed values
  "edit_component",
  "edit_footprint_pad",
  "edit_schematic_component",
  "edit_schematic_net_label",
  // Absolute-position moves
  "move_component",
  "move_schematic_component",
  "move_schematic_net_label",
  // Converging operations
  "refill_zones",
  "sync_schematic_to_board",
  "annotate_schematic",
  "snap_to_grid",
  "assign_net_to_class",
  "reconcile_backends",
  "refresh_symbol_libraries",
  "refresh_schematic_lib_symbols",
]);

/**
 * Tools that reach an external network service (JLCPCB / LCSC / EasyEDA).
 * Local SQLite reads (e.g. get_jlcpcb_database_stats) are intentionally NOT
 * here — they touch only the on-disk cache.
 */
const OPEN_WORLD_EXACT = new Set<string>([
  "download_jlcpcb_database",
  "download_jlcpcb_datasheet",
  "search_jlcpcb_parts",
  "get_jlcpcb_part",
  "suggest_jlcpcb_alternatives",
  "import_jlcpcb_symbol",
  "import_jlcpcb_symbols",
  "enrich_datasheets",
  "get_datasheet_url",
  // Freerouting in Docker mode runs `docker run eclipse-temurin:21-jre`,
  // which pulls the image from a remote registry when absent locally.
  "autoroute",
]);

/**
 * Classify a tool name into its MCP behaviour hints.
 */
function annotationsFor(name: string): ToolAnnotations {
  const annotations: ToolAnnotations = {};

  if (OPEN_WORLD_EXACT.has(name)) {
    annotations.openWorldHint = true;
  }

  const isReadOnly =
    READ_ONLY_EXACT.has(name) || READ_ONLY_PREFIXES.some((p) => name.startsWith(p));

  if (isReadOnly) {
    annotations.readOnlyHint = true;
    return annotations;
  }

  // Mutating tool — be explicit that it is not read-only so clients don't
  // fall back to an "unknown / assume read-only" default.
  annotations.readOnlyHint = false;

  const isDestructive =
    name.startsWith("delete_") || name.startsWith("remove_") || DESTRUCTIVE_EXACT.has(name);
  annotations.destructiveHint = isDestructive;

  if (!isDestructive && IDEMPOTENT_EXACT.has(name)) {
    annotations.idempotentHint = true;
  }

  return annotations;
}

/**
 * Normalize the deprecated `server.tool()` overloads the registrars use into a
 * `registerTool` config + callback, injecting the classified annotations.
 *
 * Supported shapes (all observed in src/tools/*):
 *   tool(name, description, schema, cb)
 *   tool(name, description, cb)
 *   tool(name, schema, cb)
 */
type RegisterToolConfig = {
  description?: string;
  inputSchema?: Record<string, unknown>;
  annotations?: ToolAnnotations;
};

function registerWithAnnotations(server: McpServer, args: unknown[]): unknown {
  const name = args[0] as string;
  const cb = args[args.length - 1];

  let description: string | undefined;
  let inputSchema: Record<string, unknown> | undefined;
  for (const middle of args.slice(1, -1)) {
    if (typeof middle === "string") {
      description = middle;
    } else if (middle && typeof middle === "object") {
      inputSchema = middle as Record<string, unknown>;
    }
  }

  const config: RegisterToolConfig = { annotations: annotationsFor(name) };
  if (description !== undefined) config.description = description;
  // registerTool expects a ZodRawShape (or AnySchema); the registrars pass
  // exactly the same `{ field: z.… }` shapes the deprecated overload took.
  if (inputSchema !== undefined) config.inputSchema = inputSchema;

  // The overloaded registerTool signature resolves its config param to
  // `never` for a structurally-built object, so cast through the call.
  return (server.registerTool as (n: string, c: RegisterToolConfig, cb: unknown) => unknown)(
    name,
    config,
    cb,
  );
}

/**
 * Wrap an McpServer so that tool registrars' `.tool()` calls transparently
 * acquire annotations.  Only `.tool` is intercepted; every other member is
 * delegated to the real server (bound so private fields resolve correctly).
 */
export function withToolAnnotations(server: McpServer): McpServer {
  return new Proxy(server, {
    get(target, prop, receiver) {
      if (prop === "tool") {
        return (...args: unknown[]) => registerWithAnnotations(target, args);
      }
      const value = Reflect.get(target, prop, receiver);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}
