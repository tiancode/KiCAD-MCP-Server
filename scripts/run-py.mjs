#!/usr/bin/env node
/**
 * Run the project's Python interpreter with the forwarded arguments.
 *
 * npm scripts (test:py, lint:py, format, …) must invoke pytest/black/mypy/flake8
 * from the project venv created per the setup docs. Calling `python -m pytest`
 * directly fails when the shell's `python` is some other interpreter (e.g. a
 * Homebrew Python that has no pytest) and the venv hasn't been `source`d.
 *
 * This launcher resolves the venv interpreter the same way src/python-discovery
 * does (venv → .venv → KICAD_PYTHON → system), so `npm test` works without
 * activating the venv first. CI invokes the tools directly, not via these
 * scripts, so it is unaffected.
 */
import { existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const isWindows = process.platform === "win32";
const relPython = isWindows ? ["Scripts", "python.exe"] : ["bin", "python"];

const candidates = [
  join(repoRoot, "venv", ...relPython),
  join(repoRoot, ".venv", ...relPython),
  process.env.KICAD_PYTHON,
].filter(Boolean);

const python = candidates.find((p) => existsSync(p)) ?? (isWindows ? "python" : "python3");

const result = spawnSync(python, process.argv.slice(2), {
  stdio: "inherit",
  cwd: repoRoot,
});

if (result.error) {
  console.error(`Failed to launch Python (${python}): ${result.error.message}`);
  process.exit(1);
}
process.exit(result.status ?? 1);
