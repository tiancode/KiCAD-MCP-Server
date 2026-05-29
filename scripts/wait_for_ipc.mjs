#!/usr/bin/env node
/**
 * Poll get_backend_info over a single MCP connection until the IPC backend
 * attaches (KiCAD opened with the API server on), or until timeout.
 * Exit 0 = IPC connected; exit 3 = timed out; exit 2 = harness error.
 */
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { dirname, join } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const projectRoot = dirname(__dirname);
const serverEntry = join(projectRoot, "dist", "index.js");

const TIMEOUT_MS = Number(process.argv[2] || 600_000);
const INTERVAL_MS = 10_000;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [serverEntry],
    cwd: projectRoot,
    env: { ...process.env, KICAD_BACKEND: "auto", LOG_LEVEL: "error" },
    stderr: "inherit",
  });
  const client = new Client({ name: "ipc-waiter", version: "1.0.0" }, { capabilities: {} });
  await client.connect(transport, { timeout: 180_000 });

  const deadline = Date.now() + TIMEOUT_MS;
  let n = 0;
  while (Date.now() < deadline) {
    n++;
    let info = {};
    try {
      const res = await client.callTool({ name: "get_backend_info", arguments: {} }, undefined, {
        timeout: 60_000,
      });
      const text = (res.content || []).map((c) => c.text || "").join("");
      info = JSON.parse(text);
    } catch (e) {
      info = { error: String(e) };
    }
    const elapsed = Math.round((TIMEOUT_MS - (deadline - Date.now())) / 1000);
    console.log(
      `[poll ${n} +${elapsed}s] backend=${info.backend} ipc_connected=${info.ipc_connected} kicad_running=${info.kicad_running} version=${info.version}`,
    );
    if (info.ipc_connected === true) {
      console.log(`IPC ATTACHED: backend=${info.backend} version=${info.version}`);
      await client.close();
      process.exit(0);
    }
    await sleep(INTERVAL_MS);
  }
  console.log("TIMED OUT waiting for IPC.");
  await client.close();
  process.exit(3);
}
main().catch((e) => {
  console.error("WAITER ERROR:", e);
  process.exit(2);
});
