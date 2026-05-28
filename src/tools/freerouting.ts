/**
 * Freerouting autoroute tools for KiCAD MCP server
 *
 * Provides autorouting via Freerouting (Specctra DSN/SES workflow).
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

export function registerFreeroutingTools(server: McpServer, callKicadScript: Function) {
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
    "Run Freerouting autorouter on the current PCB. Exports to Specctra DSN, runs Freerouting CLI, and imports the routed SES result. Requires Java 11+ and freerouting.jar (see check_freerouting). Set `attempts` > 1 to run best-of-N: Freerouting is invoked multiple times with varied `--max-passes`, each result is scored by (nets_routed * 1000 + segments, +50000 bonus when every `targetNets` entry routed), and the winning SES is imported. Single-attempt behaviour is unchanged when `attempts` is omitted.",
    {
      boardPath: z.string().optional().describe("Path to .kicad_pcb file (default: current board)"),
      freeroutingJar: z
        .string()
        .optional()
        .describe(
          "Path to freerouting.jar (default: ~/.kicad-mcp/freerouting.jar or FREEROUTING_JAR env)",
        ),
      maxPasses: z.number().optional().describe(
        "Maximum routing passes for single-attempt mode (default: 20). Ignored when `attempts` > 1; use `passSchedule` instead.",
      ),
      timeout: z.number().optional().describe("Per-attempt timeout in seconds (default: 300)"),
      attempts: z
        .number()
        .int()
        .min(1)
        .optional()
        .describe(
          "Number of Freerouting runs to try (default: 1 — backward-compatible). When > 1, runs best-of-N: scores each attempt by routing completeness and keeps the SES with the highest score. Recommended: 3–5 for dense boards.",
        ),
      targetNets: z
        .array(z.string())
        .optional()
        .describe(
          "Optional list of critical net names. An attempt that routes all of them earns a 50,000-point scoring bonus, breaking ties in favour of designs that include the must-have nets.",
        ),
      passSchedule: z
        .array(z.number())
        .optional()
        .describe(
          "Per-attempt `--max-passes` values to cycle through (default: [50, 60, 65, 70, 75, 80, 85, 90, 55, 95]). The list wraps if `attempts` exceeds its length.",
        ),
    },
    async (args: any) => {
      const result = await callKicadScript("autoroute", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result),
          },
        ],
      };
    },
  );

  // Export DSN only
  server.tool(
    "export_dsn",
    "Export the current PCB to Specctra DSN format. Useful for manual Freerouting workflow or external autorouters.",
    {
      boardPath: z.string().optional().describe("Path to .kicad_pcb file (default: current board)"),
      outputPath: z
        .string()
        .optional()
        .describe("Output DSN file path (default: same dir as board)"),
    },
    async (args: any) => {
      const result = await callKicadScript("export_dsn", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result),
          },
        ],
      };
    },
  );

  // Import SES
  server.tool(
    "import_ses",
    "Import a Specctra SES (session) file into the current PCB. Use after running Freerouting externally.",
    {
      sesPath: z.string().describe("Path to the .ses file to import"),
      boardPath: z.string().optional().describe("Path to .kicad_pcb file (default: current board)"),
    },
    async (args: any) => {
      const result = await callKicadScript("import_ses", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result),
          },
        ],
      };
    },
  );

  // Check Freerouting dependencies
  server.tool(
    "check_freerouting",
    "Check if Java and Freerouting JAR are available on the system. Run this before autoroute to verify prerequisites.  When something's missing the response carries an ``install`` section with concrete commands and the GitHub release URL — the human-readable output below prints them as a copy-pasteable block.",
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
          `  Java: ${result.java.found ? result.java.version ?? "found" : "not found"}` +
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
            step.shell_unix.forEach((cmd: string) =>
              lines.push(`       ${cmd}`),
            );
          }
          if (step.shell_windows && Array.isArray(step.shell_windows)) {
            lines.push("     shell (Windows):");
            step.shell_windows.forEach((cmd: string) =>
              lines.push(`       ${cmd}`),
            );
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
