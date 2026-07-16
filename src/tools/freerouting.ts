/**
 * Freerouting autoroute tools for KiCAD MCP server
 *
 * Provides autorouting via Freerouting (Specctra DSN/SES workflow).
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { CommandFunction, makePassthrough, textResult } from "./tool-response.js";

export function registerFreeroutingTools(server: McpServer, callKicadScript: CommandFunction) {
  const passthrough = makePassthrough(callKicadScript);
  // Full autoroute: export DSN -> run Freerouting -> import SES
  //
  // Best-of-N support (the `attempts` / `targetNets` / `passSchedule`
  // parameters) is ported from morningfire-pcb-automation:
  //   https://github.com/NiNjA-CodE/morningfire-pcb-automation
  //   (scripts/routing/freeroute_runner.py — `score_ses` + run loop)
  // On dense boards a single attempt regularly leaves 1–7 nets unrouted;
  // cycling through a few `--max-passes` values typically drives the
  // unrouted count to zero.
  server.tool(
    "autoroute",
    "Autoroute a PCB with Freerouting: exports DSN, runs the CLI, imports the SES (REPLACES routing on the SES's nets, no duplicates). Needs Java 11+ and freerouting.jar (check_freerouting). boardPath (or, if omitted, the open board's file) is loaded FRESH and routed — a boardPath naming a DIFFERENT file routes that file and leaves the open board untouched (nonexistent path => FILE_NOT_FOUND); the routed file is returned as routed_board_path. attempts>1 runs best-of-N. By default pre-routed traces are stripped from the DSN (they crash Freerouting 2.2.4) while copper planes are kept — includePreRoutes=true keeps the traces, includePlanes=false also strips the planes. If Freerouting still routes 0 nets the call fails honestly with a hint.",
    {
      boardPath: z
        .string()
        .optional()
        .describe(
          "Path to .kicad_pcb file to route (default: current board). A path other than the open board routes that file and leaves the open board unmodified.",
        ),
      freeroutingJar: z
        .string()
        .optional()
        .describe(
          "Path to freerouting.jar (default: ~/.kicad-mcp/freerouting.jar or FREEROUTING_JAR env)",
        ),
      maxPasses: z
        .number()
        .optional()
        .describe("Max passes in single-attempt mode (default 20); ignored when attempts > 1"),
      timeout: z.number().optional().describe("Per-attempt timeout in seconds (default: 300)"),
      includePreRoutes: z
        .boolean()
        .optional()
        .describe(
          "Keep pre-routed traces (the DSN (wiring ...) block) in what Freerouting sees. Default false: they are stripped from the DSN only (never the board) because they crash Freerouting 2.2.4.",
        ),
      includePlanes: z
        .boolean()
        .optional()
        .describe(
          "Keep copper planes (the DSN (plane ...) entries) in what Freerouting sees. Default TRUE (planes alone don't crash Freerouting, and stripping them turns the GND tree into a huge trace-routing job). Set false to strip them from the DSN only (never the board).",
        ),
      attempts: z
        .number()
        .int()
        .min(1)
        .optional()
        .describe(
          "Runs to try (default 1). Best-of-N by routing completeness; 3–5 for dense boards",
        ),
      targetNets: z
        .array(z.string())
        .optional()
        .describe(
          "Critical net names; attempts routing all of them get a tie-breaking scoring bonus",
        ),
      passSchedule: z
        .array(z.number())
        .optional()
        .describe(
          "Per-attempt --max-passes values (default [50,60,65,70,75,80,85,90,55,95]); wraps if attempts exceeds length",
        ),
    },
    passthrough("autoroute"),
  );

  server.tool(
    "check_freerouting",
    "Check that Java (or Docker) and freerouting.jar are available; run before autoroute. When something's missing the response includes install steps with copy-pasteable commands and download URL.",
    {
      freeroutingJar: z.string().optional().describe("Path to freerouting.jar to check"),
    },
    async (args: any) => {
      const result = await callKicadScript("check_freerouting", args);

      const lines: string[] = [];
      lines.push(`Ready: ${result.ready ? "yes" : "no"}`);
      lines.push(`  Execution mode: ${result.execution_mode}`);
      if (result.java) {
        lines.push(
          `  Java: ${result.java.found ? (result.java.version ?? "found") : "not found"}` +
            (result.java.java_21_ok ? "  (≥21 ✓)" : "  (<21 ✗)"),
        );
      }
      if (result.docker) {
        lines.push(
          `  Docker/Podman: ${result.docker.available ? `available (${result.docker.path})` : "not available"}`,
        );
      }
      if (result.freerouting) {
        lines.push(
          `  freerouting.jar: ${result.freerouting.jar_found ? "found" : "MISSING"} at ${result.freerouting.jar_path}`,
        );
        if (result.freerouting.requested_path) {
          lines.push(
            `    (auto-discovered versioned filename; you requested ${result.freerouting.requested_path})`,
          );
        }
      }

      // Install hint — only present when something's missing.
      const install = result.install;
      if (install && install.steps && install.steps.length > 0) {
        lines.push("");
        lines.push("Install steps:");
        install.steps.forEach((step: any, idx: number) => {
          lines.push(`  ${idx + 1}. ${step.missing}`);
          if (step.summary) lines.push(`     ${step.summary}`);
          if (step.target_path) lines.push(`     → save to: ${step.target_path}`);
          if (step.download_page) lines.push(`     download: ${step.download_page}`);
          if (step.shell_unix && Array.isArray(step.shell_unix)) {
            lines.push("     shell (Linux/macOS):");
            step.shell_unix.forEach((cmd: string) => lines.push(`       ${cmd}`));
          }
          if (step.shell_windows && Array.isArray(step.shell_windows)) {
            lines.push("     shell (Windows):");
            step.shell_windows.forEach((cmd: string) => lines.push(`       ${cmd}`));
          }
          if (step.override_with_env) {
            lines.push(`     override path via env: ${step.override_with_env}`);
          }
          if (step.java_install) lines.push(`     ${step.java_install}`);
          if (step.docker_alt) lines.push(`     ${step.docker_alt}`);
        });
        if (install.after_install) {
          lines.push("");
          lines.push(install.after_install);
        }
      }

      return textResult(lines.join("\n"));
    },
  );
}
