/**
 * Router Tools for KiCAD MCP Server
 *
 * Provides discovery and execution of routed tools
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { logger } from "../logger.js";
import {
  getAllCategories,
  getCategory,
  searchTools as registrySearchTools,
  getRegistryStats,
  WORKFLOWS,
} from "./registry.js";

// Command function type for KiCAD script calls
type CommandFunction = (command: string, params: Record<string, unknown>) => Promise<any>;

/**
 * Register all router tools with the MCP server
 */
export function registerRouterTools(server: McpServer, callKicadScript: CommandFunction): void {
  logger.info("Registering router tools");

  // ============================================================================
  // list_tool_categories
  // ============================================================================
  server.tool(
    "list_tool_categories",
    "List all available KiCAD tool categories with their descriptions and tool counts. Use this to discover which tools are available via the router.",
    {
      // No parameters
    },
    async () => {
      logger.debug("Listing tool categories");

      const stats = getRegistryStats();
      const categories = getAllCategories();

      const result = {
        total_categories: stats.total_categories,
        total_routed_tools: stats.total_routed_tools,
        total_direct_tools: stats.total_direct_tools,
        note: "Use get_category_tools to see tools in each category. Direct tools are always available.",
        categories: categories.map((c) => ({
          name: c.name,
          description: c.description,
          tool_count: c.tools.length,
        })),
      };

      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // ============================================================================
  // get_category_tools
  // ============================================================================
  server.tool(
    "get_category_tools",
    "Return all tools available in a specific category. Use list_tool_categories first to find valid category names.",
    {
      category: z.string().describe("Category name from list_tool_categories"),
    },
    async ({ category }) => {
      logger.debug(`Getting tools for category: ${category}`);

      const categoryData = getCategory(category);

      if (!categoryData) {
        const availableCategories = getAllCategories().map((c) => c.name);
        return {
          content: [
            {
              type: "text",
              text: JSON.stringify(
                {
                  error: `Unknown category: ${category}`,
                  available_categories: availableCategories,
                },
                null,
                2,
              ),
            },
          ],
        };
      }

      // Return tool names and basic info
      // Full schema is available via tool introspection once tool is called
      const result = {
        category: categoryData.name,
        description: categoryData.description,
        tool_count: categoryData.tools.length,
        tools: categoryData.tools.map((toolName) => ({
          name: toolName,
          description: `Use execute_tool with tool_name="${toolName}" to run this tool`,
        })),
        note: "Use execute_tool to run any of these tools with appropriate parameters",
      };

      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // ============================================================================
  // search_tools
  // ============================================================================
  server.tool(
    "search_tools",
    "Search all available KiCAD tools by keyword. Returns matching tool names and their categories.",
    {
      query: z.string().describe("Search term (e.g., 'gerber', 'zone', 'export', 'drc')"),
    },
    async ({ query }) => {
      logger.debug(`Searching tools for: ${query}`);

      const matches = registrySearchTools(query);

      const result = {
        query: query,
        count: matches.length,
        matches: matches,
        note:
          matches.length > 0
            ? "Use execute_tool with the tool name to run it"
            : "No tools found matching your query. Try list_tool_categories to browse all categories.",
      };

      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // ============================================================================
  // execute_tool — referenced by every router response ("Use execute_tool …")
  // but was never actually registered as an MCP tool until end-to-end MCP
  // protocol testing caught the gap.  Routed tools are also registered as
  // direct MCP tools, so they CAN be called by name; execute_tool is the
  // canonical entry point that lets a client run any tool by name without
  // first knowing whether it's direct or routed.
  // ============================================================================
  server.tool(
    "execute_tool",
    "Execute any KiCAD MCP tool by name with the given parameters. Equivalent to calling the tool directly; useful when discovering tools via list_tool_categories / search_tools.",
    {
      tool_name: z.string().describe("Tool name from list_tool_categories / search_tools"),
      params: z
        .record(z.string(), z.any())
        .optional()
        .describe("Tool parameters (default: empty object)"),
    },
    async ({ tool_name, params }) => {
      logger.debug(`execute_tool: ${tool_name}`);
      const result = await callKicadScript(tool_name, params ?? {});
      return {
        content: [{ type: "text" as const, text: JSON.stringify(result, null, 2) }],
      };
    },
  );

  // ============================================================================
  // get_workflow_tools
  // ============================================================================
  server.tool(
    "get_workflow_tools",
    "Return the ordered list of MCP tool names that make up a named workflow (e.g. create_simple_pcb, design_schematic, export_for_fab, edit_pcb). Each tool is already registered as an MCP tool — this list just tells you which subset to use and in what order.",
    {
      workflow: z
        .enum(
          Object.keys(WORKFLOWS) as [string, ...string[]],
        )
        .describe(
          "Workflow name. Use without argument to discover available workflows via list_tool_categories.",
        ),
    },
    async ({ workflow }) => {
      logger.debug(`get_workflow_tools: ${workflow}`);
      const wf = WORKFLOWS[workflow];
      if (!wf) {
        return {
          content: [
            {
              type: "text" as const,
              text: JSON.stringify(
                {
                  success: false,
                  message: `Unknown workflow: ${workflow}`,
                  available: Object.keys(WORKFLOWS),
                },
                null,
                2,
              ),
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text" as const,
            text: JSON.stringify(
              {
                success: true,
                workflow,
                description: wf.description,
                tools: wf.tools,
                count: wf.tools.length,
                hint:
                  "Each tool name is a regular MCP tool — call them directly in the listed order. " +
                  "Use list_tool_categories / search_tools to discover tools outside the workflow.",
              },
              null,
              2,
            ),
          },
        ],
      };
    },
  );

  logger.info("Router tools registered successfully");
}
