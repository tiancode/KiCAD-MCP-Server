/**
 * UI/Process management tools for KiCAD MCP server
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { logger } from "../logger.js";
import { formatKicadResult, passthroughCall } from "./tool-response.js";

export function registerUITools(server: McpServer, callKicadScript: Function) {
  const passthrough = (command: string) =>
    passthroughCall(callKicadScript as Parameters<typeof passthroughCall>[0], command);

  // Get MCP/KiCAD backend and loaded file state
  server.tool(
    "get_backend_state",
    "Return the active backend, realtime status, loaded project/board paths, and dirty state.",
    {},
    passthrough("get_backend_state"),
  );

  // Backend info (version, capabilities) — complements get_backend_state which
  // focuses on the loaded file.  Was a Python handler with no MCP wrapper
  // until the protocol-level smoke test caught the gap.
  server.tool(
    "get_backend_info",
    "Return the active backend identifier, version, and a human-readable mode description.",
    {},
    passthrough("get_backend_info"),
  );

  // Check if KiCAD UI is running
  server.tool(
    "check_kicad_ui",
    "Check if KiCAD UI is currently running. Board/IPC operations require the user to have the editor open — the server never auto-launches it; when a board op needs the editor and it's closed, ask the user to open it and wait for confirmation rather than launching it or falling back to file-only edits.",
    {},
    passthrough("check_kicad_ui"),
  );

  // Launch KiCAD UI
  server.tool(
    "launch_kicad_ui",
    "Launch KiCAD UI, optionally with a project file",
    {
      projectPath: z.string().optional().describe("Optional path to .kicad_pcb file to open"),
      autoLaunch: z
        .boolean()
        .optional()
        .describe("Whether to launch KiCAD if not running (default: true)"),
    },
    async (args: { projectPath?: string; autoLaunch?: boolean }) => {
      logger.info(
        `Launching KiCAD UI${args.projectPath ? " with project: " + args.projectPath : ""}`,
      );
      const result = await callKicadScript("launch_kicad_ui", args);
      return formatKicadResult(result);
    },
  );

  // -----------------------------------------------------------------
  // IPC-realtime command tools.  These map 1:1 to the `_ipc_*` Python
  // handlers — they run only when KiCAD is open with the IPC API server
  // enabled, and the change is reflected in the UI immediately.  Use the
  // regular tools (route_trace, add_via, …) for the universal path; reach
  // for these when you specifically need to inspect IPC state or force
  // the IPC code path for debugging.
  //
  // Editor-open requirement: these (and any board op) need KiCAD open with
  // the board loaded.  The server auto-launches KiCAD and auto-opens the
  // board when needed (opt out with KICAD_AUTO_LAUNCH=false); only when that
  // fails does the call return `needs_pcb_editor: true` — then ask the user
  // to open the board and wait, don't work around it with file-only edits.
  // -----------------------------------------------------------------
  server.tool(
    "ipc_add_track",
    "Add a track via the IPC backend (real-time).  Most callers should use route_trace instead; this tool exposes the raw IPC path for debugging.",
    {
      startX: z.number().describe("Start X (mm)"),
      startY: z.number().describe("Start Y (mm)"),
      endX: z.number().describe("End X (mm)"),
      endY: z.number().describe("End Y (mm)"),
      width: z.number().optional().describe("Track width in mm (default 0.25)"),
      layer: z.string().optional().describe("Layer name (default F.Cu)"),
      net: z.string().optional().describe("Net name to bind the track to"),
    },
    passthrough("ipc_add_track"),
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
  // list_components / ipc_get_tracks etc.) or `references` (footprint
  // reference designators like ["R1","U2"]).  Either is fine; both work.
  // -----------------------------------------------------------------
  const itemRefSchema = {
    ids: z
      .array(z.string())
      .optional()
      .describe("Board item KIIDs (preferred). Get them from list_components / ipc_get_tracks."),
    references: z
      .array(z.string())
      .optional()
      .describe(
        "Footprint reference designators (e.g. ['R1','U2']). Resolved against the live board.",
      ),
  };

  server.tool(
    "manage_selection",
    "Manage the KiCAD board editor selection (IPC-only). `action`: 'get' returns the currently selected items (each with id, type, and optional reference/value/position/layer); 'clear' deselects everything; 'add' selects items by ids and/or footprint references (forms can be mixed; the resolver de-duplicates); 'remove' deselects items by ids and/or references. `ids`/`references` are used only by 'add' and 'remove'.",
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
