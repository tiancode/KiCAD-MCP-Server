/**
 * Schematic Listing and overview queries tools for KiCAD MCP server.
 * Split out of the former monolithic schematic.ts.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { paginationParams, truncationNote } from "../pagination-params.js";
import { CommandFunction, makePassthrough } from "../tool-response.js";

export function registerSchematicQueryTools(server: McpServer, callKicadScript: CommandFunction) {
  const passthrough = makePassthrough(callKicadScript);
  // One-shot schematic snapshot — components + wires + labels + nets in a
  // single response. Cuts 3 MCP round-trips out of basic schematic inspection.
  server.tool(
    "get_schematic_overview",
    "One-shot snapshot of a schematic: components, wires, labels, and nets in a single response. Use this instead of calling list_schematic_components + list_schematic_wires + list_schematic_labels + list_schematic_nets separately.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
    },
    passthrough("get_schematic_overview"),
  );

  // List all components in schematic
  server.tool(
    "list_schematic_components",
    "List all components in a schematic with their references, values, positions, and pins. Essential for inspecting what's on the schematic before making edits.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      filter: z
        .object({
          libId: z.string().optional().describe("Filter by library ID (e.g., 'Device:R')"),
          referencePrefix: z
            .string()
            .optional()
            .describe("Filter by reference prefix (e.g., 'R', 'C', 'U')"),
        })
        .optional()
        .describe("Optional filters"),
      ...paginationParams,
    },
    async (args: {
      schematicPath: string;
      filter?: { libId?: string; referencePrefix?: string };
      limit?: number;
      offset?: number;
    }) => {
      const result = await callKicadScript("list_schematic_components", args);
      if (result.success) {
        const comps = result.components || [];
        if (comps.length === 0) {
          return {
            content: [{ type: "text", text: "No components found in schematic." }],
          };
        }
        const lines = comps.map(
          (c: any) =>
            `  ${c.reference}: ${c.libId} = "${c.value}" at (${c.position.x}, ${c.position.y}) rot=${c.rotation}°${c.pins ? ` [${c.pins.length} pins]` : ""}`,
        );
        return {
          content: [
            {
              type: "text",
              text: `Components (${comps.length}):\n${lines.join("\n")}${truncationNote(result)}`,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text",
            text: `Failed to list components: ${result.message || "Unknown error"}`,
          },
        ],
        isError: true,
      };
    },
  );

  // List all nets in schematic
  server.tool(
    "list_schematic_nets",
    "List all nets in the schematic with their connections.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      ...paginationParams,
    },
    async (args: { schematicPath: string; limit?: number; offset?: number }) => {
      const result = await callKicadScript("list_schematic_nets", args);
      if (result.success) {
        const nets = result.nets || [];
        if (nets.length === 0) {
          return {
            content: [{ type: "text", text: "No nets found in schematic." }],
          };
        }
        const lines = nets.map((n: any) => {
          const conns = (n.connections || []).map((c: any) => `${c.component}/${c.pin}`).join(", ");
          const pinCount =
            n.connected_pin_count !== undefined ? ` [${n.connected_pin_count} pin(s)]` : "";
          return `  ${n.name}${pinCount}: ${conns || "(no connections)"}`;
        });
        return {
          content: [
            {
              type: "text",
              text: `Nets (${nets.length}):\n${lines.join("\n")}${truncationNote(result)}`,
            },
          ],
        };
      }
      return {
        content: [
          { type: "text", text: `Failed to list nets: ${result.message || "Unknown error"}` },
        ],
        isError: true,
      };
    },
  );

  // List all wires in schematic
  server.tool(
    "list_schematic_wires",
    "List all wires in the schematic with start/end coordinates.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      ...paginationParams,
    },
    async (args: { schematicPath: string; limit?: number; offset?: number }) => {
      const result = await callKicadScript("list_schematic_wires", args);
      if (result.success) {
        const wires = result.wires || [];
        if (wires.length === 0) {
          return {
            content: [{ type: "text", text: "No wires found in schematic." }],
          };
        }
        const lines = wires.map(
          (w: any) => `  (${w.start.x}, ${w.start.y}) → (${w.end.x}, ${w.end.y})`,
        );
        return {
          content: [
            {
              type: "text",
              text: `Wires (${wires.length}):\n${lines.join("\n")}${truncationNote(result)}`,
            },
          ],
        };
      }
      return {
        content: [
          { type: "text", text: `Failed to list wires: ${result.message || "Unknown error"}` },
        ],
        isError: true,
      };
    },
  );

  // List all labels in schematic
  server.tool(
    "list_schematic_labels",
    "List all net labels, global labels, and power flags in the schematic. " +
      "Optionally filter by label name (netName) and/or label type (labelType).",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      netName: z
        .string()
        .optional()
        .describe(
          "Filter to labels whose name exactly matches this string (case-sensitive). Omit to return all labels.",
        ),
      labelType: z
        .enum(["net", "global", "power"])
        .optional()
        .describe(
          "Filter by label type. 'net' = local label, 'global' = global label, 'power' = power symbol. Omit to return all types.",
        ),
      ...paginationParams,
    },
    async (args: {
      schematicPath: string;
      netName?: string;
      labelType?: string;
      limit?: number;
      offset?: number;
    }) => {
      const result = await callKicadScript("list_schematic_labels", args);
      if (result.success) {
        const labels = result.labels || [];
        if (labels.length === 0) {
          return {
            content: [{ type: "text", text: "No labels found in schematic." }],
          };
        }
        const lines = labels.map(
          (l: any) => `  [${l.type}] ${l.name} at (${l.position.x}, ${l.position.y})`,
        );
        return {
          content: [
            {
              type: "text",
              text: `Labels (${labels.length}):\n${lines.join("\n")}${truncationNote(result)}`,
            },
          ],
        };
      }
      return {
        content: [
          { type: "text", text: `Failed to list labels: ${result.message || "Unknown error"}` },
        ],
        isError: true,
      };
    },
  );

  // List free-form text annotations in schematic
  server.tool(
    "list_schematic_texts",
    "List all free-form text annotations (notes, headings, documentation strings) in the schematic. " +
      "Returns position, angle, font size, bold/italic flags, and justification for each text element. " +
      "Optionally filter by a substring match on the text content.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      text: z
        .string()
        .optional()
        .describe("Case-insensitive substring filter — only return texts containing this string"),
      ...paginationParams,
    },
    async (args: { schematicPath: string; text?: string; limit?: number; offset?: number }) => {
      const result = await callKicadScript("list_schematic_texts", args);
      if (result.success) {
        const texts = result.texts || [];
        if (texts.length === 0) {
          return {
            content: [{ type: "text" as const, text: "No text annotations found in schematic." }],
          };
        }
        const lines = texts.map(
          (t: any) =>
            `  "${t.text}" at (${t.position.x}, ${t.position.y})` +
            (t.angle ? ` angle=${t.angle}` : "") +
            ` size=${t.font_size}` +
            (t.bold ? " bold" : "") +
            (t.italic ? " italic" : "") +
            ` justify=${t.justify}`,
        );
        return {
          content: [
            {
              type: "text" as const,
              text: `Text annotations (${texts.length}):\n${lines.join("\n")}${truncationNote(result)}`,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text" as const,
            text: `Failed to list text annotations: ${result.message || "Unknown error"}`,
          },
        ],
        isError: true,
      };
    },
  );
}
