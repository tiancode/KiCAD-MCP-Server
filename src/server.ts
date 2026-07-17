/**
 * KiCAD MCP Server implementation
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { spawn, execFile, ChildProcess } from "child_process";
import { existsSync } from "fs";
import { join, dirname } from "path";
import { promisify } from "util";
import { logger } from "./logger.js";
import { PACKAGE_INFO } from "./package-info.js";
import { findPythonExecutable } from "./python-discovery.js";

// Promise-returning execFile. Resolves with {stdout, stderr} on exit 0;
// rejects with an Error whose .code is either a launch errno string
// (ENOENT/EACCES/…) or the numeric non-zero exit code, and which carries
// .stdout/.stderr properties when the child produced output before exiting.
const execFileAsync = promisify(execFile);

// Commands that get the extended 10-minute timeout instead of the 30s
// default. DRC and export operations need longer timeouts for large boards.
const LONG_RUNNING_COMMANDS = new Set([
  "run_drc",
  // run_erc spawns kicad-cli sch erc and pre-refreshes lib_symbols —
  // like run_drc, unbounded by design size, so it gets the long bucket.
  "run_erc",
  "export_gerber",
  "export_pdf",
  "export_3d",
  "sync_schematic_to_board",
  "list_schematic_nets",
  "list_schematic_labels",
  "get_schematic_view",
  // Aggregators that fan out to the slow list_* handlers above (nets +
  // labels each already warrant the extended timeout on their own), so
  // the bundled call must inherit it too — otherwise a large schematic
  // blows the 30s default.
  "get_schematic_overview",
  "get_pcb_overview",
  // Symbol library queries: cold-parse of all .kicad_sym can exceed 30s
  // on Flatpak/NFS the very first time, then is fast forever after.
  "search_symbols",
  "list_symbol_libraries",
  "list_library_symbols",
  // Downloads the JLCPCB catalog / fetches a part from EasyEDA over the
  // network — can exceed 30s.
  "download_jlcpcb_database",
  "import_jlcpcb_symbol",
  "import_jlcpcb_symbols",
  // route_smart's grid A* (200k-node default budget) and auto_place's
  // ring-search placement are board-size dependent and can exceed 30s in
  // pure Python on dense boards.
  "route_smart",
  "auto_place_components",
]);

import { registerProjectTools } from "./tools/project.js";
import { registerBoardTools } from "./tools/board.js";
import { registerComponentTools } from "./tools/component.js";
import { registerRoutingTools } from "./tools/routing.js";
import { registerDesignRuleTools } from "./tools/design-rules.js";
import { registerExportTools } from "./tools/export.js";
import { registerSchematicTools } from "./tools/schematic/index.js";
import { registerLibraryTools } from "./tools/library.js";
import { registerSymbolLibraryTools } from "./tools/library-symbol.js";
import { registerJLCPCBApiTools } from "./tools/jlcpcb-api.js";
import { registerDatasheetTools } from "./tools/datasheet.js";
import { registerFootprintTools } from "./tools/footprint.js";
import { registerSymbolCreatorTools } from "./tools/symbol-creator.js";
import { registerUITools } from "./tools/ui.js";
import { registerFreeroutingTools } from "./tools/freerouting.js";
import { registerShapesTools } from "./tools/shapes.js";
import { registerTransactionTools } from "./tools/transactions.js";
import { withToolAnnotations } from "./tools/annotations.js";
import { slimToolsList } from "./tools/schema-slim.js";

import { registerProjectResources } from "./resources/project.js";
import { registerBoardResources } from "./resources/board.js";
import { registerComponentResources } from "./resources/component.js";

import { registerComponentPrompts } from "./prompts/component.js";
import { registerRoutingPrompts } from "./prompts/routing.js";
import { registerDesignPrompts } from "./prompts/design.js";
import { registerFootprintPrompts } from "./prompts/footprint.js";

/**
 * KiCAD MCP Server class
 */
export class KiCADMcpServer {
  private server: McpServer;
  private pythonProcess: ChildProcess | null = null;
  private kicadScriptPath: string;
  private stdioTransport!: StdioServerTransport;
  private requestQueue: Array<{
    request: any;
    resolve: Function;
    reject: Function;
  }> = [];
  private processingRequest = false;
  private responseBuffer: string = "";
  /** Monotonic id stamped on each request so a late response from a
   *  timed-out command can't be misattributed to the next request. */
  private nextRequestId = 1;
  private currentRequestHandler: {
    resolve: Function;
    reject: Function;
    timeoutHandle: NodeJS.Timeout;
    id: number;
  } | null = null;

  /** Resolved when Python prints {"type":"ready"} — stdin loop is live.
   *  Never rejects: startup failures surface via waitForReady's timeout. */
  private readyPromise: Promise<void>;
  private resolveReady!: () => void;
  /** Accumulates stdout until the READY marker is seen. */
  private startupBuffer: string = "";
  /** True after READY marker detected; persistent handler takes over. */
  private readyDetected: boolean = false;
  /** Cached so respawn doesn't re-run the (expensive) discovery + checks. */
  private pythonExe: string | null = null;
  /** Shared promise for concurrent respawn requests — see ensurePythonProcess. */
  private spawnInFlight: Promise<void> | null = null;

  /**
   * Constructor for the KiCAD MCP Server
   * @param kicadScriptPath Path to the Python KiCAD interface script
   * @param logLevel Log level for the server
   */
  constructor(kicadScriptPath: string, logLevel: "error" | "warn" | "info" | "debug" = "info") {
    logger.setLogLevel(logLevel);

    this.kicadScriptPath = kicadScriptPath;
    if (!existsSync(this.kicadScriptPath)) {
      throw new Error(`KiCAD interface script not found: ${this.kicadScriptPath}`);
    }

    // Initialize the MCP server using metadata from package.json so the
    // version reported to clients always matches the release.
    this.server = new McpServer(
      {
        name: PACKAGE_INFO.name,
        version: PACKAGE_INFO.version,
        description: PACKAGE_INFO.description,
      },
      {
        // Surfaced to MCP clients and used by Claude Code's tool search to
        // decide when to pull this server's (deferred) tools into context.
        // All ~160 tools load by name only until searched, so this string is
        // the primary signal for discovery — keep it under the 2 KB
        // truncation limit, key capabilities first.
        instructions: [
          "KiCAD PCB and schematic design automation. Search this server's tools whenever a task involves creating, editing, inspecting, or exporting a KiCAD project (.kicad_pro / .kicad_pcb / .kicad_sch).",
          "",
          "Capabilities by area:",
          "- Project: create / open / snapshot projects.",
          "- Board (PCB): size, outline, layers, place components, mounting holes, copper zones/pours, design rules, DRC.",
          "- Routing: tracks, vias, pad-to-pad, autoroute (freerouting), zone refill.",
          "- Schematic: create sheets; add / move / edit / delete symbols from ~10k stock libraries; wires, junctions, net labels, power symbols; connect pins; ERC; netlist; sync schematic to board.",
          "- Libraries: search/list symbol & footprint libraries, symbol & pin info, custom footprint/symbol creation, JLCPCB parts.",
          "- Export: gerbers, PDF, 3D, SVG, board preview.",
          "- State: check/launch KiCAD UI, IPC vs SWIG backend status, reconcile backends.",
          "",
          "Notes: requires KiCAD 9+. Many board ops need KiCAD open with the PCB editor — the tool returns a gate response asking the user to open it; do not auto-launch. Call get_schematic_pin_locations for exact pin coordinates before placing wires or net labels.",
        ].join("\n"),
      },
    );
    // Create the ready promise (resolved when Python sends {"type":"ready"})
    this.readyPromise = new Promise((resolve) => {
      this.resolveReady = resolve;
    });

    this.stdioTransport = new StdioServerTransport();
    logger.info("Using STDIO transport for local communication");

    this.registerAll();
  }

  /**
   * Register all tools, resources, and prompts
   */
  private registerAll(): void {
    logger.info("Registering KiCAD tools, resources, and prompts...");

    // Bind once — the previous code did `this.callKicadScript.bind(this)`
    // on every registrar call, producing 19 throwaway closures per startup.
    const cb = this.callKicadScript.bind(this);

    // Every tool is registered directly as an MCP tool — no router/registry
    // indirection. Clients see the full tool list and call tools by name.
    const toolRegistrars = [
      registerProjectTools,
      registerBoardTools,
      registerComponentTools,
      registerRoutingTools,
      registerDesignRuleTools,
      registerExportTools,
      registerSchematicTools,
      registerLibraryTools,
      registerSymbolLibraryTools,
      registerJLCPCBApiTools,
      registerDatasheetTools,
      registerFootprintTools,
      registerSymbolCreatorTools,
      registerUITools,
      registerFreeroutingTools,
      registerShapesTools,
      registerTransactionTools,
    ];
    // Register tools through an annotation-injecting proxy: the registrars
    // still call `server.tool(...)`, but the proxy forwards to
    // `registerTool` with the behaviour hints classified in annotations.ts
    // (readOnly / destructive / idempotent / openWorld). Resources and
    // prompts below use the real server — they don't take annotations.
    const annotatedServer = withToolAnnotations(this.server);
    for (const register of toolRegistrars) register(annotatedServer, cb);

    // Library resources were removed: every one dispatched a Python command
    // that never existed. Footprint/symbol library data is served by the
    // list_libraries / search_symbols / get_footprint_info tool family.
    const resourceRegistrars = [
      registerProjectResources,
      registerBoardResources,
      registerComponentResources,
    ];
    for (const register of resourceRegistrars) register(this.server, cb);

    // Prompts take only `server` — different signature, hence its own loop.
    const promptRegistrars = [
      registerComponentPrompts,
      registerRoutingPrompts,
      registerDesignPrompts,
      registerFootprintPrompts,
    ];
    for (const register of promptRegistrars) register(this.server);

    // Strip $schema / additionalProperties boilerplate from the outbound
    // tools/list payload (~3.8k tokens per client session). Must run after
    // tool registration — the SDK installs the handler lazily.
    slimToolsList(this.server);

    logger.info("All KiCAD tools, resources, and prompts registered");
  }

  /**
   * Validate prerequisites before starting the server
   */
  private async validatePrerequisites(pythonExe: string): Promise<boolean> {
    const isWindows = process.platform === "win32";
    const isLinux = process.platform !== "win32" && process.platform !== "darwin";
    const errors: string[] = [];

    // Check if Python executable exists (for absolute paths) or is executable (for commands)
    const isAbsolutePath =
      pythonExe.startsWith("/") || pythonExe.startsWith("C:") || pythonExe.startsWith("\\");
    let pythonExecutableAvailable = true;

    if (isAbsolutePath && !existsSync(pythonExe)) {
      // Absolute path that doesn't exist: bail before spawning anything.
      pythonExecutableAvailable = false;
      errors.push(`Python executable not found: ${pythonExe}`);

      if (isWindows) {
        errors.push("Windows: Install KiCAD 9.0+ from https://www.kicad.org/download/windows/");
        errors.push("Or run: .\\setup-windows.ps1 for automatic configuration");
      } else if (isLinux) {
        errors.push("Linux: Install KiCAD 9.0+ or set KICAD_PYTHON environment variable");
        errors.push("Set KICAD_PYTHON to specify a custom Python path");
      }
    }

    if (!existsSync(this.kicadScriptPath)) {
      errors.push(`KiCAD interface script not found: ${this.kicadScriptPath}`);
    }

    // Check if dist/index.js exists (if running from compiled code)
    const distPath = join(dirname(dirname(this.kicadScriptPath)), "dist", "index.js");
    if (!existsSync(distPath)) {
      errors.push("Project not built. Run: npm run build");
    }

    // Validate interpreter AND pcbnew in a single subprocess.  Previously
    // this was a `python --version` call followed by a `python -c "import
    // pcbnew; ..."` call — two spawns paying ~150-300 ms of process-start
    // cost on cold disk.  Merging them halves the startup overhead and
    // collapses two near-identical try/Promise/execFile blocks.
    //
    // Use execFile (no shell) so the path and `-c` snippet are passed as
    // discrete argv entries — no shell quoting/expansion involved.
    if (pythonExecutableAvailable && existsSync(this.kicadScriptPath)) {
      logger.info("Validating Python interpreter and pcbnew module...");

      try {
        const { stdout } = await execFileAsync(
          pythonExe,
          ["-c", "import sys, pcbnew; print('Python ' + sys.version.split()[0]); print('OK')"],
          { timeout: 5000, env: process.env },
        );

        if (stdout.includes("OK")) {
          const versionLine =
            stdout.split("\n").find((l) => l.startsWith("Python ")) ?? "(version unknown)";
          logger.info(`✓ ${versionLine.trim()} with importable pcbnew`);
        } else {
          // Process exited 0 but never printed OK — would mean print() ran
          // for the version line but pcbnew import was somehow skipped.
          // Practically unreachable but worth surfacing rather than passing.
          errors.push("pcbnew module import test produced no OK marker");
          errors.push(`Output: ${stdout.trim()}`);
        }
      } catch (error: any) {
        // Three failure shapes from execFile / promisify(execFile):
        //
        //   • Launch failure (binary unreachable): error.code is the errno
        //     STRING — "ENOENT", "EACCES", "ENOEXEC", …
        //   • Non-zero exit (process ran but Python raised): error.code is
        //     the exit NUMBER, e.g. 1.  error.stderr carries the traceback.
        //   • Timeout (binary launched, didn't finish in time): error.code
        //     is null/undefined and error.killed === true with error.signal
        //     populated (typically SIGTERM).
        //
        // The third path matters: a Flatpak cold-start or a slow
        // `import pcbnew` can hit the 5s cap, and if we mis-route it to
        // "pcbnew validation failed" the user gets useless Python traceback
        // hints when the real fix is "raise the timeout / warm the cache".
        const isLaunchErrno =
          typeof error.code === "string" &&
          ["ENOENT", "EACCES", "ENOEXEC", "EPERM"].includes(error.code);
        const isTimeout = error.killed === true && Boolean(error.signal);

        if (isLaunchErrno) {
          errors.push(`Python executable not found in PATH: ${pythonExe}`);
          errors.push(`Error: ${error.message}`);
          errors.push("Set KICAD_PYTHON environment variable to specify full path");

          if (isLinux) {
            errors.push("");
            errors.push("Linux troubleshooting:");
            errors.push("1. Check if python3 is installed: which python3");
            errors.push("2. Install KiCAD: sudo apt install kicad (Ubuntu/Debian)");
            errors.push("3. Set KICAD_PYTHON=/usr/bin/python3 in your MCP config");
          }
        } else if (isTimeout) {
          errors.push(
            `Python interpreter / pcbnew import exceeded the 5 s startup probe (${pythonExe})`,
          );
          errors.push(
            "This usually means a slow cold start (Flatpak sandbox spin-up, " +
              "network-mounted home, antivirus scan) — not a broken install. " +
              "Re-run; the second start is usually fast.  If it persists, run " +
              `manually: ${pythonExe} -c 'import pcbnew; print("OK")'`,
          );
        } else {
          // Non-zero exit — Python launched but the import failed.
          errors.push(`pcbnew validation failed: ${error.message}`);
          const stderrText = error.stderr ? String(error.stderr).trim() : "";
          if (stderrText) {
            errors.push(`Errors: ${stderrText}`);
          }

          if (isWindows) {
            errors.push("");
            errors.push("Windows troubleshooting:");
            errors.push(
              "1. Set PYTHONPATH=C:\\Program Files\\KiCad\\9.0\\lib\\python3\\dist-packages",
            );
            errors.push(
              '2. Test: "C:\\Program Files\\KiCad\\9.0\\bin\\python.exe" -c "import pcbnew"',
            );
            errors.push("3. Run: .\\setup-windows.ps1 for automatic fix");
          }
        }
      }
    }

    if (errors.length > 0) {
      logger.error("=".repeat(70));
      logger.error("STARTUP VALIDATION FAILED");
      logger.error("=".repeat(70));
      errors.forEach((err) => logger.error(err));
      logger.error("=".repeat(70));

      // Also write to stderr for Claude Desktop to capture
      process.stderr.write("\n" + "=".repeat(70) + "\n");
      process.stderr.write("KiCAD MCP Server - Startup Validation Failed\n");
      process.stderr.write("=".repeat(70) + "\n");
      errors.forEach((err) => process.stderr.write(err + "\n"));
      process.stderr.write("=".repeat(70) + "\n\n");

      return false;
    }

    return true;
  }

  /**
   * Start the MCP server and the Python KiCAD interface
   */
  async start(): Promise<void> {
    try {
      logger.info("Starting KiCAD MCP server...");

      await this.ensurePythonProcess();

      // ——— Phase 3: only now connect to MCP transport ———
      // Transport binding is one-shot (the MCP SDK doesn't support re-connect
      // and we don't need to — respawning Python doesn't drop the TS-side
      // server).  This step lives in start() and intentionally NOT in
      // ensurePythonProcess() so subsequent respawns don't try to re-bind.
      logger.info("Connecting MCP server to STDIO transport...");
      try {
        await this.server.connect(this.stdioTransport);
        logger.info("Successfully connected to STDIO transport");
      } catch (error) {
        logger.error(`Failed to connect to STDIO transport: ${error}`);
        throw error;
      }

      // Write a ready message to stderr (for debugging)
      process.stderr.write("KiCAD MCP SERVER READY\n");

      logger.info("KiCAD MCP server started and ready");
    } catch (error) {
      logger.error(`Failed to start KiCAD MCP server: ${error}`);
      throw error;
    }
  }

  /**
   * Idempotent Python-subprocess lifecycle:
   *   - alive → return immediately.
   *   - a respawn is already in flight → join that promise.
   *   - dead/never-started → spawn, wait for READY, run warm-up.
   *
   * Called from start() at server boot AND from callKicadScript() before
   * every request.  The second path is the recovery hook the user asked
   * for: when KiCad gets pkill'd and the Python child dies with it (their
   * cmdline often matches `pkill -f kicad`), the next tool call lifts a
   * fresh Python process instead of returning "Python process for KiCAD
   * scripting is not running" forever.
   */
  private async ensurePythonProcess(): Promise<void> {
    // Check spawnInFlight BEFORE pythonProcess: spawnPythonProcess assigns
    // this.pythonProcess long before READY/warm-up complete, so a caller that
    // returned on the pythonProcess check alone would dispatch into a stdin
    // loop that isn't live yet (and its response would race the warm-up).
    if (this.spawnInFlight) return this.spawnInFlight;
    if (this.pythonProcess) return;
    this.spawnInFlight = this.spawnPythonProcess().finally(() => {
      this.spawnInFlight = null;
    });
    return this.spawnInFlight;
  }

  /** Spawn + warm-up.  Always invoked through ensurePythonProcess. */
  private async spawnPythonProcess(): Promise<void> {
    // Fail any pending work attached to the previous (now-dead) Python so
    // the caller hears about it fast instead of timing out at 30 s / 10 min.
    this.drainQueueForRespawn(new Error("Python process exited; respawning"));

    // Reset per-spawn state so the new process starts from a clean slate.
    this.readyPromise = new Promise<void>((resolve) => {
      this.resolveReady = resolve;
    });
    this.startupBuffer = "";
    this.readyDetected = false;
    this.responseBuffer = "";

    logger.info(`Starting Python process with script: ${this.kicadScriptPath}`);
    if (!this.pythonExe) {
      this.pythonExe = findPythonExecutable(this.kicadScriptPath);
      logger.info(`Using Python executable: ${this.pythonExe}`);
      const isValid = await this.validatePrerequisites(this.pythonExe);
      if (!isValid) {
        throw new Error("Prerequisites validation failed. See logs above for details.");
      }
    } else {
      // Respawn — skip the discovery + prerequisites probe (the file paths
      // and bundled pcbnew haven't moved since boot).  Saves ~150-300 ms.
      logger.info(`Respawning Python with cached executable: ${this.pythonExe}`);
    }

    // Inherit the caller's environment unmodified.  PYTHONPATH detection is
    // owned by python/utils/platform_helper.py at the child's import time —
    // hard-coding a Windows fallback here leaked an invalid path into the
    // subprocess on Linux/macOS.
    this.pythonProcess = spawn(this.pythonExe, [this.kicadScriptPath], {
      stdio: ["pipe", "pipe", "pipe"],
      env: process.env,
    });

    this.pythonProcess.on("exit", (code, signal) => {
      logger.warn(`Python process exited with code ${code} and signal ${signal}`);
      this.pythonProcess = null;
      // Fail in-flight + queued requests immediately rather than waiting
      // for the per-command timeout.  The next callKicadScript will then
      // respawn the process via ensurePythonProcess.
      this.drainQueueForRespawn(
        new Error(
          `Python process exited (code=${code}, signal=${signal}). ` +
            "The next MCP tool call will respawn it automatically.",
        ),
      );
    });

    this.pythonProcess.on("error", (err) => {
      logger.error(`Python process error: ${err.message}`);
    });

    if (this.pythonProcess.stderr) {
      this.pythonProcess.stderr.on("data", (data: Buffer) => {
        logger.error(`Python stderr: ${data.toString()}`);
      });
    }

    // ——— Phase 1: stdout handler that detects the READY marker ———
    // Before Python reaches main() it may spend 55-65 s on wxApp init.
    // The stdin loop is only live after main() prints {"type":"ready"}.
    // Until then we buffer everything and scan for that exact JSON line.
    if (this.pythonProcess.stdout) {
      this.pythonProcess.stdout.on("data", (data: Buffer) => {
        if (this.readyDetected) {
          // Persistent handler (post-warm-up)
          this.handlePythonResponse(data);
        } else {
          this.startupBuffer += data.toString();
          const lines = this.startupBuffer.split("\n");
          for (let i = 0; i < lines.length; i++) {
            const line = lines[i].trim();
            if (!line) continue;
            try {
              const obj = JSON.parse(line);
              if (obj.type === "ready") {
                logger.info("Python process READY — stdin loop is live");
                this.readyDetected = true;
                // Replay any remaining buffered lines through the persistent handler
                const remaining = lines.slice(i + 1).join("\n");
                if (remaining.trim()) {
                  this.handlePythonResponse(Buffer.from(remaining));
                }
                this.resolveReady();
                return;
              }
            } catch {
              // Not valid JSON yet; keep buffering
            }
          }
        }
      });
    }

    // ——— Phase 2: wait for Python READY, then send warm-up ———
    logger.info("Waiting for Python process to be ready...");
    await this.waitForReady(120_000);
    logger.info("Python process is ready. Sending warm-up command...");
    await this.runWarmup(120_000);
    logger.info("Warm-up complete — pcbnew/wxApp initialised");
  }

  /**
   * Fail every pending request (in-flight + queued) with ``err``.
   *
   * Called when the Python process dies so callers don't wait for the
   * per-command timeout.  Also called at the head of spawnPythonProcess
   * defensively — a clean queue going into the new Python means no
   * stale state leaks across the death boundary.
   */
  private drainQueueForRespawn(err: Error): void {
    if (this.currentRequestHandler) {
      try {
        clearTimeout(this.currentRequestHandler.timeoutHandle);
      } catch {
        // Already cleared / not a real handle — best-effort cleanup.
      }
      this.currentRequestHandler.reject(err);
      this.currentRequestHandler = null;
    }
    while (this.requestQueue.length > 0) {
      const item = this.requestQueue.shift();
      if (item) item.reject(err);
    }
    this.processingRequest = false;
    this.responseBuffer = "";
  }

  /**
   * Tear down a Python worker that is wedged on a timed-out command.
   *
   * SIGTERM first; if the process hasn't exited within a short grace window
   * (a blocked pcbnew C call can ignore SIGTERM), escalate to SIGKILL. The
   * process's 'exit' handler then nulls the reference and drains the queue, so
   * the next callKicadScript respawns a clean worker. Without the SIGKILL
   * escalation a truly hung process would never exit, ensurePythonProcess
   * would keep returning the same dead worker, and the session would stay
   * wedged.
   */
  private killPythonForTimeout(): void {
    const proc = this.pythonProcess;
    if (!proc) return;
    logger.warn("Restarting the Python worker after a command timeout");
    try {
      proc.kill("SIGTERM");
    } catch (e) {
      logger.warn(`SIGTERM of timed-out worker failed: ${e}`);
    }
    setTimeout(() => {
      // exit handler nulls this.pythonProcess; if it's still the same object,
      // the process ignored SIGTERM and must be force-killed.
      if (this.pythonProcess === proc) {
        logger.warn("Worker ignored SIGTERM after timeout; sending SIGKILL");
        try {
          proc.kill("SIGKILL");
        } catch (e) {
          logger.warn(`SIGKILL of timed-out worker failed: ${e}`);
        }
      }
    }, 2000);
  }

  /**
   * Stop the MCP server and clean up resources
   */
  async stop(): Promise<void> {
    logger.info("Stopping KiCAD MCP server...");

    if (this.pythonProcess) {
      this.pythonProcess.kill();
      this.pythonProcess = null;
    }

    logger.info("KiCAD MCP server stopped");
  }

  /**
   * Wait for the Python process to print {"type":"ready"} on stdout,
   * signalling that the stdin loop is live and the process can accept
   * commands.
   */
  private async waitForReady(timeoutMs: number): Promise<void> {
    return new Promise((_resolve, reject) => {
      const timeout = setTimeout(() => {
        reject(new Error(`Python process did not send READY within ${timeoutMs / 1000} s`));
      }, timeoutMs);
      // readyPromise never rejects — the timeout above is the only error path.
      void this.readyPromise.then(() => {
        clearTimeout(timeout);
        _resolve();
      });
    });
  }

  /**
   * Send a _warmup command to the Python process to force full
   * pcbnew/wxApp initialisation.  On macOS this can take 55-65 s;
   * we use a generous timeout so the cost is paid during startup
   * rather than on the first user tool call.
   *
   * Wires into the existing request infrastructure so the persistent
   * stdout handler (already active post-READY) processes the response.
   */
  private async runWarmup(timeoutMs: number): Promise<void> {
    return new Promise<void>((resolve) => {
      if (!this.pythonProcess || !this.pythonProcess.stdin) {
        logger.warn("Python process not running — skipping warm-up");
        resolve();
        return;
      }

      const requestId = this.nextRequestId++;
      const requestStr = JSON.stringify({ command: "_warmup", params: {}, id: requestId });
      this.responseBuffer = "";

      const timeoutHandle = setTimeout(() => {
        logger.warn(
          `Warm-up timed out after ${timeoutMs / 1000} s — ` +
            "continuing without full initialisation",
        );
        this.responseBuffer = "";
        this.processingRequest = false;
        this.currentRequestHandler = null;
        resolve();
      }, timeoutMs);

      // Use the existing request infrastructure to avoid race conditions
      // with the persistent stdout handler.
      this.processingRequest = true;
      this.currentRequestHandler = {
        resolve: (result: any) => {
          clearTimeout(timeoutHandle);
          this.processingRequest = false;
          this.currentRequestHandler = null;
          if (result?.success) {
            logger.info(`Warm-up succeeded: pcbnew ${result.version} (${result.elapsed_s}s)`);
          } else {
            logger.warn(`Warm-up returned failure: ${result?.message || "unknown"} — continuing`);
          }
          resolve();
        },
        reject: (err: Error) => {
          clearTimeout(timeoutHandle);
          this.processingRequest = false;
          this.currentRequestHandler = null;
          logger.warn(`Warm-up failed: ${err.message} — continuing`);
          resolve(); // don't fail the whole server
        },
        timeoutHandle,
        id: requestId,
      };

      this.pythonProcess.stdin.write(requestStr + "\n");
    });
  }

  /**
   * Call the KiCAD scripting interface to execute commands
   *
   * @param command The command to execute
   * @param params The parameters for the command
   * @returns The result of the command execution
   */
  private async callKicadScript(command: string, params: any): Promise<any> {
    // If Python died (KiCad pkill'd, host OOM, manual kill...), bring it
    // back up before queuing.  Awaited outside the queue Promise so a
    // respawn failure surfaces as a clean rejection instead of a hung
    // promise inside the queue handler.
    if (!this.pythonProcess || this.spawnInFlight) {
      logger.info(`Python process is not running — respawning before dispatching '${command}'`);
      try {
        await this.ensurePythonProcess();
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        logger.error(`Failed to respawn Python process: ${msg}`);
        throw new Error(`Python process for KiCAD scripting could not be respawned: ${msg}`);
      }
    }

    return new Promise((resolve, reject) => {
      // Race condition guard: respawn could have raced with another exit.
      if (!this.pythonProcess) {
        reject(new Error("Python process for KiCAD scripting is not running"));
        return;
      }

      // Determine timeout based on command type
      let commandTimeout = 30000; // Default 30 seconds
      if (command === "run_simulation") {
        // The Python side runs two sequential subprocesses (kicad-cli netlist
        // export + ngspice), each bounded by the tool's timeout param
        // (default 120s). Size the Node cap above that combined budget so the
        // internal timeouts fire first instead of a worker kill at 30s.
        const perStep = Math.max(30, Number((params as any)?.timeout) || 120);
        commandTimeout = (2 * perStep + 60) * 1000;
        logger.info(
          `Using simulation timeout (${commandTimeout / 1000}s) for ngspice run @ ${perStep}s/step`,
        );
      } else if (command === "autoroute") {
        // Freerouting runs `attempts` sequential passes, each up to the tool's
        // per-attempt `timeout` (seconds), plus DSN-export + SES-import
        // overhead. The fixed 10-minute bucket below isn't enough for best-of-N,
        // so derive the Node-side cap from the tool's own params. Without this
        // the worker is killed at the 30s default mid-route (the Python
        // subprocess timeout never gets a chance to apply).
        const attempts = Math.max(1, Number((params as any)?.attempts) || 1);
        const perAttempt = Math.max(60, Number((params as any)?.timeout) || 300);
        commandTimeout = (attempts * (perAttempt + 30) + 180) * 1000;
        logger.info(
          `Using autoroute timeout (${commandTimeout / 1000}s) for ${attempts} attempt(s) ` +
            `@ ${perAttempt}s each`,
        );
      } else if (LONG_RUNNING_COMMANDS.has(command)) {
        commandTimeout = 600000; // 10 minutes for long operations
        logger.info(`Using extended timeout (${commandTimeout / 1000}s) for command: ${command}`);
      }

      this.requestQueue.push({
        request: { command, params, timeout: commandTimeout },
        resolve,
        reject,
      });

      if (!this.processingRequest) {
        this.processNextRequest();
      }
    });
  }

  /**
   * Handle incoming data from Python process stdout
   * This is a persistent handler that processes all responses
   */
  private handlePythonResponse(data: Buffer): void {
    const chunk = data.toString();
    logger.debug(`Received data chunk: ${chunk.length} bytes`);
    this.responseBuffer += chunk;

    // Try to parse complete JSON responses (may have multiple or partial)
    this.tryParseResponse();
  }

  /**
   * Try to parse a complete JSON response from the buffer.
   *
   * Responses from the Python side are single-line JSON terminated by '\n'
   * (written via _write_response).  The buffer may also contain non-JSON
   * preamble lines (e.g. C-level warnings from pcbnew that leaked to the
   * response fd before the redirect took effect).
   *
   * Strategy:
   *  1. Fast path: JSON.parse(buffer) — works for clean, complete responses
   *     (JSON.parse tolerates trailing whitespace/newlines).
   *  2. If that fails and the buffer has no '\n' yet, the response line is
   *     still arriving in chunks — keep collecting.
   *  3. If the buffer has '\n', split into lines and search from the END for
   *     a parseable JSON line.  This avoids prematurely resolving with a
   *     truncated JSON object when a large response is still chunking in.
   */
  private tryParseResponse(): void {
    if (!this.currentRequestHandler) {
      // No pending request, clear buffer if it has data (shouldn't happen)
      if (this.responseBuffer.trim()) {
        logger.warn(
          `Received data with no pending request: ${this.responseBuffer.substring(0, 100)}...`,
        );
        this.responseBuffer = "";
      }
      return;
    }

    let result: any;

    // Fast path: try to parse the response as JSON.  Handles the common
    // case of a clean, complete JSON response (possibly with trailing \n).
    try {
      result = JSON.parse(this.responseBuffer);
    } catch {
      // Direct parse failed.  Either the response is still arriving in
      // chunks, or the buffer has non-JSON preamble from pcbnew.
      //
      // The Python side writes each response as a single line of JSON
      // terminated by \n.  We use the newline as the completion signal:
      // if there is no \n in the buffer yet, the JSON line is still
      // being assembled from chunks — keep collecting.
      if (!this.responseBuffer.includes("\n")) {
        return;
      }

      // Buffer contains newline(s).  Split into lines and look for a
      // complete JSON object, searching from the END so that preamble
      // lines (which may themselves contain '{') are skipped.
      const lines = this.responseBuffer.split("\n");
      let jsonLineIndex = -1;

      for (let i = lines.length - 1; i >= 0; i--) {
        const line = lines[i].trim();
        if (line.length === 0) continue;
        if (!line.startsWith("{")) continue;

        try {
          result = JSON.parse(line);
          jsonLineIndex = i;
          break;
        } catch {
          // Looks like JSON but doesn't parse — could be an incomplete
          // final line still being chunked.  Keep collecting.
          continue;
        }
      }

      if (jsonLineIndex < 0) {
        // No parseable JSON line found yet.  Either only preamble has
        // arrived, or the JSON line is split across the last \n boundary
        // and is still incomplete.  Keep collecting.
        return;
      }

      // Log any preceding non-JSON lines as preamble
      const preambleLines = lines.slice(0, jsonLineIndex).filter((l) => l.trim().length > 0);
      if (preambleLines.length > 0) {
        logger.warn(
          `Stripped non-JSON preamble from Python response: ${preambleLines.join(" | ")}`,
        );
      }
    }

    // Correlation guard: if this response carries an id that doesn't match
    // the in-flight request's id, it is a late reply from a command we already
    // timed out on. Discard it — the current request keeps waiting for its own
    // matching response — rather than handing stale data to the wrong caller.
    const handlerId = this.currentRequestHandler.id;
    if (
      typeof handlerId === "number" &&
      result != null &&
      typeof result.id === "number" &&
      result.id !== handlerId
    ) {
      logger.warn(
        `Discarding stale response id=${result.id} while awaiting id=${handlerId} ` +
          "(late reply from a timed-out command).",
      );
      this.responseBuffer = "";
      return;
    }

    // If we get here, we have a valid JSON response
    logger.debug(`Completed KiCAD command with result: ${result.success ? "success" : "failure"}`);

    // Clear the timeout since we got a response
    if (this.currentRequestHandler.timeoutHandle) {
      clearTimeout(this.currentRequestHandler.timeoutHandle);
    }

    // Get the handler before clearing
    const handler = this.currentRequestHandler;

    this.responseBuffer = "";
    this.currentRequestHandler = null;
    this.processingRequest = false;

    // The top-level numeric id is the TS↔Python correlation id — it has done
    // its job by now and would otherwise leak into every MCP tool result.
    if (result != null && typeof result.id === "number") {
      delete result.id;
    }

    handler.resolve(result);

    setTimeout(() => this.processNextRequest(), 0);
  }

  /**
   * Process the next request in the queue
   */
  private processNextRequest(): void {
    if (this.requestQueue.length === 0 || this.processingRequest) {
      return;
    }

    this.processingRequest = true;

    const { request, resolve, reject } = this.requestQueue.shift()!;

    // Declared outside the try so the catch can disarm it: once the timeout
    // is armed, a synchronous throw (e.g. stdin.write on a just-died process)
    // must not leave it ticking — it would fire later, kill the respawned
    // worker, and clobber whatever unrelated request is in flight by then.
    let timeoutHandle: NodeJS.Timeout | undefined;

    try {
      logger.debug(`Processing KiCAD command: ${request.command}`);

      // Stamp a correlation id so a late response from a previously
      // timed-out command is recognised and discarded instead of being
      // handed to this request (see tryParseResponse).
      const requestId = this.nextRequestId++;

      const requestStr = JSON.stringify({ ...request, id: requestId });

      this.responseBuffer = "";

      const timeoutDuration = request.timeout || 30000;
      timeoutHandle = setTimeout(() => {
        logger.error(`Command timeout after ${timeoutDuration / 1000}s: ${request.command}`);
        logger.error(`Buffer contents: ${this.responseBuffer.substring(0, 200)}...`);

        // Clear state
        this.responseBuffer = "";
        this.currentRequestHandler = null;
        this.processingRequest = false;

        // Reject the promise
        reject(
          new Error(
            `Command timeout after ${timeoutDuration / 1000}s: ${request.command}. ` +
              "The KiCAD worker was restarted; if you were mid-edit, reopen the " +
              "project (open_project) before retrying.",
          ),
        );

        // The Python worker has no cancellation and reads stdin serially, so
        // it is still blocked on this command. Dispatching the next request
        // into it would pile commands up behind a possibly-wedged one, and a
        // truly hung command would dead-end the whole session. Tear the worker
        // down instead: the 'exit' handler nulls the process and drains/rejects
        // the queue, and the next callKicadScript respawns a clean process.
        // (SWIG board state rebuilds from disk on the next open_project —
        // auto-save persists every mutation; IPC keeps its board in KiCad.)
        this.killPythonForTimeout();
      }, timeoutDuration);

      this.currentRequestHandler = { resolve, reject, timeoutHandle, id: requestId };

      logger.debug(`Sending request: ${requestStr}`);
      this.pythonProcess?.stdin?.write(requestStr + "\n");
    } catch (error) {
      logger.error(`Error processing request: ${error}`);

      // Disarm the timeout armed above — this request is being rejected
      // right here, so its timer must not fire against a future request.
      if (timeoutHandle) {
        clearTimeout(timeoutHandle);
      }

      this.processingRequest = false;
      this.currentRequestHandler = null;

      setTimeout(() => this.processNextRequest(), 0);

      reject(error);
    }
  }
}
