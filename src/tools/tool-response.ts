/**
 * Signature of the server's `callKicadScript` bridge, shared by tool and
 * resource registrars (each previously declared an identical private copy).
 */
export type CommandFunction = (command: string, params: Record<string, unknown>) => Promise<any>;

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

/**
 * Narrow a result to a plain object (record). Arrays are excluded: although
 * `typeof [] === "object"`, MCP's outbound CallToolResultSchema requires
 * `structuredContent` to be an object, so attaching an array there would make
 * the SDK reject an otherwise-successful tool result.
 */
function asRecord(result: unknown): Record<string, unknown> | null {
  return typeof result === "object" && result !== null && !Array.isArray(result)
    ? (result as Record<string, unknown>)
    : null;
}

function isKicadFailure(record: Record<string, unknown> | null): boolean {
  return record !== null && record.success === false;
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
  const isError = isKicadFailure(record);

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
 * Bind a `callKicadScript` once and return a `(command) => handler` factory:
 * each handler forwards its args verbatim to the Python subprocess and
 * serializes the response via `formatKicadResult`.  The 5-line inline
 * closure was being copy-pasted into every tool file — centralizing it here
 * means future changes (error routing, structured content blocks, etc.)
 * only touch this one place.
 */
export function makePassthrough(callKicadScript: CommandFunction) {
  return (command: string) =>
    async (args: Record<string, unknown> = {}) => {
      const result = await callKicadScript(command, args);
      return formatKicadResult(result);
    };
}
