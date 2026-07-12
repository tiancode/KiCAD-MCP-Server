/**
 * Configuration handling for KiCAD MCP server
 */

import { readFile } from "fs/promises";
import { existsSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";
import { z } from "zod";
import { logger } from "./logger.js";

// Get the current directory
const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Default config location
const DEFAULT_CONFIG_PATH = join(dirname(__dirname), "config", "default-config.json");

/**
 * Server configuration schema.
 *
 * The MCP server's name, version, and description are read directly from
 * package.json at runtime (see src/server.ts), so they're intentionally
 * not modelled here.  Unknown keys in a user-supplied config are stripped
 * by Zod's default object behaviour, so older configs that still carry
 * those fields will continue to parse without error.
 */
const ConfigSchema = z.object({
  // Overrides the Python interpreter via the KICAD_PYTHON path (see src/index.ts).
  pythonPath: z.string().optional(),
  logLevel: z.enum(["error", "warn", "info", "debug"]).default("info"),
  // Redirects the on-disk log directory (see src/index.ts; logger defaults to a
  // platform-appropriate dir otherwise).
  logDir: z.string().optional(),
});

/**
 * Server configuration type
 */
export type Config = z.infer<typeof ConfigSchema>;

/**
 * Load configuration from file
 *
 * @param configPath Path to the configuration file (optional)
 * @returns Loaded and validated configuration
 */
export async function loadConfig(configPath?: string): Promise<Config> {
  try {
    // Determine which config file to load
    const filePath = configPath || DEFAULT_CONFIG_PATH;

    // Check if file exists
    if (!existsSync(filePath)) {
      if (configPath) {
        // The caller explicitly named this file — its absence is a real
        // misconfiguration worth flagging.
        logger.warn(`Configuration file not found: ${filePath}, using defaults`);
      } else {
        // No default config shipped is the normal case; don't cry wolf.
        logger.debug(`No default config at ${filePath}; using built-in defaults`);
      }
      return ConfigSchema.parse({});
    }

    // Read and parse configuration
    const configData = await readFile(filePath, "utf-8");
    const config = JSON.parse(configData);

    // Validate configuration
    return ConfigSchema.parse(config);
  } catch (error) {
    logger.error(`Error loading configuration: ${error}`);

    // Return default configuration
    return ConfigSchema.parse({});
  }
}
