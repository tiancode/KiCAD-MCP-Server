/**
 * tools/list schema slimming.
 *
 * zod-to-json-schema stamps every generated inputSchema with
 * `$schema: "http://json-schema.org/draft-07/schema#"` and emits
 * `additionalProperties: false` on every object node. Neither carries
 * information an MCP client can act on: argument validation happens
 * server-side against the original zod schema on every tools/call, and the
 * $schema URI is boilerplate. Across ~150 tools the two keys cost ≈14 KB
 * (≈3.8k tokens) of the tools/list payload that every client session pays
 * up front, so we strip them from the outbound response.
 *
 * `slimToolsList(server)` wraps the SDK's tools/list request handler (set
 * lazily by McpServer.setToolRequestHandlers on first tool registration —
 * call this AFTER all tools are registered). It mutates only the response
 * objects the handler builds fresh per request, never the registered zod
 * schemas, so server-side validation is unchanged.
 */

import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { logger } from "../logger.js";

/**
 * Recursively remove schema-noise keys. The type guards (`string` /
 * literal `false`) double as safety: a hypothetical tool parameter literally
 * named `$schema` or `additionalProperties` would appear as an object-valued
 * entry in a `properties` map and is left untouched.
 */
function stripSchemaNoise(node: unknown): void {
  if (Array.isArray(node)) {
    for (const item of node) stripSchemaNoise(item);
    return;
  }
  if (node === null || typeof node !== "object") return;
  const obj = node as Record<string, unknown>;
  if (typeof obj.$schema === "string") delete obj.$schema;
  if (obj.additionalProperties === false) delete obj.additionalProperties;
  for (const value of Object.values(obj)) stripSchemaNoise(value);
}

export function slimToolsList(server: McpServer): void {
  // Protocol keeps request handlers in a private Map keyed by method name,
  // and setRequestHandler() refuses to replace an existing entry — so wrap
  // the stored handler directly (SDK 1.x, shared/protocol.ts). If the SDK
  // ever renames the field, we just skip slimming rather than break startup.
  const handlers = (
    server.server as unknown as {
      _requestHandlers?: Map<string, (req: unknown, extra: unknown) => unknown>;
    }
  )._requestHandlers;
  const inner = handlers?.get("tools/list");
  if (!handlers || !inner) {
    logger.warn("tools/list handler not found — skipping schema slimming");
    return;
  }
  handlers.set("tools/list", async (req, extra) => {
    const res = (await inner(req, extra)) as { tools?: Array<{ inputSchema?: unknown }> };
    if (Array.isArray(res?.tools)) {
      for (const tool of res.tools) stripSchemaNoise(tool.inputSchema);
    }
    return res;
  });
}
