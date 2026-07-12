/**
 * JLCPCB API tools for KiCAD MCP server
 * Provides access to JLCPCB's complete parts catalog via their API
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { CommandFunction, formatKicadResult, makePassthrough } from "./tool-response.js";

export function registerJLCPCBApiTools(server: McpServer, callKicadScript: CommandFunction) {
  const passthrough = makePassthrough(callKicadScript);
  // Download JLCPCB parts database
  server.tool(
    "download_jlcpcb_database",
    `Populate the local JLCPCB parts DB (used by search_jlcpcb_parts) via the public JLCSearch API (tscircuit) — no credentials.

This path is slow (~40-60 min) and leaves category/manufacturer BLANK. RECOMMENDED INSTEAD: scripts/download_jlcpcb.py (jlcparts dataset, ~7.15M parts with category/manufacturer/pricing, ~5 min). An existing non-empty DB is kept.`,
    {
      force: z
        .boolean()
        .optional()
        .default(false)
        .describe("Force re-download even if database exists"),
    },
    async (args: { force?: boolean }) => {
      const result = await callKicadScript("download_jlcpcb_database", args);
      if (result.success) {
        return {
          content: [
            {
              type: "text",
              text:
                `✓ Successfully downloaded JLCPCB parts database\n\n` +
                `Total parts: ${result.total_parts}\n` +
                `Basic parts: ${result.basic_parts}\n` +
                `Extended parts: ${result.extended_parts}\n` +
                `Database size: ${result.db_size_mb} MB\n` +
                `Database path: ${result.db_path}`,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text",
            text:
              `✗ Failed to download JLCPCB database: ${result.message || "Unknown error"}\n\n` +
              `The JLCSearch path needs no credentials but is slow/flaky; for a reliable, fuller catalog run scripts/download_jlcpcb.py instead.`,
          },
        ],
        isError: true,
      };
    },
  );

  // Search JLCPCB parts
  server.tool(
    "search_jlcpcb_parts",
    `Search the local JLCPCB catalog (download_jlcpcb_database first). 'query' is AND full-text over description+MPN; synonym/format mismatches can miss — a no-match auto-retries OR-style, flagged fuzzy. category/manufacturer only work with the jlcparts-built DB (scripts/download_jlcpcb.py); with the older JLCSearch DB they fold into text search — check the returned warning. Zero-stock results auto-retry without the stock filter (flagged out_of_stock_only), so empty = not in the catalog. Cost Basic < Preferred < Extended (Basic: no setup fee, ~350 parts; Extended: ~$3/unique part; Preferred: stocked Extended) — try Basic → Preferred → All (no combined Basic+Preferred; two calls); never conclude absence from a Basic-only miss.`,
    {
      query: z
        .string()
        .optional()
        .describe("Free-text search; put package in the 'package' filter, not here."),
      mpn: z
        .string()
        .optional()
        .describe(
          "MPN exact→prefix lookup — most reliable path; prefer candidate MPNs over free-text. Overrides 'query'.",
        ),
      category: z.string().optional().describe("Category filter (e.g. 'Resistors')"),
      package: z
        .string()
        .optional()
        .describe("Filter by package type (e.g., '0603', 'SOT-23', 'QFN-32')"),
      library_type: z
        .enum(["Basic", "Extended", "Preferred", "All"])
        .optional()
        .default("All")
        .describe("Filter by library type (see cost notes in tool description)"),
      manufacturer: z.string().optional().describe("Manufacturer filter (e.g. 'Sunlord')"),
      in_stock: z
        .boolean()
        .optional()
        .default(true)
        .describe("Only show parts with available stock"),
      limit: z.number().optional().default(20).describe("Maximum number of results to return"),
    },
    async (args: any) => {
      const result = await callKicadScript("search_jlcpcb_parts", args);
      if (result.success && result.parts) {
        const warnings: string[] = Array.isArray(result.warnings) ? result.warnings : [];
        const warningText = warnings.length > 0 ? `⚠️ ${warnings.join("\n⚠️ ")}\n\n` : "";

        if (result.parts.length === 0) {
          return {
            content: [
              {
                type: "text",
                text:
                  warningText +
                  `No JLCPCB parts found matching your criteria (incl. out-of-stock).\n\n` +
                  `Tip: search by a candidate manufacturer part number via the 'mpn' parameter — ` +
                  `it's the most reliable path. For free-text, fewer/looser words match better ` +
                  `(all words must match), and put package in the 'package' filter.`,
              },
            ],
          };
        }

        const oosNote = result.out_of_stock_only
          ? `⚠️ No in-stock matches — these parts exist in the catalog but are OUT OF STOCK.\n\n`
          : "";

        const fuzzyNote = result.fuzzy
          ? `⚠️ Fuzzy match (no exact all-terms hit) — results are best-effort, ranked by relevance.\n\n`
          : "";

        const partsList = result.parts
          .map((p: any) => {
            const priceInfo =
              p.price_breaks && p.price_breaks.length > 0
                ? ` - $${p.price_breaks[0].price}/ea`
                : "";
            const stockInfo = p.stock > 0 ? ` (${p.stock} in stock)` : " (out of stock)";
            return `${p.lcsc}: ${p.mfr_part} - ${p.description} [${p.library_type}]${priceInfo}${stockInfo}`;
          })
          .join("\n");

        return {
          content: [
            {
              type: "text",
              text:
                warningText +
                oosNote +
                fuzzyNote +
                `Found ${result.count} JLCPCB parts:\n\n${partsList}\n\n` +
                `💡 Cost: prefer Basic (no setup fee), then Preferred (stocked, low/no fee), then Extended (~$3 setup fee per unique part).`,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text",
            text:
              `Failed to search JLCPCB parts: ${result.message || "Unknown error"}\n\n` +
              `Make sure you've downloaded the database first using download_jlcpcb_database.`,
          },
        ],
        isError: true,
      };
    },
  );

  // Get JLCPCB part details
  server.tool(
    "get_jlcpcb_part",
    "Get detailed information about a specific JLCPCB part by LCSC number",
    {
      lcsc_number: z.string().describe("LCSC part number (e.g., 'C25804', 'C2286')"),
    },
    async (args: { lcsc_number: string }) => {
      const result = await callKicadScript("get_jlcpcb_part", args);
      if (result.success && result.part) {
        const p = result.part;
        const priceTable =
          p.price_breaks && p.price_breaks.length > 0
            ? "\n\nPrice Breaks:\n" +
              p.price_breaks.map((pb: any) => `  ${pb.qty}+: $${pb.price}/ea`).join("\n")
            : "";

        const footprints =
          result.footprints && result.footprints.length > 0
            ? "\n\nSuggested KiCAD Footprints:\n" +
              result.footprints.map((f: string) => `  - ${f}`).join("\n")
            : "";

        return {
          content: [
            {
              type: "text",
              text:
                `LCSC: ${p.lcsc}\n` +
                `MFR Part: ${p.mfr_part}\n` +
                `Manufacturer: ${p.manufacturer}\n` +
                `Category: ${p.category} / ${p.subcategory}\n` +
                `Package: ${p.package}\n` +
                `Description: ${p.description}\n` +
                `Library Type: ${p.library_type} ${p.library_type === "Basic" ? "(Free assembly!)" : ""}\n` +
                `Stock: ${p.stock}\n` +
                (p.datasheet ? `Datasheet: ${p.datasheet}\n` : "") +
                priceTable +
                footprints,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text",
            text:
              `Part not found: ${args.lcsc_number}\n\n` +
              `Make sure you've downloaded the JLCPCB database first.`,
          },
        ],
        isError: true,
      };
    },
  );

  // Download a part's datasheet PDF
  server.tool(
    "download_jlcpcb_datasheet",
    `Download a JLCPCB/LCSC part's datasheet PDF by LCSC number to <output_dir>/<lcsc>.pdf. Uses the local DB's stored CDN link, falling back to https://www.lcsc.com/datasheet/<lcsc>.pdf; verified to be a real PDF before keeping. Requires network. Returns saved path, source URL, bytes, and source (db | lcsc_fallback | cached).`,
    {
      lcsc_number: z.string().describe("LCSC part number (e.g., 'C25804', 'C2286')"),
      output_dir: z
        .string()
        .optional()
        .describe(
          "Directory for the PDF (created if missing). Default: data dir's datasheets/ folder.",
        ),
      overwrite: z
        .boolean()
        .optional()
        .default(false)
        .describe("Re-download even if a non-empty file already exists"),
    },
    async (args: { lcsc_number: string; output_dir?: string; overwrite?: boolean }) => {
      const result = await callKicadScript("download_jlcpcb_datasheet", args);
      if (result.success) {
        const kb = result.bytes ? ` (${(result.bytes / 1024).toFixed(0)} KB)` : "";
        const srcNote =
          result.source === "cached"
            ? " [already cached]"
            : result.source === "lcsc_fallback"
              ? " [via lcsc.com fallback]"
              : "";
        return {
          content: [
            {
              type: "text",
              text:
                `✓ Datasheet for ${result.lcsc} saved${srcNote}${kb}\n\n` +
                `Path: ${result.path}\n` +
                `Source URL: ${result.url}`,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text",
            text:
              `✗ Failed to download datasheet: ${result.message || "Unknown error"}` +
              (result.url ? `\n\nURL tried: ${result.url}` : ""),
          },
        ],
        isError: true,
      };
    },
  );

  // Get JLCPCB database statistics
  server.tool(
    "get_jlcpcb_database_stats",
    "Get statistics about the local JLCPCB parts database",
    {},
    async () => {
      const result = await callKicadScript("get_jlcpcb_database_stats", {});
      if (result.success) {
        const stats = result.stats;
        return {
          content: [
            {
              type: "text",
              text:
                `JLCPCB Database Statistics:\n\n` +
                `Total parts: ${stats.total_parts.toLocaleString()}\n` +
                `Basic parts: ${stats.basic_parts.toLocaleString()} (free assembly)\n` +
                `Extended parts: ${stats.extended_parts.toLocaleString()} ($3 setup fee each)\n` +
                `In stock: ${stats.in_stock.toLocaleString()}\n` +
                `Database path: ${stats.db_path}`,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text",
            text:
              `JLCPCB database not found or empty.\n\n` +
              `Run download_jlcpcb_database first to populate the database.`,
          },
        ],
        isError: true,
      };
    },
  );

  // Suggest alternative parts
  server.tool(
    "suggest_jlcpcb_alternatives",
    "Suggest similar JLCPCB parts that may be cheaper, better stocked, or Basic library type — for cost optimization or out-of-stock parts.",
    {
      lcsc_number: z.string().describe("Reference LCSC part number to find alternatives for"),
      limit: z.number().optional().default(5).describe("Maximum number of alternatives to return"),
    },
    async (args: { lcsc_number: string; limit?: number }) => {
      const result = await callKicadScript("suggest_jlcpcb_alternatives", args);
      if (result.success && result.alternatives) {
        if (result.alternatives.length === 0) {
          return {
            content: [
              {
                type: "text",
                text: `No alternatives found for ${args.lcsc_number}`,
              },
            ],
          };
        }

        const altsList = result.alternatives
          .map((p: any, i: number) => {
            const priceInfo =
              p.price_breaks && p.price_breaks.length > 0
                ? ` - $${p.price_breaks[0].price}/ea`
                : "";
            const savings =
              result.reference_price && p.price_breaks && p.price_breaks.length > 0
                ? ` (${((1 - p.price_breaks[0].price / result.reference_price) * 100).toFixed(0)}% cheaper)`
                : "";
            return `${i + 1}. ${p.lcsc}: ${p.mfr_part} [${p.library_type}]${priceInfo}${savings}\n   ${p.description}\n   Stock: ${p.stock}`;
          })
          .join("\n\n");

        return {
          content: [
            {
              type: "text",
              text: `Alternative parts for ${args.lcsc_number}:\n\n${altsList}`,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text",
            text: `Failed to find alternatives: ${result.message || "Unknown error"}`,
          },
        ],
        isError: true,
      };
    },
  );

  // Import LCSC/JLCPCB parts as placeable KiCAD symbols + footprints
  server.tool(
    "import_jlcpcb_symbols",
    `Generate KiCAD symbols + footprints for LCSC parts via easyeda2kicad into the shared "easyeda" cache library. Batch a whole BOM: cached parts are skipped, one bad id never aborts the rest. Then place with add_schematic_component(symbol="easyeda:<name from response>"). Needs easyeda2kicad + network.`,
    {
      lcscNumbers: z
        .array(z.string())
        .min(1)
        .describe('LCSC part numbers, e.g. ["C7593"] — single-element array is fine'),
      forceRefresh: z
        .boolean()
        .optional()
        .default(false)
        .describe("Re-fetch and overwrite parts already in the cache library"),
      inferPinTypes: z
        .boolean()
        .optional()
        .default(true)
        .describe(
          "Retype unambiguous power pins (VDD*/VCC*/VSS*/GND*/VBAT) from easyeda2kicad's " +
            "blanket 'unspecified' to 'power_in' so ERC checks power driving. false leaves pins as-is.",
        ),
    },
    async (args: { lcscNumbers: string[]; forceRefresh?: boolean; inferPinTypes?: boolean }) => {
      const { lcscNumbers, forceRefresh, inferPinTypes } = args;
      // A single part dispatches to the singular Python command (returns the
      // symbol directly); multiple parts use the batch command (per-part
      // "results" array with imported|cached|failed statuses).
      const result =
        lcscNumbers.length === 1
          ? await callKicadScript("import_jlcpcb_symbol", {
              lcsc_number: lcscNumbers[0],
              forceRefresh,
              inferPinTypes,
            })
          : await callKicadScript("import_jlcpcb_symbols", {
              lcsc_numbers: lcscNumbers,
              forceRefresh,
              inferPinTypes,
            });
      return formatKicadResult(result);
    },
  );
  // BOM availability check against the local JLCPCB catalog
  server.tool(
    "check_bom_availability",
    "Check each BOM line of the loaded board against the local JLCPCB catalog (download_jlcpcb_database first): " +
      "groups by value+footprint, matches by LCSC field (exact) or value+package search, reports stock, unit price " +
      "at quantity, and cost per board. not_found / low_stock / out_of_stock lines need sourcing attention.",
    {
      boardQty: z
        .number()
        .int()
        .optional()
        .describe("Boards to order (default 1) — drives price breaks and stock sufficiency"),
    },
    passthrough("check_bom_availability"),
  );
}
