#!/usr/bin/env node
/**
 * MCP test-client harness for the KiCAD MCP Server.
 *
 * Spawns `node dist/index.js` over a real MCP stdio transport (the SDK's
 * StdioClientTransport), performs the MCP initialize handshake, then runs a
 * scripted list of tools/call steps from a JSON "plan" file, printing each
 * result. This exercises the FULL stack: MCP protocol -> TS tools layer ->
 * Python subprocess -> KiCAD (SWIG pcbnew / IPC).
 *
 * Usage:  node scripts/mcp_test_client.mjs <plan.json>
 *
 * Plan file shape:
 *   {
 *     "listTools": true,                // optional: print tool count + names
 *     "steps": [
 *       { "tool": "create_project", "args": {...}, "label": "...",
 *         "long": false,                // give the call the 10-min budget
 *         "optional": true,             // don't count an error as a failure
 *         "expectError": true }         // a success:false IS the pass condition
 *     ]
 *   }
 */
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { readFileSync, writeFileSync } from "fs";
import { dirname, join, resolve } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const projectRoot = dirname(__dirname);
const serverEntry = join(projectRoot, "dist", "index.js");

const planPath = process.argv[2] || join(__dirname, "plan.json");
const plan = JSON.parse(readFileSync(planPath, "utf8"));

const LONG_TIMEOUT = 600_000; // 10 min — matches the server's long-op budget
const NORMAL_TIMEOUT = 120_000;

function trim(s, n = 1400) {
  if (typeof s !== "string") s = JSON.stringify(s);
  return s.length > n ? s.slice(0, n) + ` …[+${s.length - n} chars]` : s;
}

function renderContent(content) {
  if (!Array.isArray(content)) return trim(content);
  return content
    .map((c) => {
      if (c.type === "text") return c.text;
      if (c.type === "image")
        return `[image ${c.mimeType} ${c.data ? c.data.length + " b64-bytes" : "no-data"}]`;
      return `[${c.type}]`;
    })
    .join("\n");
}

async function main() {
  const transport = new StdioClientTransport({
    command: process.execPath, // node
    args: [serverEntry],
    cwd: projectRoot,
    env: { ...process.env, KICAD_BACKEND: process.env.KICAD_BACKEND || "auto", LOG_LEVEL: "warn" },
    stderr: "inherit",
  });

  const client = new Client(
    { name: "kicad-mcp-test-client", version: "1.0.0" },
    { capabilities: {} },
  );

  console.log(`▶ connecting to server: node ${serverEntry}`);
  const t0 = Date.now();
  await client.connect(transport, { timeout: 180_000 });
  console.log(`✓ connected + initialized in ${((Date.now() - t0) / 1000).toFixed(1)}s\n`);

  if (plan.listTools) {
    const { tools } = await client.listTools();
    console.log(`TOOLS REGISTERED: ${tools.length}`);
    console.log(
      tools
        .map((t) => t.name)
        .sort()
        .join(", "),
    );
    console.log("");
  }

  const results = [];
  let pass = 0;
  let fail = 0;
  const failed = [];

  for (let i = 0; i < plan.steps.length; i++) {
    const step = plan.steps[i];
    const tag = `#${String(i + 1).padStart(2, "0")} ${step.tool}${step.label ? ` (${step.label})` : ""}`;
    const timeout = step.long ? LONG_TIMEOUT : NORMAL_TIMEOUT;
    try {
      const res = await client.callTool(
        { name: step.tool, arguments: step.args || {} },
        undefined,
        { timeout, maxTotalTimeout: timeout },
      );
      const text = renderContent(res.content);
      const isErr = res.isError === true;
      const ok = step.expectError ? isErr : !isErr;
      results.push({ step: tag, isError: isErr, text });
      if (ok) {
        pass++;
        console.log(`✅ ${tag}${step.expectError ? " [expected-error]" : ""}\n${trim(text)}\n`);
      } else if (step.optional) {
        console.log(`⚠️  ${tag} [optional, non-fatal]\n${trim(text)}\n`);
      } else {
        fail++;
        failed.push(tag);
        console.log(`❌ ${tag}\n${trim(text)}\n`);
      }
    } catch (e) {
      results.push({ step: tag, threw: String(e) });
      if (step.optional) {
        console.log(`⚠️  ${tag} [optional, threw]\n${trim(String(e))}\n`);
      } else {
        fail++;
        failed.push(tag);
        console.log(`❌ ${tag} THREW\n${trim(String(e))}\n`);
      }
    }
  }

  const outPath = join(__dirname, "last_run.json");
  writeFileSync(outPath, JSON.stringify(results, null, 2));
  console.log("──────────────────────────────────────────");
  console.log(`SUMMARY: ${pass} passed, ${fail} failed, ${plan.steps.length} steps total`);
  if (failed.length) console.log(`FAILED: ${failed.join(" | ")}`);
  console.log(`(full results → ${outPath})`);

  await client.close();
  process.exit(fail > 0 ? 1 : 0);
}

main().catch((e) => {
  console.error("HARNESS ERROR:", e);
  process.exit(2);
});
