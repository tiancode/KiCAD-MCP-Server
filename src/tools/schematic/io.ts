/**
 * Schematic Export, ERC, netlist, and board sync tools for KiCAD MCP server.
 * Split out of the former monolithic schematic.ts.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { formatKicadResult } from "../tool-response.js";

export function registerSchematicIoTools(server: McpServer, callKicadScript: Function) {
  // Export schematic to SVG
  server.tool(
    "export_schematic_svg",
    "Export schematic to SVG format using kicad-cli.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      outputPath: z.string().describe("Output SVG file path"),
      blackAndWhite: z.boolean().optional().describe("Export in black and white"),
    },
    async (args: { schematicPath: string; outputPath: string; blackAndWhite?: boolean }) => {
      const result = await callKicadScript("export_schematic_svg", args);
      if (result.success) {
        return {
          content: [
            {
              type: "text",
              text: `Exported schematic SVG to ${result.file?.path || args.outputPath}`,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text",
            text: `Failed to export SVG: ${result.message || "Unknown error"}`,
          },
        ],
        isError: true,
      };
    },
  );

  // Export schematic to PDF
  server.tool(
    "export_schematic_pdf",
    "Export schematic to PDF format using kicad-cli.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      outputPath: z.string().describe("Output PDF file path"),
      blackAndWhite: z.boolean().optional().describe("Export in black and white"),
    },
    async (args: { schematicPath: string; outputPath: string; blackAndWhite?: boolean }) => {
      const result = await callKicadScript("export_schematic_pdf", args);
      if (result.success) {
        return {
          content: [
            {
              type: "text",
              text: `Exported schematic PDF to ${result.file?.path || args.outputPath}`,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text",
            text: `Failed to export PDF: ${result.message || "Unknown error"}`,
          },
        ],
        isError: true,
      };
    },
  );

  // Run Electrical Rules Check (ERC)
  server.tool(
    "run_erc",
    "Run ERC on a schematic and return all violations. Gotcha: KiCad requires every power-input pin to be driven by a " +
      "power-output pin or PWR_FLAG — labels alone aren't enough; summary.recommendations[] lists the 'add PWR_FLAG' fixes " +
      "and summary.real_errors counts only non-PWR_FLAG issues. lib_symbols are auto-refreshed from disk first " +
      "(silences 'symbol doesn't match library'; opt out with autoRefreshLibSymbols=false).",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch schematic file"),
      autoRefreshLibSymbols: z
        .boolean()
        .optional()
        .describe(
          "Re-inject lib_symbols from the on-disk .kicad_sym before ERC (default true) to silence lib_symbol_mismatch warnings from format drift. Pass false to keep those warnings (debugging library drift).",
        ),
    },
    async (args: { schematicPath: string; autoRefreshLibSymbols?: boolean }) => {
      const result = await callKicadScript("run_erc", args);
      if (result.success) {
        const violations: any[] = result.violations || [];
        const lines: string[] = [`ERC result: ${violations.length} violation(s)`];
        if (result.summary?.by_severity) {
          const s = result.summary.by_severity;
          lines.push(
            `  Errors: ${s.error ?? 0}  Warnings: ${s.warning ?? 0}  Info: ${s.info ?? 0}`,
          );
        }
        const refresh = result.lib_symbols_refresh;
        if (refresh && refresh.refreshed && refresh.refreshed.length > 0) {
          lines.push(
            `  Pre-ERC refresh: ${refresh.refreshed.length} lib_symbols entry(ies) re-aligned with disk (${refresh.refreshed.join(", ")})`,
          );
        }
        const recs: any[] = result.summary?.recommendations || [];
        if (recs.length > 0) {
          lines.push("");
          lines.push("Recommendations:");
          recs.forEach((r: any) => {
            const netList = (r.nets || []).join(", ");
            lines.push(`  • ${r.message}`);
            if (netList) lines.push(`    Nets needing the fix: ${netList}`);
            lines.push(`    ${r.action}`);
          });
        }
        if (violations.length > 0) {
          lines.push("");
          violations.slice(0, 30).forEach((v: any, i: number) => {
            const loc =
              v.location && v.location.x !== undefined
                ? ` @ (${v.location.x}, ${v.location.y})`
                : "";
            const fp = v.likely_false_positive
              ? v.type === "lib_symbol_mismatch"
                ? " [likely lib_symbol FP]"
                : " [likely PWR_FLAG FP]"
              : "";
            lines.push(`${i + 1}. [${v.severity}]${fp} ${v.message}${loc}`);
          });
          if (violations.length > 30) {
            lines.push(`... and ${violations.length - 30} more`);
          }
        }
        return { content: [{ type: "text", text: lines.join("\n") }] };
      } else {
        return {
          content: [
            {
              type: "text",
              text: `ERC failed: ${result.message || "Unknown error"}${result.errorDetails ? "\n" + result.errorDetails : ""}`,
            },
          ],
          isError: true,
        };
      }
    },
  );

  // Generate netlist
  server.tool(
    "generate_netlist",
    "Return a structured JSON netlist — components (reference, value, footprint) and nets (name + connected component/pin pairs). For inspecting connectivity inline; writes no file. To export a netlist file (Spice/KiCad XML/Cadstar/OrcadPCB2), use export_netlist.",
    {
      schematicPath: z.string().describe("Absolute path to the .kicad_sch schematic file"),
    },
    async (args: { schematicPath: string }) => {
      const result = await callKicadScript("generate_netlist", args);
      if (result.success && result.netlist) {
        const netlist = result.netlist;
        const output = [
          `=== Netlist for ${args.schematicPath} ===`,
          `\nComponents (${netlist.components.length}):`,
          ...netlist.components.map(
            (comp: any) =>
              `  ${comp.reference}: ${comp.value} (${comp.footprint || "No footprint"})`,
          ),
          `\nNets (${netlist.nets.length}):`,
          ...netlist.nets.map((net: any) => {
            const connections = net.connections
              .map((conn: any) => `${conn.component}/${conn.pin}`)
              .join(", ");
            return `  ${net.name}: ${connections}`;
          }),
        ].join("\n");

        return {
          content: [
            {
              type: "text",
              text: output,
            },
          ],
        };
      } else {
        return {
          content: [
            {
              type: "text",
              text: `Failed to generate netlist: ${result.message || "Unknown error"}`,
            },
          ],
          isError: true,
        };
      }
    },
  );

  // Sync schematic to PCB board (equivalent to KiCAD F8 / "Update PCB from Schematic")
  server.tool(
    "sync_schematic_to_board",
    "Import the schematic netlist into the PCB (= F8 / Tools → Update PCB from Schematic). Call after the schematic is complete and before placing/routing — without it the board has no footprints or net assignments and place_component/route_pad_to_pad produce an empty, unroutable board.",
    {
      schematicPath: z.string().describe("Absolute path to the .kicad_sch schematic file"),
      boardPath: z.string().describe("Absolute path to the .kicad_pcb board file"),
    },
    async (args: { schematicPath: string; boardPath: string }) => {
      const result = await callKicadScript("sync_schematic_to_board", args);
      return formatKicadResult(result);
    },
  );
}
