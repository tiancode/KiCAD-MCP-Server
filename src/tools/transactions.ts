/**
 * Transaction tools (IPC-only).
 *
 * Group a sequence of mutating MCP calls into a single KiCad undo step.
 * Useful for multi-step agentic workflows — an AI doing a 5-step PCB
 * refactor produces ONE Ctrl-Z entry the human can use to bail out,
 * not five.
 *
 * Workflow:
 *   1. transaction({action: "begin", description: "Move power section"})
 *   2. move_component / route_trace / add_via / ...   (any number)
 *   3. transaction({action: "commit"}) — atomic undo step lands
 *
 * If anything fails partway through, call transaction({action: "rollback"})
 * to discard everything since begin.
 *
 * Caveat: only create / update / remove of board items participate.
 * set_origin and set_title_block_info are sent as direct kipy property
 * commands and apply immediately, outside the transaction.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { logger } from "../logger.js";
import { CommandFunction, formatKicadResult } from "./tool-response.js";

export function registerTransactionTools(server: McpServer, callKicadScript: CommandFunction) {
  server.tool(
    "transaction",
    "Manage a KiCad transaction / undo group (IPC-only). 'begin' opens one so subsequent mutating calls collapse into a single undo step (no nesting); 'commit' lands it atomically; 'rollback' discards all changes since begin; 'status' reports the open transaction.",
    {
      action: z
        .enum(["begin", "commit", "rollback", "status"])
        .describe("begin | commit | rollback | status"),
      description: z
        .string()
        .optional()
        .describe("Undo label: 'begin' sets it (default 'MCP Operation'); 'commit' overrides it"),
    },
    async (args: { action: "begin" | "commit" | "rollback" | "status"; description?: string }) => {
      const commandByAction = {
        begin: "begin_transaction",
        commit: "commit_transaction",
        rollback: "rollback_transaction",
        status: "get_transaction_status",
      } as const;
      const { action, ...rest } = args;
      const result = await callKicadScript(commandByAction[action], rest);
      return formatKicadResult(result);
    },
  );

  logger.info("Transaction tools registered (1 tool)");
}
