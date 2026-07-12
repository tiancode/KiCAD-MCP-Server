/**
 * UI/Process management tools for KiCAD MCP server
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { logger } from "../logger.js";
import { CommandFunction, formatKicadResult, makePassthrough } from "./tool-response.js";

export function registerUITools(server: McpServer, callKicadScript: CommandFunction) {
  const passthrough = makePassthrough(callKicadScript);

  // Backend info (version, capabilities) — complements get_backend_state which
  // focuses on the loaded file.  Was a Python handler with no MCP wrapper
  // until the protocol-level smoke test caught the gap.
  server.tool(
    "get_backend_info",
    "Return the active backend identifier, version, and a human-readable mode description.",
    {},
    passthrough("get_backend_info"),
  );

  // Check / launch the KiCAD UI
  server.tool(
    "manage_kicad_ui",
    "Check or launch the KiCAD UI. action='status' reports whether KiCAD is running (board/IPC ops need the PCB editor open; the server auto-heals unless KICAD_AUTO_LAUNCH=false — on needs_pcb_editor, ask the user to open the board, don't fall back to file-only edits). action='launch' starts KiCAD, optionally with a project file.",
    {
      action: z
        .enum(["status", "launch"])
        .describe("'status' checks if KiCAD UI is running; 'launch' starts it."),
      projectPath: z
        .string()
        .optional()
        .describe("Optional path to .kicad_pcb file to open (launch only)"),
      autoLaunch: z
        .boolean()
        .optional()
        .describe("Whether to launch KiCAD if not running (default: true; launch only)"),
    },
    async (args: { action: "status" | "launch"; projectPath?: string; autoLaunch?: boolean }) => {
      const { action, ...rest } = args;
      if (action === "launch") {
        logger.info(
          `Launching KiCAD UI${rest.projectPath ? " with project: " + rest.projectPath : ""}`,
        );
      }
      const command = action === "launch" ? "launch_kicad_ui" : "check_kicad_ui";
      const result = await callKicadScript(command, rest);
      return formatKicadResult(result);
    },
  );

  // -----------------------------------------------------------------
  // reconcile_backends — explicit cross-backend sync.
  //
  // The SWIG and IPC paths each hold their own copy of the board (SWIG
  // in-memory + on-disk file vs. KiCad's UI memory).  Writes from one
  // side silently invalidate the other; the dispatcher refuses cross-
  // backend mutations with `needs_reconcile: true` until this tool runs.
  // -----------------------------------------------------------------
  server.tool(
    "reconcile_backends",
    "Flush pending changes between the SWIG and IPC backends. " +
      "Use direction='ipc_to_swig' after IPC mutations (the tool calls " +
      "ipc_save_board and reloads the SWIG board from disk). " +
      "Use direction='swig_to_ipc' after SWIG/file writes (e.g. " +
      "sync_schematic_to_board) to reload KiCad's in-memory board from disk " +
      "via board.revert(); it refuses only when IPC also has unsaved changes.",
    {
      direction: z
        .enum(["ipc_to_swig", "swig_to_ipc"])
        .describe(
          "Which side has pending changes that need to land on the other. " +
            "Both directions are automatic; swig_to_ipc refuses only when IPC " +
            "also has unsaved changes (a true two-sided conflict).",
        ),
    },
    passthrough("reconcile_backends"),
  );

  // -----------------------------------------------------------------
  // run_action — escape hatch into KiCad's internal TOOL_ACTION system.
  // Action names are unstable across KiCad versions; the response carries
  // a RAS_INVALID / RAS_FRAME_NOT_OPEN status so AI callers can retry.
  // -----------------------------------------------------------------
  server.tool(
    "run_action",
    "Invoke any KiCad internal TOOL_ACTION by name (escape hatch via IPC). Action names are KiCad-internal and unstable across releases — use only when no dedicated tool exists. Returns {status, statusName} where statusName is RAS_OK / RAS_INVALID / RAS_FRAME_NOT_OPEN.",
    {
      action: z
        .string()
        .describe(
          "KiCad TOOL_ACTION name (e.g. 'pcbnew.EditorControl.zoomFitScreen'). Unstable API.",
        ),
    },
    passthrough("run_action"),
  );

  // -----------------------------------------------------------------
  // Selection / interaction tools (IPC-only).
  //
  // Identification: most tools accept items by `ids` (KIIDs from
  // get_component_list / query_traces etc.) or `references` (footprint
  // reference designators like ["R1","U2"]).  Either is fine; both work.
  // -----------------------------------------------------------------
  const itemRefSchema = {
    ids: z
      .array(z.string())
      .optional()
      .describe("Board item KIIDs (preferred). Get them from get_component_list / query_copper."),
    references: z
      .array(z.string())
      .optional()
      .describe(
        "Footprint reference designators (e.g. ['R1','U2']). Resolved against the live board.",
      ),
  };

  server.tool(
    "manage_selection",
    "Manage the KiCAD board editor selection (IPC-only). 'get' returns selected items (id, type, optional reference/value/position/layer); 'clear' deselects all; 'add'/'remove' select/deselect by `ids` and/or footprint `references` (mixable, de-duplicated).",
    {
      action: z
        .enum(["get", "clear", "add", "remove"])
        .describe("Selection operation: get | clear | add | remove."),
      ...itemRefSchema,
    },
    async (args: {
      action: "get" | "clear" | "add" | "remove";
      ids?: string[];
      references?: string[];
    }) => {
      const commandByAction = {
        get: "get_selection",
        clear: "clear_selection",
        add: "add_to_selection",
        remove: "remove_from_selection",
      } as const;
      const { action, ...rest } = args;
      const result = await callKicadScript(commandByAction[action], rest);
      return formatKicadResult(result);
    },
  );

  server.tool(
    "hit_test",
    "Find board items at (x, y) (IPC-only). With no `id` / `reference`, sweeps all footprints / tracks / vias / zones / shapes and returns every item under the point. With an `id` or `reference`, tests just that item.",
    {
      position: z
        .object({
          x: z.number(),
          y: z.number(),
          unit: z.enum(["mm", "inch"]).optional().describe("Coordinate unit (default mm)"),
        })
        .optional()
        .describe("Coordinate to test"),
      x: z.number().optional().describe("Flat X (mm). Use `position` instead when possible."),
      y: z.number().optional().describe("Flat Y (mm)."),
      tolerance: z
        .number()
        .optional()
        .describe("Hit tolerance in the same unit as the position (default 0)."),
      id: z.string().optional().describe("Optional KIID — test only this specific item."),
      reference: z
        .string()
        .optional()
        .describe("Optional footprint reference — test only that footprint."),
    },
    passthrough("hit_test"),
  );

  server.tool(
    "interactive_move",
    "Start KiCad's interactive move tool on the supplied items (IPC-only). KiCad puts the items on the cursor; the user finishes positioning by hand. Blocking — further mutating API calls return AS_BUSY until the user clicks or presses Escape, so do NOT chain another tool call right after.",
    itemRefSchema,
    passthrough("interactive_move"),
  );

  logger.info("UI + IPC management + selection tools registered");
}
