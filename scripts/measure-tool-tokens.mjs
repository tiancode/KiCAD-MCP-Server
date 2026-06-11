#!/usr/bin/env node
/**
 * Measure the token cost of this server's tools/list payload — the fixed
 * per-session context cost every MCP client pays for all registered tools.
 *
 * Registers every tool against a throwaway McpServer (no Python subprocess),
 * invokes the real tools/list handler — including the schema-slimming layer
 * from src/tools/schema-slim.ts — and reports total size plus the largest
 * tools, so description/schema bloat shows up before it ships.
 *
 * Usage:  npm run build && node scripts/measure-tool-tokens.mjs [--top N]
 *
 * "Tokens" are estimated at ~3.7 chars/token (typical for this payload mix
 * of JSON punctuation and English text); treat them as relative, not exact.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import * as tools from "../dist/tools/index.js";
import { withToolAnnotations } from "../dist/tools/annotations.js";
import { slimToolsList } from "../dist/tools/schema-slim.js";

const CHARS_PER_TOKEN = 3.7;
const top = Number(process.argv[process.argv.indexOf("--top") + 1]) || 15;
const tok = (chars) => Math.round(chars / CHARS_PER_TOKEN);

const server = new McpServer({ name: "measure", version: "0" });
const annotated = withToolAnnotations(server);
const noop = async () => ({ success: true });
for (const register of Object.values(tools)) {
  if (typeof register === "function") register(annotated, noop);
}
slimToolsList(server);

const handler = server.server._requestHandlers.get("tools/list");
const { tools: list } = await handler({ method: "tools/list", params: {} }, {});

const rows = list
  .map((t) => ({
    name: t.name,
    chars: JSON.stringify(t).length,
    descChars: (t.description ?? "").length,
  }))
  .sort((a, b) => b.chars - a.chars);
const total = rows.reduce((sum, r) => sum + r.chars, 0);

console.log(`tools: ${rows.length}`);
console.log(`tools/list payload: ${total} chars ≈ ${tok(total)} tokens`);
console.log(`\ntop ${top} largest tools (total chars / description chars):`);
for (const r of rows.slice(0, top)) {
  console.log(`${String(r.chars).padStart(6)} ${String(r.descChars).padStart(5)}  ${r.name}`);
}
const longDescs = rows.filter((r) => r.descChars > 400);
if (longDescs.length) {
  console.log(`\n⚠ ${longDescs.length} tool(s) with description > 400 chars:`);
  for (const r of longDescs) console.log(`  ${r.descChars}  ${r.name}`);
}
