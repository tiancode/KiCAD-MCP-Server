/**
 * Schematic Export, ERC, netlist, and board sync tools for KiCAD MCP server.
 * Split out of the former monolithic schematic.ts.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { CommandFunction, makePassthrough } from "../tool-response.js";

export function registerSchematicIoTools(server: McpServer, callKicadScript: CommandFunction) {
  const passthrough = makePassthrough(callKicadScript);
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
    "Run ERC and return violations. Gotcha: every power-input pin needs a power-output pin or PWR_FLAG — " +
      "labels alone aren't enough; summary.recommendations[] lists the PWR_FLAG fixes and " +
      "summary.real_errors counts only non-PWR_FLAG issues. lib_symbols auto-refresh from disk first.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch schematic file"),
      autoRefreshLibSymbols: z
        .boolean()
        .optional()
        .describe(
          "Re-inject lib_symbols from disk before ERC (default true), silencing format-drift lib_symbol_mismatch warnings. false keeps them.",
        ),
      maxViolations: z
        .number()
        .int()
        .optional()
        .describe(
          "Max violations listed in the response (default 30; 0 = all). Totals in summary are unaffected; truncation is shown as 'showing N of M'.",
        ),
    },
    async (args: {
      schematicPath: string;
      autoRefreshLibSymbols?: boolean;
      maxViolations?: number;
    }) => {
      const result = await callKicadScript("run_erc", args);
      if (result.success) {
        const violations: any[] = result.violations || [];
        const summary = result.summary || {};
        const total: number = summary.total ?? violations.length;
        const lines: string[] = [];
        // Headline FIRST: real_errors (excludes PWR_FLAG false positives) is the
        // number that actually matters, then the raw error/warning totals.
        // Only claim an exclusion when one actually happened — otherwise the
        // "excludes ... false positives" boilerplate implied filtering that left
        // the count unchanged (real_errors == raw errors), which read as a bug.
        const raw = summary.raw_by_severity ?? summary.by_severity ?? {};
        const rawErrors: number = raw.error ?? 0;
        const realErrors: number = summary.real_errors ?? 0;
        const excludedErrors = rawErrors - realErrors;
        lines.push(
          excludedErrors > 0
            ? `ERC: ${realErrors} real error(s) — excluded ${excludedErrors} PWR_FLAG/lib_symbol false positive(s) of ${rawErrors} raw`
            : `ERC: ${realErrors} real error(s)`,
        );
        lines.push(
          `  Totals: ${rawErrors} error(s), ${raw.warning ?? 0} warning(s), ${raw.info ?? 0} info — ${total} violation(s)`,
        );
        if (summary.likely_false_positives) {
          lines.push(
            `  Tagged likely false positives (excluded from real_errors): ${summary.likely_false_positives}`,
          );
        }
        const refresh = result.lib_symbols_refresh;
        if (refresh && refresh.refreshed && refresh.refreshed.length > 0) {
          lines.push(
            `  Pre-ERC refresh: ${refresh.refreshed.length} lib_symbols entry(ies) re-aligned with disk (${refresh.refreshed.join(", ")})`,
          );
        }
        const recs: any[] = summary.recommendations || [];
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
          const shown = summary.shown ?? violations.length;
          lines.push(`Violations (showing ${shown} of ${total}):`);
          violations.forEach((v: any, i: number) => {
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
          if (summary.truncated || total > shown) {
            lines.push(
              `... and ${total - shown} more (pass maxViolations=0 to list all, or a higher number)`,
            );
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
    "Return a structured JSON netlist — components (reference, value, footprint) and nets with connected component/pin pairs. Writes no file; to export a netlist file (Spice/XML/etc.) use export_netlist.",
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
  // SPICE simulation (ngspice batch mode)
  server.tool(
    "run_simulation",
    "Run a SPICE analysis (op/tran/dc/ac) via ngspice batch mode on the exported netlist. Returns node " +
      "voltages (op) or x + per-signal arrays downsampled to maxPoints. Requires ngspice on PATH and " +
      "symbols with SPICE model fields assigned.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      analysis: z.enum(["op", "tran", "dc", "ac"]).describe("Analysis type"),
      analysisParams: z
        .object({
          tstep: z.string().optional().describe("tran: time step, e.g. '1u'"),
          tstop: z.string().optional().describe("tran: stop time, e.g. '10m'"),
          tstart: z.string().optional().describe("tran: optional start time"),
          source: z.string().optional().describe("dc: swept source name, e.g. 'V1'"),
          start: z.union([z.number(), z.string()]).optional().describe("dc: sweep start"),
          stop: z.union([z.number(), z.string()]).optional().describe("dc: sweep stop"),
          step: z.union([z.number(), z.string()]).optional().describe("dc: sweep step"),
          variation: z
            .enum(["dec", "oct", "lin"])
            .optional()
            .describe("ac: sweep type (default dec)"),
          points: z
            .union([z.number(), z.string()])
            .optional()
            .describe("ac: points per decade/octave or total"),
          fstart: z.union([z.number(), z.string()]).optional().describe("ac: start frequency"),
          fstop: z
            .union([z.number(), z.string()])
            .optional()
            .describe("ac: stop frequency, e.g. '1Meg'"),
        })
        .optional()
        .describe(
          "Analysis parameters (tran: tstep+tstop; dc: source/start/stop/step; ac: points/fstart/fstop)",
        ),
      signals: z
        .array(z.string())
        .optional()
        .describe("Signals to record, e.g. ['v(out)', 'i(V1)'] — required for tran/dc/ac"),
      maxPoints: z
        .number()
        .int()
        .optional()
        .describe("Downsample returned data to at most this many rows (default 2000)"),
      timeout: z.number().optional().describe("ngspice timeout in seconds (default 120)"),
    },
    passthrough("run_simulation"),
  );

  server.tool(
    "sync_schematic_to_board",
    "Import the schematic netlist into the PCB (= F8 / Update PCB from Schematic). Call after the schematic is complete and before placing/routing — without it the board has no footprints or net assignments.",
    {
      schematicPath: z.string().describe("Absolute path to the .kicad_sch schematic file"),
      boardPath: z.string().describe("Absolute path to the .kicad_pcb board file"),
    },
    passthrough("sync_schematic_to_board"),
  );
}
