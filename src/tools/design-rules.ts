/**
 * Design rules tools for KiCAD MCP server
 *
 * These tools handle design rule checking and configuration
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { logger } from "../logger.js";
import { CommandFunction, formatKicadResult } from "./tool-response.js";

/**
 * Register design rule tools with the MCP server
 *
 * @param server MCP server instance
 * @param callKicadScript Function to call KiCAD script commands
 */
export function registerDesignRuleTools(server: McpServer, callKicadScript: CommandFunction): void {
  logger.info("Registering design rule tools");

  // ------------------------------------------------------
  // Design Rules Tool (read + update)
  // ------------------------------------------------------
  server.tool(
    "design_rules",
    "Read or update PCB design rules: no parameters reads current rules; any parameter updates it. Covers clearance, track width, via/micro-via sizes, minimums, hole diameter, courtyard.",
    {
      clearance: z.number().optional().describe("Minimum clearance between copper items (mm)"),
      trackWidth: z.number().optional().describe("Default track width (mm)"),
      viaDiameter: z.number().optional().describe("Default via diameter (mm)"),
      viaDrill: z.number().optional().describe("Default via drill size (mm)"),
      microViaDiameter: z.number().optional().describe("Default micro via diameter (mm)"),
      microViaDrill: z.number().optional().describe("Default micro via drill size (mm)"),
      minTrackWidth: z.number().optional().describe("Minimum track width (mm)"),
      minViaDiameter: z.number().optional().describe("Minimum via diameter (mm)"),
      minViaDrill: z.number().optional().describe("Minimum via drill size (mm)"),
      minMicroViaDiameter: z.number().optional().describe("Minimum micro via diameter (mm)"),
      minMicroViaDrill: z.number().optional().describe("Minimum micro via drill size (mm)"),
      minHoleDiameter: z.number().optional().describe("Minimum hole diameter (mm)"),
      requireCourtyard: z.boolean().optional().describe("Require courtyards for all footprints"),
      courtyardClearance: z
        .number()
        .optional()
        .describe("Minimum clearance between courtyards (mm)"),
    },
    async (params) => {
      const hasWriteParams = Object.values(params).some((value) => value !== undefined);
      if (hasWriteParams) {
        logger.debug("Setting design rules");
        return formatKicadResult(await callKicadScript("set_design_rules", params));
      }
      logger.debug("Getting design rules");
      return formatKicadResult(await callKicadScript("get_design_rules", {}));
    },
  );

  // ------------------------------------------------------
  // Run DRC Tool
  // ------------------------------------------------------
  server.tool(
    "run_drc",
    "Run KiCAD Design Rule Check (DRC) on the current PCB and return violations with per-item offender locations (description + x/y mm). Returns the first maxViolations inline; the full list is written to the violations file.",
    {
      reportPath: z.string().optional().describe("Optional path to save the DRC report"),
      maxViolations: z
        .number()
        .int()
        .optional()
        .describe("Max violations returned inline (default 30, 0 = all)."),
    },
    async ({ reportPath, maxViolations }) => {
      logger.debug("Running DRC check");
      const result = await callKicadScript("run_drc", { reportPath, maxViolations });

      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Assign Net to Class Tool
  // ------------------------------------------------------
  server.tool(
    "assign_net_to_class",
    "Assign a net to an existing net class to apply its specific design rules.",
    {
      net: z.string().describe("Name of the net"),
      netClass: z.string().describe("Name of the net class"),
    },
    async ({ net, netClass }) => {
      logger.debug(`Assigning net ${net} to class ${netClass}`);
      const result = await callKicadScript("assign_net_to_class", {
        net,
        netClass,
      });

      return formatKicadResult(result);
    },
  );

  logger.info("Design rule tools registered");
}
