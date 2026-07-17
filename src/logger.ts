/**
 * Logger for KiCAD MCP server
 */

import { existsSync, mkdirSync, appendFileSync } from "fs";
import { join } from "path";
import * as os from "os";

type LogLevel = "error" | "warn" | "info" | "debug";

const DEFAULT_LOG_DIR = join(os.homedir(), ".kicad-mcp", "logs");

/**
 * Logger class for KiCAD MCP server
 */
class Logger {
  private logLevel: LogLevel = "info";
  private logDir: string = DEFAULT_LOG_DIR;

  /**
   * Set the log level
   * @param level Log level to set
   */
  setLogLevel(level: LogLevel): void {
    this.logLevel = level;
  }

  /**
   * Set the log directory
   * @param dir Directory to store log files
   */
  setLogDir(dir: string): void {
    this.logDir = dir;

    if (!existsSync(this.logDir)) {
      mkdirSync(this.logDir, { recursive: true });
    }
  }

  /**
   * Log an error message
   * @param message Message to log
   */
  error(message: string): void {
    this.log("error", message);
  }

  /**
   * Log a warning message
   * @param message Message to log
   */
  warn(message: string): void {
    // Like error(), warn is emitted at every log level.
    this.log("warn", message);
  }

  /**
   * Log an info message
   * @param message Message to log
   */
  info(message: string): void {
    if (["info", "debug"].includes(this.logLevel)) {
      this.log("info", message);
    }
  }

  /**
   * Log a debug message
   * @param message Message to log
   */
  debug(message: string): void {
    if (this.logLevel === "debug") {
      this.log("debug", message);
    }
  }

  /**
   * Log a message with the specified level
   * @param level Log level
   * @param message Message to log
   */
  private log(level: LogLevel, message: string): void {
    const now = new Date();
    const pad = (n: number, w = 2) => String(n).padStart(w, "0");
    const timestamp =
      `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ` +
      `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())},${pad(now.getMilliseconds(), 3)}`;
    const formattedMessage = `[${timestamp}] [${level.toUpperCase()}] ${message}`;

    // Log to console.error (stderr) only - stdout is reserved for MCP protocol
    // All log levels go to stderr to avoid corrupting STDIO MCP transport
    console.error(formattedMessage);

    try {
      if (!existsSync(this.logDir)) {
        mkdirSync(this.logDir, { recursive: true });
      }

      const logFile = join(this.logDir, `kicad-mcp-${new Date().toISOString().split("T")[0]}.log`);
      appendFileSync(logFile, formattedMessage + "\n");
    } catch (error) {
      console.error(`Failed to write to log file: ${error}`);
    }
  }
}

export const logger = new Logger();
