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
  type: z
    .enum(["smd", "thru_hole", "np_thru_hole"])
    .describe("Pad type: smd | thru_hole | np_thru_hole"),
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
    .describe("Override default layer list, e.g. ['F.Cu','F.Paste','F.Mask']"),
  roundrect_ratio: z
    .number()
    .min(0)
    .max(0.5)
    .optional()
    .describe("Corner radius ratio for roundrect shape (0.0–0.5, default 0.25)"),
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
  // ── create_footprint ──────────────────────────────────────────────────── //
  server.tool(
    "create_footprint",
    "Create a new KiCAD footprint (.kicad_mod) inside a .pretty library directory. " +
      "Supports SMD and THT pads, courtyard, silkscreen, and fab-layer rectangles.",
    {
      libraryPath: z
        .string()
        .describe(
          "Path to the .pretty library directory (created if missing). " +
            "E.g. C:/MyProject/MyLib.pretty",
        ),
      name: z.string().describe("Footprint name, e.g. 'R_0603_Custom'"),
      description: z.string().optional().describe("Human-readable description"),
      tags: z.string().optional().describe("Space-separated tag string, e.g. 'resistor SMD 0603'"),
      pads: z
        .array(PadSchema)
        .optional()
        .describe("List of pads to add (can be empty for outlines-only footprints)"),
      courtyard: RectSchema.optional().describe(
        "Courtyard rectangle on F.CrtYd (recommended: 0.25 mm clearance around pads)",
      ),
      silkscreen: RectSchema.optional().describe("Silkscreen rectangle on F.SilkS"),
      fabLayer: RectSchema.optional().describe(
        "Fab-layer rectangle on F.Fab (shows component body)",
      ),
      refPosition: z
        .object({ x: z.number(), y: z.number() })
        .optional()
        .describe("Position of the REF** text (default: 0, -1.27)"),
      valuePosition: z
        .object({ x: z.number(), y: z.number() })
        .optional()
        .describe("Position of the Value text (default: 0, 1.27)"),
      overwrite: z
        .boolean()
        .optional()
        .describe("Replace existing footprint file (default: false)"),
    },
    passthrough("create_footprint"),
  );

  // ── edit_footprint_pad ────────────────────────────────────────────────── //
  server.tool(
    "edit_footprint_pad",
    "Edit an existing pad inside a .kicad_mod footprint file. " +
      "Updates size, position, drill, or shape without recreating the whole footprint.",
    {
      footprintPath: z
        .string()
        .describe("Full path to the .kicad_mod file, e.g. C:/MyLib.pretty/R_Custom.kicad_mod"),
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

  // ── list_footprint_libraries ─────────────────────────────────────────── //
  server.tool(
    "list_footprint_libraries",
    "Discover FOOTPRINT libraries by SCANNING THE FILESYSTEM for .pretty directories, with a preview of the first 20 footprints in each. Use when libraries may not be registered in the fp-lib-table; for registered library names only use list_libraries (type=footprint), and for the full contents of ONE library use list_library_contents (type=footprint).",
    {
      searchPaths: z
        .array(z.string())
        .optional()
        .describe(
          "Override default search paths. Each entry should be a directory that contains .pretty subdirs.",
        ),
    },
    passthrough("list_footprint_libraries"),
  );
}
