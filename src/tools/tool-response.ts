export type McpTextResult = {
  content: Array<{
    type: "text";
    text: string;
  }>;
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

export function formatKicadResult(result: unknown): McpTextResult {
  const text = JSON.stringify(result) ?? String(result);

  return {
    content: [
      {
        type: "text",
        text,
      },
    ],
    ...(isKicadFailure(result) ? { isError: true as const } : {}),
  };
}

/**
 * Build a parameterless MCP handler that forwards args to the Python
 * subprocess and JSON-pretty-prints the response.  The 5-line inline
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
    return {
      content: [{ type: "text" as const, text: JSON.stringify(result, null, 2) }],
    };
  };
}
