/**
 * Board resources for KiCAD MCP server
 *
 * These resources provide information about the PCB board
 * to the LLM, enabling better context-aware assistance.
 */

import { McpServer, ResourceTemplate } from "@modelcontextprotocol/sdk/server/mcp.js";
import { logger } from "../logger.js";
import { boardSummary, componentSummary, jsonResource, resourceFailure } from "./resource-utils.js";
import { CommandFunction } from "../tools/tool-response.js";

/**
 * Register board resources with the MCP server
 *
 * @param server MCP server instance
 * @param callKicadScript Function to call KiCAD script commands
 */
export function registerBoardResources(server: McpServer, callKicadScript: CommandFunction): void {
  logger.info("Registering board resources");

  server.resource("board_info", "kicad://board/info", async (uri) => {
    logger.debug("Retrieving board information");
    const result = await callKicadScript("get_board_info", {});

    if (!result.success) {
      return resourceFailure(uri, "Failed to retrieve board information", result);
    }

    logger.debug("Successfully retrieved board information");
    return jsonResource(uri, result);
  });

  server.resource("layer_list", "kicad://board/layers", async (uri) => {
    logger.debug("Retrieving layer list");
    const result = await callKicadScript("get_layer_list", {});

    if (!result.success) {
      return resourceFailure(uri, "Failed to retrieve layer list", result);
    }

    logger.debug(`Successfully retrieved ${result.layers?.length || 0} layers`);
    return jsonResource(uri, result);
  });

  server.resource(
    "board_extents",
    new ResourceTemplate("kicad://board/extents/{unit?}", {
      list: async () => ({
        resources: [
          { uri: "kicad://board/extents/mm", name: "Millimeters" },
          { uri: "kicad://board/extents/inch", name: "Inches" },
        ],
      }),
    }),
    async (uri, params) => {
      const unit = params.unit || "mm";

      logger.debug(`Retrieving board extents in ${unit}`);
      const result = await callKicadScript("get_board_extents", { unit });

      if (!result.success) {
        return resourceFailure(uri, "Failed to retrieve board extents", result);
      }

      logger.debug("Successfully retrieved board extents");
      return jsonResource(uri, result);
    },
  );

  server.resource(
    "board_2d_view",
    new ResourceTemplate("kicad://board/2d-view/{format?}", {
      list: async () => ({
        resources: [
          { uri: "kicad://board/2d-view/png", name: "PNG Format" },
          { uri: "kicad://board/2d-view/jpg", name: "JPEG Format" },
          { uri: "kicad://board/2d-view/svg", name: "SVG Format" },
        ],
      }),
    }),
    async (uri, params) => {
      const format = (params.format || "png") as "png" | "jpg" | "svg";
      const width = params.width ? parseInt(params.width as string) : undefined;
      const height = params.height ? parseInt(params.height as string) : undefined;
      // Handle layers parameter - could be string or array
      const layers = typeof params.layers === "string" ? params.layers.split(",") : params.layers;

      logger.debug("Retrieving 2D board view");
      const result = await callKicadScript("get_board_2d_view", {
        layers,
        width,
        height,
        format,
      });

      if (!result.success) {
        return resourceFailure(uri, "Failed to retrieve 2D board view", result);
      }

      logger.debug("Successfully retrieved 2D board view");

      if (format === "svg") {
        return {
          contents: [
            {
              uri: uri.href,
              text: result.imageData,
              mimeType: "image/svg+xml",
            },
          ],
        };
      } else {
        return {
          contents: [
            {
              uri: uri.href,
              blob: result.imageData,
              mimeType: format === "jpg" ? "image/jpeg" : "image/png",
            },
          ],
        };
      }
    },
  );

  // NOTE: the former board_3d_view resource was removed — it dispatched a
  // Python command that never existed (UNKNOWN_COMMAND on every read). Use
  // the export_3d tool for 3D output, or kicad://board/2d-view for imagery.

  server.resource("board_statistics", "kicad://board/statistics", async (uri) => {
    logger.debug("Generating board statistics");

    const boardResult = await callKicadScript("get_board_info", {});
    if (!boardResult.success) {
      return resourceFailure(
        uri,
        "Failed to generate board statistics",
        boardResult,
        "Failed to retrieve board information",
      );
    }

    // Get component list (limit:0 = uncapped; resources carry full data)
    const componentsResult = await callKicadScript("get_component_list", { limit: 0 });
    if (!componentsResult.success) {
      return resourceFailure(
        uri,
        "Failed to generate board statistics",
        componentsResult,
        "Failed to retrieve component list",
      );
    }

    // Get nets list (limit:0 = uncapped; resources carry full data)
    const netsResult = await callKicadScript("get_nets_list", { limit: 0 });
    if (!netsResult.success) {
      return resourceFailure(
        uri,
        "Failed to generate board statistics",
        netsResult,
        "Failed to retrieve nets list",
      );
    }

    const statistics = {
      board: boardSummary(boardResult),
      components: componentSummary(componentsResult),
      nets: {
        count: netsResult.nets?.length || 0,
      },
    };

    logger.debug("Successfully generated board statistics");
    return jsonResource(uri, statistics);
  });

  logger.info("Board resources registered");
}
