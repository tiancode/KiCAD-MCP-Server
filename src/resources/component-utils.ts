/**
 * Shared helpers for KiCAD resource modules.
 */

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
