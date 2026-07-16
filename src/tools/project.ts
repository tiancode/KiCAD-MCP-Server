/**
 * Project management tools for KiCAD MCP server
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { CommandFunction, formatKicadResult, makePassthrough } from "./tool-response.js";

export function registerProjectTools(server: McpServer, callKicadScript: CommandFunction) {
  const passthrough = makePassthrough(callKicadScript);
  // Create project tool
  server.tool(
    "create_project",
    "Create a new KiCAD project. Auto-launches the KiCAD UI by default so the IPC backend can attach (realtime sync + transactions); autoLaunch=false skips. Refuses (errorCode PROJECT_EXISTS) if project files already exist unless overwrite=true, or INVALID_NAME if `name` is empty/whitespace. Returns project.path (the .kicad_pro) and boardPath (the .kicad_pcb).",
    {
      path: z
        .string()
        .describe(
          "Directory in which to create <name>.kicad_pro (NOT the .kicad_pro file itself). If a .kicad_pro file path is passed, it is split into directory + name; a name that disagrees with the filename errors PROJECT_NAME_CONFLICT.",
        ),
      name: z
        .string()
        .describe(
          "Project name (without extension), used as the <name>.kicad_pro basename. Must be non-empty (an empty/whitespace name errors INVALID_NAME).",
        ),
      autoLaunch: z
        .boolean()
        .optional()
        .describe("Launch KiCAD UI after creation (default true; false for headless/CI)"),
      overwrite: z
        .boolean()
        .optional()
        .describe("Replace existing .kicad_pro/.kicad_pcb/.kicad_sch (default false: refuse)"),
    },
    passthrough("create_project"),
  );

  // Open project tool
  server.tool(
    "open_project",
    "Open an existing KiCAD project. Accepts `filename` or `path` (interchangeable): a .kicad_pro/.kicad_pcb file, or a directory containing exactly one .kicad_pro (errors NO_PROJECT_IN_DIR / AMBIGUOUS_PROJECT otherwise). A missing file errors FILE_NOT_FOUND, a non-KiCad file UNSUPPORTED_FILE, and a corrupt board PARSE_ERROR — a failed open leaves any previously-loaded project intact. On success returns project.path (the .kicad_pro) and boardPath (the .kicad_pcb). Auto-launches the KiCAD UI by default so the IPC backend can attach (realtime sync + transactions); autoLaunch=false skips.",
    {
      filename: z
        .string()
        .optional()
        .describe("Path to a .kicad_pro or .kicad_pcb file (alias of `path`)"),
      path: z
        .string()
        .optional()
        .describe(
          "Project location: a .kicad_pro/.kicad_pcb file, or the directory containing exactly one .kicad_pro. Provide either this or `filename`.",
        ),
      autoLaunch: z
        .boolean()
        .optional()
        .describe("Launch KiCAD UI after opening (default true; false for headless/CI)"),
    },
    passthrough("open_project"),
  );

  // Save project tool
  server.tool(
    "save_project",
    "Save the currently-loaded KiCAD project (the last one created/opened). The response states which board file was written (savedPath, the .kicad_pcb) and returns project.path (the .kicad_pro) + boardPath (the .kicad_pcb).",
    {
      path: z
        .string()
        .optional()
        .describe(
          "Optional save-as target (a .kicad_pro or .kicad_pcb file). Omit to save the current project in place.",
        ),
      filename: z
        .string()
        .optional()
        .describe("Alias of `path` (legacy) — optional save-as target"),
    },
    passthrough("save_project"),
  );

  // Get project info tool
  server.tool(
    "get_project_info",
    "Get information about the current KiCAD project. Returns project.path (the .kicad_pro) and boardPath (the .kicad_pcb), consistent with create_project / open_project / save_project.",
    {},
    async () => {
      const result = await callKicadScript("get_project_info", {});
      return formatKicadResult(result);
    },
  );

  // Snapshot project tool — copies the project files and, when possible,
  // renders the saved board to a PDF checkpoint.
  server.tool(
    "snapshot_project",
    "Save a named checkpoint snapshot of the project: copies the project files and renders the board to PDF when possible (returned as a `pdf` field; on failure the PDF is omitted and a note explains why), and records the step label. Call after each major step; required by the demo workflow before waiting for user confirmation.",
    {
      step: z.string().describe("Step number or identifier, e.g. '1'"),
      label: z.string().describe("Short checkpoint label, e.g. 'schematic_ok'"),
      prompt: z
        .string()
        .optional()
        .describe("Prompt text saved as PROMPT_step{step}_{timestamp}.md alongside the snapshot"),
    },
    passthrough("snapshot_project"),
  );
}
