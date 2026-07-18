/**
 * Export tools for KiCAD MCP server
 *
 * These tools handle exporting PCB data to various formats
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { logger } from "../logger.js";
import { CommandFunction, formatKicadResult } from "./tool-response.js";

/**
 * A case-insensitive enum: accepts any casing on input and normalizes to the
 * canonical (given) values before validation, so "csv" and "CSV" both pass.
 * The advertised JSON schema still shows the canonical values.
 */
function upperEnum<T extends [string, ...string[]]>(values: T) {
  return z.preprocess((v) => (typeof v === "string" ? v.toUpperCase() : v), z.enum(values));
}

/**
 * Register export tools with the MCP server
 *
 * @param server MCP server instance
 * @param callKicadScript Function to call KiCAD script commands
 */
export function registerExportTools(server: McpServer, callKicadScript: CommandFunction): void {
  logger.info("Registering export tools");

  server.tool(
    "export_gerber",
    "Export PCB Gerber manufacturing files to a directory.",
    {
      outputDir: z.string().describe("Directory to save Gerber files"),
      layers: z.array(z.string()).optional().describe("Layer names to export (default: all)"),
      useProtelExtensions: z.boolean().optional().describe("Use Protel filename extensions"),
      generateDrillFiles: z.boolean().optional().describe("Generate drill files"),
      generateMapFile: z
        .boolean()
        .optional()
        .describe("Also write a drill map (+ .gbrjob) next to the drill files; see files.map"),
      mapFormat: z
        .enum(["gerberx2", "pdf", "postscript", "dxf", "svg"])
        .optional()
        .describe("Drill-map format when generateMapFile is set (default gerberx2)"),
      useAuxOrigin: z.boolean().optional().describe("Use auxiliary axis as origin"),
    },
    async ({
      outputDir,
      layers,
      useProtelExtensions,
      generateDrillFiles,
      generateMapFile,
      mapFormat,
      useAuxOrigin,
    }) => {
      logger.debug(`Exporting Gerber files to: ${outputDir}`);
      const result = await callKicadScript("export_gerber", {
        outputDir,
        layers,
        useProtelExtensions,
        generateDrillFiles,
        generateMapFile,
        mapFormat,
        useAuxOrigin,
      });

      return formatKicadResult(result);
    },
  );

  server.tool(
    "export_pdf",
    "Export the PCB layout as a PDF document.",
    {
      outputPath: z.string().describe("Path to save the PDF file"),
      layers: z.array(z.string()).optional().describe("Layer names to include (default: all)"),
      blackAndWhite: z.boolean().optional().describe("Export in black and white"),
      frameReference: z.boolean().optional().describe("Include frame reference"),
      pageSize: z
        .enum(["A4", "A3", "A2", "A1", "A0", "Letter", "Legal", "Tabloid"])
        .optional()
        .describe("Page size"),
    },
    async ({ outputPath, layers, blackAndWhite, frameReference, pageSize }) => {
      logger.debug(`Exporting PDF to: ${outputPath}`);
      const result = await callKicadScript("export_pdf", {
        outputPath,
        layers,
        blackAndWhite,
        frameReference,
        pageSize,
      });

      return formatKicadResult(result);
    },
  );

  server.tool(
    "export_3d",
    "Export the PCB as a 3D model.",
    {
      outputPath: z.string().describe("Path to save the 3D model file"),
      format: z.enum(["STEP", "VRML"]).describe("3D model format (STEP or VRML)"),
      includeComponents: z.boolean().optional().describe("Include 3D component models"),
      includeCopper: z.boolean().optional().describe("Include copper layers"),
      includeSolderMask: z.boolean().optional().describe("Include solder mask"),
      includeSilkscreen: z.boolean().optional().describe("Include silkscreen"),
    },
    async ({
      outputPath,
      format,
      includeComponents,
      includeCopper,
      includeSolderMask,
      includeSilkscreen,
    }) => {
      logger.debug(`Exporting 3D model to: ${outputPath}`);
      const result = await callKicadScript("export_3d", {
        outputPath,
        format,
        includeComponents,
        includeCopper,
        includeSolderMask,
        includeSilkscreen,
      });

      return formatKicadResult(result);
    },
  );

  server.tool(
    "export_bom",
    "Export a Bill of Materials (BOM) from the PCB. Mounting holes and board hardware " +
      "(ref prefix MH or MountingHole footprint) are excluded by default — set " +
      "includeMountingHoles to keep them; the response reports excludedMountingHoles. " +
      "Sourcing columns (MPN, Manufacturer, LCSC, Datasheet, …) come from footprint " +
      "fields: run sync_schematic_to_board first so schematic symbol fields are copied " +
      "onto the board. A requested attribute matches a field by exact name, " +
      'case-insensitively, or by leading token ("LCSC" resolves the "LCSC Part" ' +
      "field); any attribute missing on every footprint is reported in attributesMissing " +
      "plus a warning (never silently dropped).",
    {
      outputPath: z.string().describe("Path to save the BOM file"),
      format: upperEnum(["CSV", "XML", "HTML", "JSON"]).describe(
        "BOM file format (case-insensitive)",
      ),
      groupByValue: z
        .boolean()
        .optional()
        .describe(
          "Group components by value+footprint. When members of a group disagree on " +
            "an attribute, the distinct values are joined with '; ' (groups are not split).",
        ),
      includeMountingHoles: z
        .boolean()
        .optional()
        .describe("Include mounting holes / board hardware (default false)"),
      includeAttributes: z
        .array(z.string())
        .optional()
        .describe(
          'Sourcing/custom footprint fields to add as columns, e.g. ["LCSC","MPN",' +
            "\"Manufacturer\"]. Alias of 'attributes'.",
        ),
      attributes: z
        .array(z.string())
        .optional()
        .describe("Alias of includeAttributes (accepted for convenience)."),
    },
    async ({
      outputPath,
      format,
      groupByValue,
      includeMountingHoles,
      includeAttributes,
      attributes,
    }) => {
      logger.debug(`Exporting BOM to: ${outputPath}`);
      const result = await callKicadScript("export_bom", {
        outputPath,
        format,
        groupByValue,
        includeMountingHoles,
        includeAttributes,
        attributes,
      });

      return formatKicadResult(result);
    },
  );

  server.tool(
    "export_netlist",
    "Export the schematic netlist to a file via kicad-cli. Use when you need a netlist file on disk (e.g. SPICE for simulation); for inline net/component data use generate_netlist.",
    {
      schematicPath: z.string().describe("Absolute path to the .kicad_sch schematic file"),
      outputPath: z.string().describe("Absolute path for the output file"),
      format: z
        .enum(["KiCad", "Spice", "Cadstar", "OrcadPCB2"])
        .optional()
        .describe("Netlist format (default: KiCad)"),
    },
    async ({ schematicPath, outputPath, format }) => {
      logger.debug(`Exporting netlist to: ${outputPath}`);
      const result = await callKicadScript("export_netlist", {
        schematicPath,
        outputPath,
        format,
      });

      return formatKicadResult(result);
    },
  );

  server.tool(
    "export_position_file",
    "Export a component placement (pick-and-place) file for PCB assembly.",
    {
      outputPath: z.string().describe("Path to save the position file"),
      format: upperEnum(["CSV", "ASCII"])
        .optional()
        .describe("File format (case-insensitive, default: CSV)"),
      units: z.enum(["mm", "mil", "inch"]).optional().describe("Units to use (default: mm)"),
      side: z
        .enum(["top", "bottom", "both"])
        .optional()
        .describe("Which board side to include (default: both)"),
    },
    async ({ outputPath, format, units, side }) => {
      logger.debug(`Exporting position file to: ${outputPath}`);
      const result = await callKicadScript("export_position_file", {
        outputPath,
        format,
        units,
        side,
      });

      return formatKicadResult(result);
    },
  );

  logger.info("Export tools registered");
}
