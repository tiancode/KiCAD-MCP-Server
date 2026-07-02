/**
 * JLCPCB API tools for KiCAD MCP server
 * Provides access to JLCPCB's complete parts catalog via their API
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { formatKicadResult } from "./tool-response.js";

export function registerJLCPCBApiTools(server: McpServer, callKicadScript: Function) {
  // Download JLCPCB parts database
  server.tool(
    "download_jlcpcb_database",
    `Populate the local JLCPCB parts DB (used by search_jlcpcb_parts) via the public JLCSearch API (tscircuit) — no credentials.

This path is paginated (100 parts/request -> ~2.5M parts, ~40-60 min) and leaves category/manufacturer BLANK in the DB. RECOMMENDED INSTEAD: run scripts/download_jlcpcb.py, which pulls the prebuilt jlcparts dataset (~7.15M parts, with category/manufacturer and tiered pricing populated) in ~5 min.

An existing non-empty DB is kept; pass force=true to re-download.`,
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
    `Search the local JLCPCB parts catalog (download_jlcpcb_database first). Returns real pricing, stock, and library type (Basic/Preferred = free or low assembly fee).

MATCHING — read this, it changes how you should call the tool:
- BEST: if you have a candidate manufacturer part number, pass it as 'mpn'. It does an exact (then prefix) lookup and is by far the most reliable path. Prefer searching by a few candidate MPNs over describing the part.
- 'query' is full-text over description + MPN. Value/unit words now match safely ('4.7uF', '0.5A', '510kΩ'). Multiple words must ALL appear in the text (AND); a synonym/format mismatch ('buck' vs 'Step-Down', '0.5A' vs '500mA', 'µH' vs 'uH') can still drop the result. If nothing matches it auto-retries OR-style and flags the result as fuzzy.
- Put package in the 'package' filter, NOT in 'query'.
- 'category'/'manufacturer' are populated and filter correctly when the DB was built from the jlcparts dataset (scripts/download_jlcpcb.py). They are blank only with the older JLCSearch download, in which case the tool detects that, folds the value into the text search, and returns a warning. Check for a warning instead of assuming they don't work.
- in_stock defaults true. A zero in-stock result auto-retries without the stock filter; if matches exist they are returned with out_of_stock_only=true. So an empty result now reliably means "not in this catalog" rather than "just out of stock" — distrust 'not found' less.

COST — JLCPCB assembly economics, apply unless the user says otherwise:
- Prefer Basic, then Preferred, then Extended. Basic parts have no per-part setup fee; Extended parts add a ~$3 one-time fee per unique part number; Preferred are extended parts JLCPCB keeps stocked (better availability, lower/often-waived fee).
- Basic is a SMALL set (~350 parts total), so a Basic-only search returns nothing for most ICs. Strategy: try library_type='Basic', then 'Preferred', then fall back to 'All'/'Extended' — never conclude "no such part" from a Basic-only miss. There is no combined Basic+Preferred filter, so that is two calls.`,
    {
      query: z
        .string()
        .optional()
        .describe(
          "Free-text over description + MPN. All words must match (AND). Best with a candidate MPN; for package use the 'package' filter.",
        ),
      mpn: z
        .string()
        .optional()
        .describe(
          "Manufacturer part number for an exact→prefix lookup (e.g. 'TPS54331DR'). Most reliable; case-insensitive. Takes priority over 'query'.",
        ),
      category: z
        .string()
        .optional()
        .describe(
          "Category filter (e.g. 'Resistors'). Works when the DB was built from jlcparts (scripts/download_jlcpcb.py); blank only with the older JLCSearch DB, where it's folded into text search and a warning is returned.",
        ),
      package: z
        .string()
        .optional()
        .describe("Filter by package type (e.g., '0603', 'SOT-23', 'QFN-32')"),
      library_type: z
        .enum(["Basic", "Extended", "Preferred", "All"])
        .optional()
        .default("All")
        .describe(
          "Filter by library type. Cost order Basic < Preferred < Extended (Basic has no setup fee; Extended adds ~$3 per unique part). Basic is a small set (~350) — try Basic, then Preferred, then All; don't infer 'no part' from a Basic-only miss.",
        ),
      manufacturer: z
        .string()
        .optional()
        .describe(
          "Manufacturer filter (e.g. 'Sunlord'). Works when the DB was built from jlcparts; blank only with the older JLCSearch DB, where it's folded into text search and a warning is returned.",
        ),
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
    `Download a JLCPCB/LCSC part's datasheet PDF to disk by LCSC number.

Resolves the URL from the local database's stored JLCPCB CDN link (present for ~87% of parts after download_jlcpcb_database) and falls back to the constructed LCSC URL (https://www.lcsc.com/datasheet/<lcsc>.pdf) when no stored link exists. The file is verified to be a real PDF (%PDF magic) before it is kept.

Saves to <output_dir>/<lcsc>.pdf (default: the kicad-mcp data dir's datasheets/ folder). Idempotent: an existing non-empty file is returned as-is unless overwrite=true. Requires network access; returns the saved path, source URL, size in bytes, and source (db | lcsc_fallback | cached).`,
    {
      lcsc_number: z.string().describe("LCSC part number (e.g., 'C25804', 'C2286')"),
      output_dir: z
        .string()
        .optional()
        .describe("Directory to save the PDF into (created if missing). Defaults to the data dir."),
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
    `Suggest alternative JLCPCB parts for a given component.

Finds similar parts that may be cheaper, have more stock, or are Basic library type.
Useful for cost optimization and finding alternatives when parts are out of stock.`,
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

  // Import an LCSC/JLCPCB part as a placeable KiCAD symbol + footprint
  server.tool(
    "import_jlcpcb_symbol",
    `Generate a real KiCAD symbol + footprint for an LCSC/JLCPCB part and register it so it can be placed.

The other JLCPCB tools only return database metadata; for ICs and other parts without a KiCAD stock symbol this means hand-building one with create_symbol. This tool removes that step: it uses easyeda2kicad to fetch the part's EasyEDA data by LCSC number (e.g. C7593) and writes a .kicad_sym + .pretty footprint into a shared cache library (~/.kicad-mcp/easyeda.kicad_sym + easyeda.pretty/), registered in the user-global sym-/fp-lib-table under the nickname "easyeda".

After import, place it with add_schematic_component(library="easyeda", componentName=<symbol>) — the response's "symbol" field gives the exact name. Idempotent: a part already in the cache is returned without re-fetching (set forceRefresh to update it).

Requires the easyeda2kicad package in the KiCAD MCP Python environment (pip install easyeda2kicad) and network access to EasyEDA.`,
    {
      lcsc_number: z
        .string()
        .describe(
          'LCSC part number, e.g. "C7593" (the C-prefixed id from search_jlcpcb_parts / get_jlcpcb_part)',
        ),
      forceRefresh: z
        .boolean()
        .optional()
        .default(false)
        .describe("Re-fetch and overwrite even if the part is already in the cache library"),
    },
    async (args: { lcsc_number: string; forceRefresh?: boolean }) => {
      const result = await callKicadScript("import_jlcpcb_symbol", args);
      return formatKicadResult(result);
    },
  );

  // Batch-import multiple LCSC/JLCPCB parts (pre-cache a whole BOM)
  server.tool(
    "import_jlcpcb_symbols",
    `Batch version of import_jlcpcb_symbol: pre-generate KiCAD symbols + footprints for a list of LCSC parts in one call.

Use this to pre-cache a whole BOM up front (during planning) so every part's symbol is ready in the shared "easyeda" library before you start placing — no per-part network wait later. Each id is imported independently: one bad/discontinued id never aborts the rest, already-cached parts are skipped without a network call, and duplicates are processed once.

The response reports requested/imported/cached/failed counts, a "failures" list, and a per-part "results" array (each with status imported|cached|failed and, on success, the symbol name + lib_id). success is true when at least one part was obtained; all_succeeded is true only when nothing failed. Place each part with add_schematic_component(library="easyeda", componentName=<results[i].symbol>).

Requires the easyeda2kicad package and network access to EasyEDA.`,
    {
      lcsc_numbers: z
        .array(z.string())
        .min(1)
        .describe('LCSC part numbers, e.g. ["C7593", "C12087", "C105601"]'),
      forceRefresh: z
        .boolean()
        .optional()
        .default(false)
        .describe("Re-fetch and overwrite parts already in the cache library"),
    },
    async (args: { lcsc_numbers: string[]; forceRefresh?: boolean }) => {
      const result = await callKicadScript("import_jlcpcb_symbols", args);
      return formatKicadResult(result);
    },
  );
}
