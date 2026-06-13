/**
 * Shared helpers for KiCAD resource modules.
 */

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
