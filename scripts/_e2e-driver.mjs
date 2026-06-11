// Temporary e2e driver: connects an MCP client to the real server over stdio,
// then executes tool calls read as JSON lines from a FIFO. Results append to a
// log file. Usage: node scripts/_e2e-driver.mjs <fifo> <log>
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { createReadStream, appendFileSync } from "fs";
import readline from "readline";

const [fifo, logPath] = process.argv.slice(2);
const log = (line) => {
  appendFileSync(logPath, line + "\n");
  console.log(line);
};

const client = new Client({ name: "e2e-test", version: "0" });
const transport = new StdioClientTransport({
  command: "node",
  args: ["dist/index.js"],
  env: { ...process.env, LOG_LEVEL: "info" },
  stderr: "ignore",
});

log(`[driver] connecting at ${new Date().toISOString()}`);
await client.connect(transport);
const { tools } = await client.listTools();
log(`[driver] CONNECTED tools=${tools.length}`);

async function handle(line) {
  let cmd;
  try {
    cmd = JSON.parse(line);
  } catch {
    log(`[driver] BAD JSON: ${line}`);
    return;
  }
  const { id = "?", tool, args = {} } = cmd;
  const t0 = Date.now();
  try {
    const res = await client.callTool({ name: tool, arguments: args }, undefined, {
      timeout: 600000,
    });
    const text = (res.content ?? [])
      .map((c) => (c.type === "text" ? c.text : `[${c.type} ${String(c.data ?? "").length}b]`))
      .join("\n");
    log(`RESULT ${id} isError=${!!res.isError} ${Date.now() - t0}ms\n${text}\nEND ${id}`);
  } catch (err) {
    log(`RESULT ${id} EXCEPTION ${Date.now() - t0}ms\n${err.message}\nEND ${id}`);
  }
}

// FIFO read loop: reopen after each writer closes.
for (;;) {
  const rl = readline.createInterface({ input: createReadStream(fifo) });
  for await (const line of rl) {
    if (line.trim() === "QUIT") {
      log("[driver] quitting");
      await client.close();
      process.exit(0);
    }
    if (line.trim()) await handle(line);
  }
}
