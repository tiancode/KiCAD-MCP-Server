/**
 * Symbol creator tools for KiCAD MCP server
 *
 * create_symbol              – add a new symbol to a .kicad_sym library
 * delete_symbol              – remove a symbol from a library
 * list_symbols_in_library    – list all symbols in a .kicad_sym file
 *
 * (Adding a library to the sym-lib-table is handled by the merged
 * register_library tool in library.ts.)
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { CommandFunction, makePassthrough } from "./tool-response.js";

const PinSchema = z.object({
  name: z.string().describe("Pin name, e.g. 'VCC'; '~' for unnamed"),
  number: z.union([z.string(), z.number()]).describe("Pin number, e.g. '1', '2', 'A1'"),
  type: z
    .enum([
      "input",
      "output",
      "bidirectional",
      "tri_state",
      "passive",
      "free",
      "unspecified",
      "power_in",
      "power_out",
      "open_collector",
      "open_emitter",
      "no_connect",
    ])
    .describe("Electrical pin type"),
  at: z
    .object({
      x: z.number().describe("X position in mm"),
      y: z.number().describe("Y position in mm"),
      angle: z
        .number()
        .describe("Direction pin extends FROM body: 0=right, 90=up, 180=left, 270=down"),
    })
    .describe("Pin endpoint position (wire connection point)"),
  length: z.number().optional().describe("Pin length in mm (default 2.54)"),
  shape: z
    .enum([
      "line",
      "inverted",
      "clock",
      "inverted_clock",
      "input_low",
      "clock_low",
      "output_low",
      "falling_edge_clock",
      "non_logic",
    ])
    .optional()
    .describe("Pin graphic shape (default: line)"),
});

const RectSchema = z.object({
  x1: z.number(),
  y1: z.number(),
  x2: z.number(),
  y2: z.number(),
  width: z.number().optional().describe("Stroke width in mm (default 0.254)"),
  fill: z
    .enum(["none", "outline", "background"])
    .optional()
    .describe("Fill type (default: background)"),
});

const PolylineSchema = z.object({
  points: z.array(z.object({ x: z.number(), y: z.number() })).describe("List of XY points in mm"),
  width: z.number().optional().describe("Stroke width in mm (default 0.254)"),
  fill: z.enum(["none", "outline", "background"]).optional(),
});

export function registerSymbolCreatorTools(server: McpServer, callKicadScript: CommandFunction) {
  const passthrough = makePassthrough(callKicadScript);
  // ── create_symbol ────────────────────────────────────────────────────── //
  server.tool(
    "create_symbol",
    "Create a schematic symbol in a .kicad_sym library (file created if missing); run register_library (type=symbol) afterwards so KiCAD finds it. " +
      "Pin positions are wire endpoints. Grid & pin length 2.54 mm; body ±2.54–5.08 mm: " +
      "left pins x=body_left−length angle=0; right x=body_right+length angle=180; " +
      "top y=body_top+length angle=270; bottom y=body_bottom−length angle=90.",
    {
      libraryPath: z.string().describe("Path to the .kicad_sym file (created if missing)"),
      name: z.string().describe("Symbol name, e.g. 'TMC2209'"),
      referencePrefix: z
        .string()
        .optional()
        .describe("Reference prefix: 'U', 'R', 'J', etc. (default 'U')"),
      description: z.string().optional().describe("Human-readable description"),
      keywords: z.string().optional().describe("Space-separated search keywords"),
      datasheet: z.string().optional().describe("Datasheet URL or '~'"),
      footprint: z
        .string()
        .optional()
        .describe("Default footprint, e.g. 'Package_SO:SOIC-8_3.9x4.9mm_P1.27mm'"),
      inBom: z.boolean().optional().describe("Include in BOM (default true)"),
      onBoard: z.boolean().optional().describe("Include in PCB netlist (default true)"),
      pins: z.array(PinSchema).optional().describe("Pins (empty for graphical-only symbols)"),
      rectangles: z
        .array(RectSchema)
        .optional()
        .describe("Body rectangle(s), typically one for the IC body"),
      polylines: z
        .array(PolylineSchema)
        .optional()
        .describe("Polylines for custom body shapes (op-amp triangles, etc.)"),
      overwrite: z
        .boolean()
        .optional()
        .describe("Replace existing symbol of same name (default false)"),
    },
    passthrough("create_symbol"),
  );

  // ── delete_symbol ────────────────────────────────────────────────────── //
  server.tool(
    "delete_symbol",
    "Remove a symbol from a .kicad_sym library file.",
    {
      libraryPath: z.string().describe("Path to the .kicad_sym file"),
      name: z.string().describe("Symbol name to delete"),
    },
    passthrough("delete_symbol"),
  );

  // ── list_symbols_in_library ──────────────────────────────────────────── //
  server.tool(
    "list_symbols_in_library",
    "List symbol names in a .kicad_sym file given its path. Works for unregistered files (e.g. right after create_symbol); for a registered library nickname use list_library_contents (type=symbol).",
    {
      libraryPath: z.string().describe("Path to the .kicad_sym file"),
    },
    passthrough("list_symbols_in_library"),
  );
}
