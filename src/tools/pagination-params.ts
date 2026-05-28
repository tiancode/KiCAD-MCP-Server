import { z } from "zod";

/**
 * Shared optional pagination params for list-returning tools. Spread into a
 * tool's zod shape (`{ ...existing, ...paginationParams }`) so the agent can
 * page past the server-side default cap (100). Omitting them returns the
 * first page; responses carry `total` / `truncated` so the agent knows
 * whether there is more to fetch.
 */
export const paginationParams = {
  limit: z
    .number()
    .int()
    .optional()
    .describe("Max items to return (default 100; 0 = no cap)"),
  offset: z
    .number()
    .int()
    .optional()
    .describe("Items to skip before returning (default 0), for paging"),
};

/** Just `offset` — for tools that already declare their own `limit`. */
export const offsetParam = {
  offset: z
    .number()
    .int()
    .optional()
    .describe("Items to skip before returning (default 0), for paging"),
};

/**
 * Build a human-readable "showing X-Y of N" suffix for tools that format
 * their list as text (and would otherwise hide the pagination metadata the
 * Python side returns). Empty string when the response was not truncated.
 */
export function truncationNote(result: {
  truncated?: boolean;
  total?: number;
  count?: number;
  offset?: number;
}): string {
  if (!result || !result.truncated) return "";
  const offset = result.offset ?? 0;
  const count = result.count ?? 0;
  const total = result.total ?? offset + count;
  return `\n\n[showing ${offset + 1}-${offset + count} of ${total}; pass offset=${offset + count} (and limit) to page]`;
}
