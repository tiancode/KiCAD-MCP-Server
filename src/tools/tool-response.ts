export type McpTextResult = {
  content: Array<{
    type: "text";
    text: string;
  }>;
  /**
   * The raw Python result object, surfaced for MCP clients that consume
   * structured output instead of re-parsing the JSON text block. No
   * `outputSchema` is declared on the tools, so the SDK does NOT validate
   * this (see McpServer.validateToolOutput — it returns early when the tool
   * has no output schema), making it safe to attach for every result shape.
   */
  structuredContent?: Record<string, unknown>;
  isError?: true;
};

function isKicadFailure(result: unknown): boolean {
  return (
    typeof result === "object" &&
    result !== null &&
    "success" in result &&
    (result as { success?: unknown }).success === false
  );
}

function asRecord(result: unknown): Record<string, unknown> | null {
  return typeof result === "object" && result !== null
    ? (result as Record<string, unknown>)
    : null;
}

function pickString(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  return typeof value === "string" ? value : "";
}

/**
 * Build a one-line, human-readable summary that leads the text block so the
 * agent (and the user reading the transcript) sees the outcome without
 * scanning the whole JSON blob. On failure it lifts the buried `message`,
 * `errorCode`, and remediation `hint` to the front; on success it surfaces a
 * `message` only when the tool provided one.
 */
function summarize(record: Record<string, unknown> | null, isError: boolean): string {
  if (!record) return "";

  const message = pickString(record, "message") || pickString(record, "error");

  if (isError) {
    const code = pickString(record, "errorCode");
    const hint = pickString(record, "hint");
    const head = `❌ Error${code ? ` [${code}]` : ""}: ${message || "operation failed"}`;
    return hint ? `${head}\n💡 ${hint}` : head;
  }

  return message ? `✓ ${message}` : "";
}

export function formatKicadResult(result: unknown): McpTextResult {
  const record = asRecord(result);
  const isError = isKicadFailure(result);

  const summary = summarize(record, isError);
  const json = JSON.stringify(result) ?? String(result);
  const text = summary ? `${summary}\n${json}` : json;

  const out: McpTextResult = {
    content: [{ type: "text", text }],
  };
  if (record) out.structuredContent = record;
  if (isError) out.isError = true;
  return out;
}

/**
 * Build a parameterless MCP handler that forwards args to the Python
 * subprocess and serializes the response as compact JSON.  The 5-line inline
 * closure was being copy-pasted into every tool file — pulling it here
 * keeps the content shape in one place so future changes (error
 * routing, structured content blocks, etc.) only touch one file.
 */
export function passthroughCall(
  callKicadScript: (command: string, args: Record<string, unknown>) => Promise<unknown>,
  command: string,
) {
  return async (args: Record<string, unknown> = {}) => {
    const result = await callKicadScript(command, args);
    return formatKicadResult(result);
  };
}
