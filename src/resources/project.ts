/**
 * Project resources for KiCAD MCP server
 *
 * These resources provide information about the KiCAD project
 * to the LLM, enabling better context-aware assistance.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { logger } from "../logger.js";
import { countComponentTypes, jsonResource, resourceError } from "./resource-utils.js";
import { CommandFunction } from "../tools/tool-response.js";

/**
 * Register project resources with the MCP server
 *
 * @param server MCP server instance
 * @param callKicadScript Function to call KiCAD script commands
 */
export function registerProjectResources(
  server: McpServer,
  callKicadScript: CommandFunction,
): void {
  logger.info("Registering project resources");

  // ------------------------------------------------------
  // Project Information Resource
  // ------------------------------------------------------
  server.resource("project_info", "kicad://project/info", async (uri) => {
    logger.debug("Retrieving project information");
    const result = await callKicadScript("get_project_info", {});

    if (!result.success) {
      logger.error(`Failed to retrieve project information: ${result.errorDetails}`);
      return resourceError(uri, "Failed to retrieve project information", result.errorDetails);
    }

    logger.debug("Successfully retrieved project information");
    return jsonResource(uri, result);
  });

  // NOTE: former project_properties / project_files / project_status
  // resources were removed — they dispatched Python commands that never
  // existed (UNKNOWN_COMMAND on every read). Use kicad://project/info or
  // kicad://project/summary instead.

  // ------------------------------------------------------
  // Project Summary Resource
  // ------------------------------------------------------
  server.resource("project_summary", "kicad://project/summary", async (uri) => {
    logger.debug("Generating project summary");

    // Get project info
    const infoResult = await callKicadScript("get_project_info", {});
    if (!infoResult.success) {
      logger.error(`Failed to retrieve project information: ${infoResult.errorDetails}`);
      return resourceError(uri, "Failed to generate project summary", infoResult.errorDetails);
    }

    // Get board info
    const boardResult = await callKicadScript("get_board_info", {});
    if (!boardResult.success) {
      logger.error(`Failed to retrieve board information: ${boardResult.errorDetails}`);
      return resourceError(uri, "Failed to generate project summary", boardResult.errorDetails);
    }

    // Get component list (limit:0 = uncapped; resources carry full data)
    const componentsResult = await callKicadScript("get_component_list", { limit: 0 });
    if (!componentsResult.success) {
      logger.error(`Failed to retrieve component list: ${componentsResult.errorDetails}`);
      return resourceError(
        uri,
        "Failed to generate project summary",
        componentsResult.errorDetails,
      );
    }

    // Combine all information into a summary
    const summary = {
      project: infoResult.project,
      board: {
        size: boardResult.size,
        layers: boardResult.layers?.length || 0,
        title: boardResult.title,
      },
      components: {
        count: componentsResult.components?.length || 0,
        types: countComponentTypes(componentsResult.components || []),
      },
    };

    logger.debug("Successfully generated project summary");
    return jsonResource(uri, summary);
  });

  logger.info("Project resources registered");
}
