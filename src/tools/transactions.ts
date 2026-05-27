/**
 * Transaction tools (IPC-only).
 *
 * Group a sequence of mutating MCP calls into a single KiCad undo step.
 * Useful for multi-step agentic workflows — an AI doing a 5-step PCB
 * refactor produces ONE Ctrl-Z entry the human can use to bail out,
 * not five.
 *
 * Workflow:
 *   1. begin_transaction({description: "Move power section"})
 *   2. move_component / route_trace / add_via / ...   (any number)
 *   3. commit_transaction() — atomic undo step lands
 *
 * If anything fails partway through, call rollback_transaction() to
 * discard everything since begin.
 *
 * Caveat: only create / update / remove of board items participate.
 * set_origin and set_title_block_info are sent as direct kipy property
 * commands and apply immediately, outside the transaction.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { logger } from "../logger.js";
import { passthroughCall } from "./tool-response.js";

export function registerTransactionTools(server: McpServer, callKicadScript: Function) {
  const passthrough = (command: string) =>
    passthroughCall(callKicadScript as Parameters<typeof passthroughCall>[0], command);

  server.tool(
    "begin_transaction",
    "Open a KiCad transaction (IPC-only). Mutating calls made after this collapse into a single Ctrl-Z undo step until you call commit_transaction. Refuses to nest — commit or rollback the existing transaction first.",
    {
      description: z
        .string()
        .optional()
        .describe(
          "Label shown in KiCad's undo history (default 'MCP Operation'). Use something the human will recognize.",
        ),
    },
    passthrough("begin_transaction"),
  );

  server.tool(
    "commit_transaction",
    "Push the currently open transaction to KiCad as one atomic undo step (IPC-only). Fails if no transaction is open.",
    {
      description: z
        .string()
        .optional()
        .describe(
          "Override the label set at begin_transaction. Omit to keep the original label.",
        ),
    },
    passthrough("commit_transaction"),
  );

  server.tool(
    "rollback_transaction",
    "Discard the currently open transaction — every change since begin_transaction is reverted (IPC-only). Fails if no transaction is open.",
    {},
    passthrough("rollback_transaction"),
  );

  server.tool(
    "get_transaction_status",
    "Report whether a transaction is currently open and its description label (IPC-only).",
    {},
    passthrough("get_transaction_status"),
  );

  logger.info("Transaction tools registered (4 tools)");
}
