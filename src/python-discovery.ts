/**
 * Python executable discovery.
 *
 * Locates the right Python interpreter to spawn the KiCAD interface script
 * with: project venvs first, then the KICAD_PYTHON override, then
 * KiCAD-bundled Python (including the Linux Flatpak shim), and finally
 * system Python. Extracted from server.ts to keep that file focused on the
 * MCP server lifecycle.
 */

import { existsSync, readdirSync } from "fs";
import { join, dirname } from "path";
import { execSync } from "child_process";
import { logger } from "./logger.js";

function getWindowsKiCadPythonCandidates(): string[] {
  const roots = [
    process.env.LOCALAPPDATA ? join(process.env.LOCALAPPDATA, "Programs", "KiCad") : undefined,
    "C:\\Program Files\\KiCad",
    "C:\\Program Files (x86)\\KiCad",
  ].filter((root): root is string => Boolean(root));

  const candidates: string[] = [];

  for (const root of roots) {
    if (!existsSync(root)) {
      continue;
    }

    try {
      const versionDirs = readdirSync(root, { withFileTypes: true })
        .filter((entry) => entry.isDirectory())
        .map((entry) => entry.name)
        .sort((a, b) => b.localeCompare(a, undefined, { numeric: true }));

      for (const versionDir of versionDirs) {
        candidates.push(join(root, versionDir, "bin", "python.exe"));
      }
    } catch (error: any) {
      logger.warn(`Failed to inspect KiCAD install directory ${root}: ${error.message}`);
    }
  }

  return [...new Set(candidates)];
}

/**
 * Locate the Flatpak KiCAD shim, or null if Flatpak KiCAD isn't installed
 * or the shim file is missing from this checkout.
 *
 * The shim is `scripts/kicad-flatpak-python.sh` — a thin bash wrapper
 * around `flatpak run --command=python3 org.kicad.KiCad`.  We point the
 * TypeScript server at the shim and let Flatpak Python serve every
 * subsequent `import pcbnew` / `import kipy` / etc; the sandbox bundles
 * the full runtime dep set this project needs.
 *
 * Detection is intentionally local (no `flatpak list` shell-out):
 * checking install-dir existence is one stat() per location and reliable
 * because Flatpak persists its app data there on every install.
 */
function findFlatpakKiCadShim(scriptPath: string): string | null {
  if (process.platform !== "linux") {
    return null;
  }

  const home = process.env.HOME;
  const installRoots = [
    "/var/lib/flatpak/app/org.kicad.KiCad",
    home ? join(home, ".local/share/flatpak/app/org.kicad.KiCad") : "",
  ].filter((p): p is string => Boolean(p));

  const installed = installRoots.find((p) => existsSync(p));
  if (!installed) {
    return null;
  }

  // scriptPath is <repo>/python/kicad_interface.py — climb one to <repo>.
  const repoRoot = dirname(dirname(scriptPath));
  const shim = join(repoRoot, "scripts", "kicad-flatpak-python.sh");

  if (!existsSync(shim)) {
    logger.warn(
      `Flatpak KiCAD detected at ${installed} but shim missing at ${shim}; ` +
        "falling back to host Python (which likely doesn't have pcbnew). " +
        "Re-run `chmod +x scripts/kicad-flatpak-python.sh` after a fresh clone.",
    );
    return null;
  }

  return shim;
}

/**
 * Find the Python executable to use.
 * Prioritizes project venvs, then explicit overrides, then KiCAD-bundled Python
 * (including the Flatpak shim on Linux) before falling back to system Python.
 */
export function findPythonExecutable(scriptPath: string): string {
  const isWindows = process.platform === "win32";
  const isMac = process.platform === "darwin";
  const isLinux = !isWindows && !isMac;

  // Get the project root (parent of the python/ directory)
  const projectRoot = dirname(dirname(scriptPath));

  // Check for virtual environment
  const venvPaths = [
    join(projectRoot, "venv", isWindows ? "Scripts" : "bin", isWindows ? "python.exe" : "python"),
    join(projectRoot, ".venv", isWindows ? "Scripts" : "bin", isWindows ? "python.exe" : "python"),
  ];

  for (const venvPath of venvPaths) {
    if (existsSync(venvPath)) {
      logger.info(`Found virtual environment Python at: ${venvPath}`);
      return venvPath;
    }
  }

  // Allow override via KICAD_PYTHON environment variable (any platform)
  if (process.env.KICAD_PYTHON) {
    logger.info(`Using KICAD_PYTHON environment variable: ${process.env.KICAD_PYTHON}`);
    return process.env.KICAD_PYTHON;
  }

  // Platform-specific KiCAD bundled Python detection
  if (isWindows) {
    // Windows: Always prefer KiCAD's bundled Python (pcbnew.pyd is compiled for it).
    for (const kicadPython of getWindowsKiCadPythonCandidates()) {
      if (existsSync(kicadPython)) {
        logger.info(`Found KiCAD bundled Python at: ${kicadPython}`);
        return kicadPython;
      }
    }
  } else if (isMac) {
    // macOS: Try KiCAD's bundled Python (check multiple versions and locations)
    const kicadPythonVersions = ["3.9", "3.10", "3.11", "3.12", "3.13"];

    // Standard KiCAD installation paths
    const kicadAppPaths = [
      "/Applications/KiCad/KiCad.app",
      "/Applications/KiCAD/KiCad.app", // Alternative capitalization
      `${process.env.HOME}/Applications/KiCad/KiCad.app`, // User Applications folder
    ];

    // Check all KiCAD app locations with all Python versions
    for (const appPath of kicadAppPaths) {
      for (const version of kicadPythonVersions) {
        const kicadPython = `${appPath}/Contents/Frameworks/Python.framework/Versions/${version}/bin/python3`;
        if (existsSync(kicadPython)) {
          logger.info(`Found KiCAD bundled Python at: ${kicadPython}`);
          return kicadPython;
        }
      }
    }

    // Fallback to Homebrew Python (if pcbnew is installed via pip)
    const homebrewPaths = [
      "/opt/homebrew/bin/python3", // Apple Silicon
      "/usr/local/bin/python3", // Intel Mac
      "/opt/homebrew/bin/python3.12",
      "/opt/homebrew/bin/python3.11",
    ];

    for (const path of homebrewPaths) {
      if (existsSync(path)) {
        logger.info(`Found Homebrew Python at: ${path} (ensure pcbnew is importable)`);
        return path;
      }
    }
  } else if (isLinux) {
    // Linux: Try KiCAD bundled Python locations first
    const linuxKicadPaths = [
      "/usr/lib/kicad/bin/python3",
      "/usr/local/lib/kicad/bin/python3",
      "/opt/kicad/bin/python3",
    ];

    for (const path of linuxKicadPaths) {
      if (existsSync(path)) {
        logger.info(`Found KiCAD bundled Python at: ${path}`);
        return path;
      }
    }

    // Flatpak KiCAD: bundled Python ships pcbnew + every runtime dep but
    // lives inside the sandbox.  scripts/kicad-flatpak-python.sh wraps
    // `flatpak run --command=python3 org.kicad.KiCad` so the TypeScript
    // server can spawn it like any other Python.  Without this branch the
    // Flatpak-only user falls through to /usr/bin/python3 — which never
    // has pcbnew — and validatePrerequisites aborts startup.
    const flatpakShim = findFlatpakKiCadShim(scriptPath);
    if (flatpakShim) {
      logger.info(`Found Flatpak KiCAD; using shim at: ${flatpakShim}`);
      return flatpakShim;
    }

    // Resolve system python3 to full path using 'which'
    try {
      const result = execSync("which python3", { encoding: "utf-8" }).trim();
      if (result && existsSync(result)) {
        logger.info(`Resolved system Python via which: ${result}`);
        return result;
      }
    } catch {
      logger.warn("Failed to resolve python3 via which command");
    }

    // Fallback to common system paths
    const systemPaths = ["/usr/bin/python3", "/bin/python3"];
    for (const path of systemPaths) {
      if (existsSync(path)) {
        logger.info(`Found system Python at: ${path}`);
        return path;
      }
    }
  }

  // Default to system Python (last resort)
  logger.info("Using system Python (no venv found)");
  return isWindows ? "python.exe" : "python3";
}
