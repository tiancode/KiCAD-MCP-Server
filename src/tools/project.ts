/**
 * Project management tools for KiCAD MCP server
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { formatKicadResult, makePassthrough } from "./tool-response.js";

export function registerProjectTools(server: McpServer, callKicadScript: Function) {
  const passthrough = makePassthrough(callKicadScript);
  // Create project tool
  server.tool(
    "create_project",
    "Create a new KiCAD project. Auto-launches the KiCAD UI by default so the IPC backend can attach (unlocks realtime sync + transactions). Pass autoLaunch=false to skip.",
    {
      path: z.string().describe("Project directory path"),
      name: z.string().describe("Project name"),
      autoLaunch: z
        .boolean()
        .optional()
        .describe(
          "Launch the KiCAD UI for this project after creation so the IPC backend can attach. Defaults to true. Set false for headless / CI runs.",
        ),
      overwrite: z
        .boolean()
        .optional()
        .describe(
          "Replace an existing project at this path. Defaults to false: if the target .kicad_pro/.kicad_pcb/.kicad_sch already exist, the tool refuses (errorCode PROJECT_EXISTS) instead of clobbering them. Set true only when you intend to overwrite.",
        ),
    },
    passthrough("create_project"),
  );

  // Open project tool
  server.tool(
    "open_project",
    "Open an existing KiCAD project. Auto-launches the KiCAD UI by default so the IPC backend can attach (unlocks realtime sync + transactions). Pass autoLaunch=false to skip.",
    {
      filename: z.string().describe("Path to .kicad_pro or .kicad_pcb file"),
      autoLaunch: z
        .boolean()
        .optional()
        .describe(
          "Launch the KiCAD UI for this project after opening so the IPC backend can attach. Defaults to true. Set false for headless / CI runs.",
        ),
    },
    passthrough("open_project"),
  );

  // Save project tool
  server.tool(
    "save_project",
    "Save the current KiCAD project",
    {
      path: z.string().optional().describe("Optional new path to save to"),
    },
    passthrough("save_project"),
  );

  // Get project info tool
  server.tool(
    "get_project_info",
    "Get information about the current KiCAD project",
    {},
    async () => {
      const result = await callKicadScript("get_project_info", {});
      return formatKicadResult(result);
    },
  );

  // Snapshot project tool — saves a named checkpoint as PDF/image
  server.tool(
    "snapshot_project",
    "Save a named checkpoint snapshot of the current project state (renders board to PDF and records step label). Call after completing each major step — e.g. after Step 1 (schematic_ok) and Step 2 (layout_ok). Required by the demo workflow before waiting for user confirmation.",
    {
      step: z.string().describe("Step number or identifier, e.g. '1' or '2'"),
      label: z
        .string()
        .describe("Short label for this checkpoint, e.g. 'schematic_ok' or 'layout_ok'"),
      prompt: z
        .string()
        .optional()
        .describe(
          "Full prompt text to save as PROMPT_step{step}_{timestamp}.md alongside the snapshot",
        ),
    },
    passthrough("snapshot_project"),
  );
}
