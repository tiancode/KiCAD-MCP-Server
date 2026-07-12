/**
 * Freerouting autoroute tools for KiCAD MCP server
 *
 * Provides autorouting via Freerouting (Specctra DSN/SES workflow).
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { CommandFunction, makePassthrough } from "./tool-response.js";

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
    "Autoroute the current PCB with Freerouting: exports Specctra DSN, runs the Freerouting CLI, imports the routed SES. Requires Java 11+ and freerouting.jar (see check_freerouting). attempts > 1 runs best-of-N with varied --max-passes and imports the result routing the most nets.",
    {
      boardPath: z.string().optional().describe("Path to .kicad_pcb file (default: current board)"),
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

  // Check Freerouting dependencies
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

      return {
        content: [
          {
            type: "text",
            text: lines.join("\n"),
          },
        ],
      };
    },
  );
}
