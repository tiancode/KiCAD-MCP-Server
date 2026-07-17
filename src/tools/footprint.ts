/**
 * Footprint tools for KiCAD MCP server
 *
 * create_footprint      – generate a complete .kicad_mod file in a .pretty library
 * edit_footprint_pad    – update size / position / drill / shape of one pad
 * list_footprint_libraries – list available .pretty libraries
 *
 * (Registering a library in the fp-lib-table is handled by the merged
 * register_library tool in library.ts.)
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { CommandFunction, makePassthrough } from "./tool-response.js";

// ---- shared sub-schemas ------------------------------------------------- //

const PadPosition = z.object({
  x: z.number().describe("X position in mm"),
  y: z.number().describe("Y position in mm"),
  angle: z.number().optional().describe("Rotation angle in degrees (default 0)"),
});

const PadSize = z.object({
  w: z.number().describe("Width in mm"),
  h: z.number().describe("Height in mm"),
});

const PadSchema = z.object({
  number: z.string().describe("Pad number / name, e.g. '1', '2', 'A1'"),
  type: z.enum(["smd", "thru_hole", "np_thru_hole"]).describe("Pad type"),
  shape: z
    .enum(["rect", "circle", "oval", "roundrect"])
    .optional()
    .describe("Pad shape (default: rect for SMD, circle for THT)"),
  at: PadPosition.describe("Pad centre position"),
  size: PadSize.describe("Pad size in mm"),
  drill: z
    .union([
      z.number().describe("Round drill diameter in mm"),
      z.object({ w: z.number(), h: z.number() }).describe("Oval drill w×h in mm"),
    ])
    .optional()
    .describe("Drill size (required for thru_hole pads)"),
  layers: z
    .array(z.string())
    .optional()
    .describe("Override default layers, e.g. ['F.Cu','F.Paste','F.Mask']"),
  roundrect_ratio: z
    .number()
    .min(0)
    .max(0.5)
    .optional()
    .describe("Roundrect corner radius ratio (default 0.25)"),
});

const RectSchema = z.object({
  x1: z.number().describe("Left X in mm"),
  y1: z.number().describe("Top Y in mm"),
  x2: z.number().describe("Right X in mm"),
  y2: z.number().describe("Bottom Y in mm"),
  width: z.number().optional().describe("Line width in mm"),
});

// ---- tool registration --------------------------------------------------- //

export function registerFootprintTools(server: McpServer, callKicadScript: CommandFunction) {
  const passthrough = makePassthrough(callKicadScript);
  server.tool(
    "create_footprint",
    "Create a new KiCAD footprint (.kicad_mod) inside a .pretty library directory. " +
      "Supports SMD and THT pads, courtyard, silkscreen, and fab-layer rectangles.",
    {
      libraryPath: z
        .string()
        .describe("Path to the .pretty library directory (created if missing)"),
      name: z.string().describe("Footprint name, e.g. 'R_0603_Custom'"),
      description: z.string().optional().describe("Human-readable description"),
      tags: z.string().optional().describe("Space-separated tags"),
      pads: z
        .array(PadSchema)
        .optional()
        .describe("Pads to add (empty for outlines-only footprints)"),
      courtyard: RectSchema.optional().describe(
        "Courtyard rect on F.CrtYd (recommend 0.25 mm clearance around pads)",
      ),
      silkscreen: RectSchema.optional().describe("Silkscreen rectangle on F.SilkS"),
      fabLayer: RectSchema.optional().describe(
        "Fab-layer rectangle on F.Fab (shows component body)",
      ),
      refPosition: z
        .object({ x: z.number(), y: z.number() })
        .optional()
        .describe("REF** text position (default 0, -1.27)"),
      valuePosition: z
        .object({ x: z.number(), y: z.number() })
        .optional()
        .describe("Value text position (default 0, 1.27)"),
      overwrite: z.boolean().optional().describe("Replace existing footprint file (default false)"),
    },
    passthrough("create_footprint"),
  );

  server.tool(
    "edit_footprint_pad",
    "Edit one pad in a .kicad_mod footprint file (size/position/drill/shape) without recreating the footprint.",
    {
      footprintPath: z.string().describe("Full path to the .kicad_mod file"),
      padNumber: z.union([z.string(), z.number()]).describe("Pad number to edit, e.g. '1' or 2"),
      size: PadSize.optional().describe("New pad size in mm"),
      at: PadPosition.optional().describe("New pad position in mm"),
      drill: z
        .union([
          z.number().describe("Round drill diameter in mm"),
          z.object({ w: z.number(), h: z.number() }).describe("Oval drill"),
        ])
        .optional()
        .describe("New drill size (for THT pads)"),
      shape: z.enum(["rect", "circle", "oval", "roundrect"]).optional().describe("New pad shape"),
    },
    passthrough("edit_footprint_pad"),
  );

  server.tool(
    "list_footprint_libraries",
    "Scan the filesystem for .pretty footprint libraries (previews first 20 footprints each). Use when libraries may be missing from the fp-lib-table; for registered names use list_libraries (type=footprint), for one library's full contents use list_library_contents (type=footprint).",
    {
      searchPaths: z
        .array(z.string())
        .optional()
        .describe("Override default search paths (dirs containing .pretty subdirs)"),
    },
    passthrough("list_footprint_libraries"),
  );
}
