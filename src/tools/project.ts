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
    "Create a new KiCAD project. Auto-launches the KiCAD UI by default so the IPC backend can attach (realtime sync + transactions); autoLaunch=false skips. Refuses (errorCode PROJECT_EXISTS) if project files already exist unless overwrite=true.",
    {
      path: z.string().describe("Project directory path"),
      name: z.string().describe("Project name"),
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
    "Open an existing KiCAD project. Auto-launches the KiCAD UI by default so the IPC backend can attach (realtime sync + transactions); autoLaunch=false skips.",
    {
      filename: z.string().describe("Path to .kicad_pro or .kicad_pcb file"),
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
    "Save a named checkpoint snapshot of the project (renders board to PDF, records step label). Call after each major step; required by the demo workflow before waiting for user confirmation.",
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
