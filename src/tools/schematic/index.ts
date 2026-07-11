/**
 * Schematic tools for KiCAD MCP server.
 *
 * This module was split out of a single 2300-line file into per-category
 * sub-modules (component, wire, query, io, view) that mirror the Python
 * handler layout. registerSchematicTools wires them all up so existing
 * imports keep working unchanged.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { registerSchematicComponentTools } from "./component.js";
import { registerSchematicWireTools } from "./wire.js";
import { registerSchematicQueryTools } from "./query.js";
import { registerSchematicIoTools } from "./io.js";
import { registerSchematicViewTools } from "./view.js";
import { CommandFunction } from "../tool-response.js";

export function registerSchematicTools(server: McpServer, callKicadScript: CommandFunction) {
  registerSchematicComponentTools(server, callKicadScript);
  registerSchematicWireTools(server, callKicadScript);
  registerSchematicQueryTools(server, callKicadScript);
  registerSchematicIoTools(server, callKicadScript);
  registerSchematicViewTools(server, callKicadScript);
}
