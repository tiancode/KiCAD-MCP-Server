/**
 * Package metadata, read once at module load.
 *
 * Extracted from server.ts so the MCP server can report the real release
 * version (instead of a hardcoded fallback) without that file owning the
 * package.json parsing. dist/package-info.js → ../package.json points at
 * the repo root.
 */

import { readFileSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";

export const PACKAGE_INFO: { name: string; version: string; description: string } = (() => {
  try {
    const pkgPath = join(dirname(fileURLToPath(import.meta.url)), "..", "package.json");
    const raw = JSON.parse(readFileSync(pkgPath, "utf-8"));
    return {
      name: typeof raw.name === "string" ? raw.name : "kicad-mcp-server",
      version: typeof raw.version === "string" ? raw.version : "0.0.0",
      description:
        typeof raw.description === "string"
          ? raw.description
          : "MCP server for KiCAD PCB design operations",
    };
  } catch {
    return {
      name: "kicad-mcp-server",
      version: "0.0.0",
      description: "MCP server for KiCAD PCB design operations",
    };
  }
})();
