/**
 * Component resources for KiCAD MCP server
 *
 * These resources provide information about components on the PCB
 * to the LLM, enabling better context-aware assistance.
 */

import { McpServer, ResourceTemplate } from "@modelcontextprotocol/sdk/server/mcp.js";
import { logger } from "../logger.js";
import { jsonResource, resourceFailure } from "./resource-utils.js";
import { CommandFunction } from "../tools/tool-response.js";

/**
 * Register component resources with the MCP server
 *
 * @param server MCP server instance
 * @param callKicadScript Function to call KiCAD script commands
 */
export function registerComponentResources(
  server: McpServer,
  callKicadScript: CommandFunction,
): void {
  logger.info("Registering component resources");

  // ------------------------------------------------------
  // Component List Resource
  // ------------------------------------------------------
  server.resource("component_list", "kicad://components", async (uri) => {
    logger.debug("Retrieving component list");
    // limit:0 = uncapped. Resources are pulled on demand (not in the per-turn
    // model loop), so they return the full list; the get_component_list *tool*
    // stays capped at 100.
    const result = await callKicadScript("get_component_list", { limit: 0 });

    if (!result.success) {
      return resourceFailure(uri, "Failed to retrieve component list", result);
    }

    logger.debug(`Successfully retrieved ${result.components?.length || 0} components`);
    return jsonResource(uri, result);
  });

  // ------------------------------------------------------
  // Component Details Resource
  // ------------------------------------------------------
  server.resource(
    "component_details",
    new ResourceTemplate("kicad://component/{reference}/details", {
      list: undefined,
    }),
    async (uri, params) => {
      const { reference } = params;
      logger.debug(`Retrieving details for component: ${reference}`);
      const result = await callKicadScript("get_component_properties", {
        reference,
      });

      if (!result.success) {
        return resourceFailure(
          uri,
          `Failed to retrieve details for component ${reference}`,
          result,
          "Failed to retrieve component details",
        );
      }

      logger.debug(`Successfully retrieved details for component: ${reference}`);
      return jsonResource(uri, result);
    },
  );

  // NOTE: former component_connections / component_placement /
  // component_groups / component_visualization resources were removed — they
  // dispatched Python commands that never existed (UNKNOWN_COMMAND on every
  // read). Connectivity and placement data are available through the
  // get_component_list / get_component_properties tools instead.

  logger.info("Component resources registered");
}
