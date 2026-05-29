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
import { formatKicadResult } from "./tool-response.js";

export function registerTransactionTools(server: McpServer, callKicadScript: Function) {
  server.tool(
    "transaction",
    "Manage a KiCad transaction / undo group (IPC-only). `action`: 'begin' opens a transaction so subsequent mutating calls collapse into a single Ctrl-Z undo step (refuses to nest — commit or rollback the open one first); 'commit' lands it as one atomic undo step (fails if none open); 'rollback' discards every change since begin (fails if none open); 'status' reports whether one is open and its label. 'begin'/'commit' accept an optional `description` label.",
    {
      action: z
        .enum(["begin", "commit", "rollback", "status"])
        .describe("Transaction operation: begin | commit | rollback | status."),
      description: z
        .string()
        .optional()
        .describe(
          "Undo-history label. On 'begin' sets the label (default 'MCP Operation'); on 'commit' overrides the begin label. Ignored by 'rollback'/'status'.",
        ),
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
