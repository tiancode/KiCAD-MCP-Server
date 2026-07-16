/**
 * Shared helpers for KiCAD resource modules.
 */

import { logger } from "../logger.js";

/**
 * Build the JSON resource response shape every resource handler returns:
 * `contents` with a single application/json text entry for the given URI.
 */
export function jsonResource(uri: URL, payload: unknown) {
  return {
    contents: [
      {
        uri: uri.href,
        text: JSON.stringify(payload),
        mimeType: "application/json",
      },
    ],
  };
}

/**
 * Build the standard `{ error, details }` JSON resource response used when a
 * KiCAD command fails.
 */
export function resourceError(uri: URL, error: string, details: unknown) {
  return jsonResource(uri, { error, details });
}

/**
 * Log and build the failure response for a KiCAD command a resource depends
 * on — the `if (!result.success)` guard every handler was hand-rolling.
 * `logLabel` names what failed in the log line when it differs from the
 * user-facing `error` (e.g. sub-fetches of an aggregate summary).
 */
export function resourceFailure(uri: URL, error: string, result: any, logLabel = error) {
  logger.error(`${logLabel}: ${result.errorDetails}`);
  return resourceError(uri, error, result.errorDetails);
}

/**
 * The `board` block shared by the board-statistics and project-summary
 * resources, built from a `get_board_info` result.
 */
export function boardSummary(boardResult: any) {
  return {
    size: boardResult.size,
    layers: boardResult.layers?.length || 0,
    title: boardResult.title,
  };
}

/**
 * The `components` block shared by the board-statistics and project-summary
 * resources, built from a `get_component_list` result.
 */
export function componentSummary(componentsResult: any) {
  return {
    count: componentsResult.components?.length || 0,
    types: countComponentTypes(componentsResult.components || []),
  };
}

/**
 * Count components by type, keyed on the first whitespace-delimited token of
 * each component's `value` (falling back to "Unknown"). Used by both the board
 * and project resource summaries.
 */
export function countComponentTypes(components: any[]): Record<string, number> {
  const typeCounts: Record<string, number> = {};

  for (const component of components) {
    const type = component.value?.split(" ")[0] || "Unknown";
    typeCounts[type] = (typeCounts[type] || 0) + 1;
  }

  return typeCounts;
}
