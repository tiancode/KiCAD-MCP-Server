# Changelog

All notable changes to the KiCAD MCP Server project are documented here.

## [Unreleased]

### Tool-Surface Cleanup + Graphic-Shape Editing (2026-06-11)

- **New `list_shapes` / `delete_shape` / `edit_shape` tools (IPC).**
  Graphic shapes created by `add_segment`/`add_circle`/… can now be
  enumerated (with layer / kind / bounding-box filters), deleted, and
  edited (move, layer, stroke width, fill). `delete_shape` refuses
  multi-match filter deletes unless `all: true` (mirrors
  `delete_copper_pour`).
- **Removed redundant MCP tools** in favor of one canonical tool each
  (Python command routes remain): `export_svg` →
  `get_board_2d_view(format=svg)`, `export_schematic_svg` →
  `get_schematic_view(format=svg)`, `get_drc_violations` → `run_drc`,
  `get_backend_state` → `get_backend_info`, `export_dsn`/`import_ses`
  → `autoroute`, raw `ipc_*` passthroughs → the high-level tools that
  auto-route through IPC. Tool count: 164.
- **IPC board API is cached per connection.** A fresh `IPCBoardAPI`
  per dispatch dropped the open transaction handle — the next
  mutation was refused with "client already has a commit in
  progress" while `get_transaction_status` reported `open: false`.
- **`reconcile_backends(swig_to_ipc)` detects external disk edits.**
  A `.kicad_pcb` modified outside the MCP (text editor, git, script)
  is detected via the content signature and triggers KiCad revert +
  SWIG reload (`externalDiskChange: true`) instead of "already in
  sync".
- **Freerouting saves mark cross-backend divergence.** `autoroute` /
  `import_ses` now set `_swig_writes_landed`, so an IPC save can no
  longer clobber autoroute results with KiCad's stale memory. Eleven
  missing mutators were also added to `_BOARD_MUTATING_COMMANDS`
  (observed: `add_gnd_stitching_vias` results vanished on reconcile).
- **`add_mounting_hole` IPC path delegates to SWIG.** The inline IPC
  version read flat `x`/`y` keys (every hole landed at (0, 0)),
  opened its own commit, and ignored `padDiameter`/`plated`.
- KIIDs and layer names in IPC responses are normalized via shared
  helpers (`kiid_str`, `normalize_board_layer`) — no more
  `value: "uuid"` proto reprs or bare enum ints leaking into tool
  output.

### Dual-Backend Safety + Lifecycle Robustness

A series of fixes hardening the SWIG↔IPC interaction model and making
the Python subprocess survive transient failures. The user-visible
shape is: tools that used to silently produce wrong data, hang, or
return contradictions now either succeed or refuse with a structured
error pointing at the remediation.

- **PCB-editor gate** (`KiCADInterface._ipc_has_open_board_document`).
  IPC board ops now refuse with `success: false, needs_pcb_editor:
true` unless `kipy.get_open_documents()` lists a `.kicad_pcb`
  document. The previous process-existence proxy
  (`is_pcb_editor_running`) falsely passed when `pcbnew` was alive as
  a kiway worker with no board loaded; every call then quietly
  returned empty data. Gate response shape:
  `_pcb_editor_gate_response`.
- **Cross-backend conflict gate.** Each side carries its own copy of
  the board (SWIG memory + on-disk file vs. KiCad UI memory); writes
  from one used to silently invalidate the other. Two flags on
  `KiCADInterface` (`_ipc_writes_pending`, `_swig_writes_landed`)
  track each side's dirtiness via the IPCBackend change callback +
  SWIG auto-save signature. Cross-backend mutations refuse with
  `success: false, needs_reconcile: true, direction: "ipc_to_swig" |
"swig_to_ipc"` and a structured remediation hint.
- **`reconcile_backends` tool** flushes IPC→disk + SWIG reload
  automatically when called with `direction: "ipc_to_swig"`. The
  reverse direction returns the manual steps (kipy has no
  reload-from-disk API). `get_backend_state` exposes
  `ipcWritesPending` / `swigWritesLanded` so agents can pre-empt the
  gate without trial-and-error.
- **PCB editor auto-open after `open_project`.** After
  `_autolaunch_for_project` attaches IPC, the handler invokes a
  best-effort `run_action` candidate list
  (`kicadManager.Control.editPCB`, `editPcbnew`,
  `common.Control.openPcbnew`, `pcbnew.EditorControl.openBoardEditor`)
  so the project manager opens the board frame. Response carries
  `pcbEditorAutoOpened: <action_name>` on success. The earlier
  manual-recovery text that told users to "call `open_project` with
  the `.kicad_pcb` path" — i.e. the very call that just succeeded —
  has been removed.
- **IPC attach polling after launch.** Polls
  `_try_enable_ipc_backend(force=True)` on 0.5 s intervals up to
  `_AUTOLAUNCH_IPC_POLL_DEADLINE_S` (10 s default) so the wxApp init
  race no longer surfaces a transient `ipcAttached: false`. Response
  carries `ipcAttachAttempts` and `ipcAttachElapsedMs`; when polling
  times out the response includes `retryAfterMs: 5000` plus an
  actionable "wait then retry / open Preferences → Plugins" warning,
  not a misleading hard failure.
- **`get_backend_info` / `check_kicad_ui` force-attach.** Always
  invoke `_try_enable_ipc_backend(force=True)`, regardless of whether
  `/proc` already shows KiCad — covers the "user launched KiCad after
  MCP started" case. The SWIG branch now distinguishes "KiCad isn't
  running" (recommend `launch_kicad_ui`) from "KiCad is running but
  the IPC API server is disabled" (recommend Preferences → Plugins →
  Enable IPC).
- **`launch_kicad_ui(projectPath=…)` forwards file-open to a running
  instance** via IPC `run_action` candidates (`common.Control.openFile`
  family) and falls back to spawning `kicad <path>` so KiCad's
  `wxSingleInstanceChecker` hands the open request off to the existing
  process. Response carries `fileOpenForwarded: bool, fileOpenMethod:
"already_open" | "ipc_action" | "spawn" | "none"`. Used to be a
  no-op when KiCad was already running.
- **Python subprocess auto-respawn.** When the Python child dies
  (e.g. `pkill -9 -f kicad` catches the cmdline), the Node server
  drains the in-flight queue with a fast error and the next
  `callKicadScript` calls `ensurePythonProcess()` to spawn a fresh
  Python (cached `pythonExe`, no re-prerequisite check). No more
  "Python process for KiCAD scripting is not running" until the user
  reloads the MCP connection.
- **`KiCADProcessManager` handles `pacman -Syu kicad`**: strips the
  trailing `" (deleted)"` from `/proc/<pid>/exe` so the gate doesn't
  permanently block the editor after an in-place upgrade.

### Schematic + ERC

- **`add_schematic_component` / `move_schematic_component` snap to the
  1.27 mm KiCad schematic grid by default.** A request for `(130, 80)`
  is written as `(130.81, 80.01)`. The response carries `.position`
  (actual landing) plus a `.snap` block (requested vs. snapped) when
  coordinates moved. Opt out with `snapToGrid: false`.
- **`add_schematic_net_label`** honours a new `snapTolerance` (default
  0.05 mm): a raw `position` near a pin auto-snaps onto the pin
  endpoint so float imprecision doesn't break the electrical
  connection. Response always includes
  `connected_to_pin: {ref, pin} | null` — verified at KiCad's IU
  precision — so callers can confirm without running ERC.
- **ERC coordinate-unit bug fix.** kicad-cli 10.0.3 emits a
  schematic ERC JSON whose header claims `coordinate_units: "mm"` but
  `items[].pos` is `internal-units / 10000`. A symbol at
  (129.84, 94.92) mm came back as (1.2984, 0.9492). `handle_run_erc`
  now scales `pos.x/y` by 100 and stamps `unit: "mm"` on the
  resulting `location`.
- **`run_erc` auto-refreshes embedded `lib_symbols` before kicad-cli**
  (opt out with `autoRefreshLibSymbols: false`). Silences the "Symbol
  X doesn't match copy in library Y" warnings that every MCP-placed
  component used to emit because the injection format drifts from
  KiCad's canonical form. Refresh result surfaces under
  `response.lib_symbols_refresh`.
- **`run_erc` recommendations.** `summary.recommendations[]` surfaces
  structured `add_pwr_flag` (with extracted net names + a concrete
  `add_schematic_component` example) and `refresh_lib_symbols`
  entries. `summary.real_errors` counts ONLY non-PWR_FLAG issues so
  the headline number reflects genuine wiring problems.
- **PWR_FLAG no longer leaks as a real net.**
  `_parse_virtual_connections` registers `#FLG` pin positions with a
  sentinel instead of the literal `"PWR_FLAG"`, and
  `_build_hierarchical_pad_net_map` skips `#FLG` symbols entirely.
  `sync_schematic_to_board` no longer adds a synthetic `"PWR_FLAG"`
  net to the board's `NetInfo`.
- **`refresh_schematic_lib_symbols` tool** rewrites every embedded
  `lib_symbols` entry from the on-disk `.kicad_sym` so the post-upgrade
  `lib_symbol_mismatch` swarm can be cleared in one call. Different
  from `refresh_symbol_libraries` (which only rebuilds the MCP's
  library index).
- **`annotate_schematic` documents its no-op case.** Returns
  `noop: true` when every symbol already has a concrete reference,
  with a message explaining the tool is only needed for `?`-suffix
  placeholders.

### Discovery + Search

- **`search_symbols` tokenises multi-keyword queries with strict AND.**
  `search_symbols(query="VCC power", library="power")` used to return
  0 because the whole string was substring-matched as one token.
  Whitespace splits into tokens; any token that finds no match zeros
  the candidate. Library nickname is now a matchable field (low
  weight) so `"VCC power"` naturally resolves to `power:VCC`.
- **`search_footprints` ranks exact / shorter matches first.**
  Collects all matches before applying `limit`, then orders by band
  (exact > prefix > substring) and name length within band.
  `LED_D5.0mm` no longer gets buried under `LED_D5.0mm-3` etc. Regex
  metacharacters in the input are escaped so `.` matches a literal
  dot.
- **`get_symbol_info` inlines pin info.** Response carries `pins[]`
  (number / name / x / y / angle / length / type in the symbol's
  local coordinate frame) plus `pin_count` and `pin_bounding_box`, so
  callers can plan placement coordinates without round-tripping
  through `get_schematic_pin_locations`.

### Routing + Geometry

- **`route_pad_to_pad` refuses obstacle crossings by default.** When
  the straight line (or either leg of a via path) crosses a third
  pad, the call returns
  `success: false, hasObstacles: true, obstacleCount, obstaclesCrossed`
  - a `hint` pointing at `force: true` for the legacy
    "insert anyway, return warnings" behaviour. Tool description
    rewritten to lead with "Insert ONE STRAIGHT trace segment" + "NOT
    an autorouter".
- **`refill_zones` refuses the SWIG path by default** because
  `pcbnew.ZONE_FILLER.Fill()` has a documented segfault / silently
  wrong-fill history when invoked outside KiCad's own process. Pass
  `force: true` to opt into the subprocess-isolated fill for
  headless flows; the response then carries a `warnings` entry
  flagging the uncertainty.
- **`add_copper_pour` / `add_zone` IPC handler** reads the canonical
  `outline` parameter name (previously only read `points`), falls
  back to the board's Edge.Cuts bounding box when omitted, and
  converts top-level / per-point `unit` (`mm` / `inch` / `mil`) to
  millimetres before forwarding to kipy.
- **`get_component_list` / `get_component_properties` never mix IPC
  with SWIG.** Old code patched a missing `boundingBox` from the SWIG
  board (which held pre-IPC-mutation positions), so a freshly moved
  component came back with new `position` and stale `boundingBox`.
  Both handlers now return a pure IPC view; missing `boundingBox`
  stays `null` rather than being silently wrong. `layer` is
  normalised through `.name` / `BL_` prefix strip so the output is
  `"F.Cu"` even on kipy builds where `str(layer)` returns the raw
  int.
- **`get_pad_position` accepts the schema-documented `pad`** parameter,
  not just legacy `padName` / `padNumber`.

### Freerouting

- **`check_freerouting` install hint.** Response gains
  `install.steps[]` listing the GitHub release URL, per-platform
  shell commands, and the `FREEROUTING_JAR` override when components
  are missing. TS adapter prints a copy-pasteable block; when
  everything's in place the install section is omitted.
- **Versioned JAR filenames auto-discovered.** GitHub releases ship
  `freerouting-X.Y.Z.jar`, not the bare `freerouting.jar` the default
  path expects. `_resolve_freerouting_jar` globs for
  `freerouting-*.jar` in the same dir and picks the newest match.
  Both `check_freerouting` and `autoroute` use the resolver, so users
  don't have to rename release artifacts. The response gains
  `freerouting.requested_path` when auto-discovery landed on a
  different filename.

### Per-Response Banner Cleanup

The dispatcher used to inject `_backend` / `_realtime` /
`_recommendation` into every successful response. The recommendation
text repeated on every SWIG call AND incorrectly fired on schematic /
file-only tools that can't use IPC. All three fields are gone from
the dispatcher — backend state is queryable on demand via
`get_backend_state` / `get_backend_info`.

### Server Internals — Architecture Refactor

- **`python/kicad_interface.py` reduced from 6 668 → 2 797 lines (−58 %)** by
  splitting per-tool handlers into a `python/handlers/` package. 81 of 81
  inline `_handle_*` methods (~3 600 lines of mechanical logic) now live in
  14 per-domain modules: `ui.py`, `project.py`, `footprint.py`,
  `symbol_creator.py`, `jlcpcb.py`, `datasheet.py`, `ipc.py`, `routing.py`,
  `schematic_component.py`, `schematic_wire.py`, `schematic_query.py`,
  `schematic_io.py`, `schematic_view.py`, `board.py`. The 80 trampoline
  methods on `KiCADInterface` were replaced by a single `__getattr__`
  dispatcher driven by a `_HANDLER_MAP` class attribute, so tests calling
  `iface._handle_<command>(params)` keep working unchanged.

- **MCP server identity now comes from `package.json`** at module load
  instead of being hardcoded as `"1.0.0"`. Clients negotiating capabilities
  now see the real release version (`2.2.3`).

- **`express` dropped from dependencies** (it was imported but never used);
  removing it cleared 6 transitive npm-audit advisories (`hono`, `qs`,
  `ip-address`, `express-rate-limit`, `fast-uri`). `npm audit --high`
  now reports 0 vulnerabilities.

### Performance

- **30 – 120 s startup latency on a real KiCAD install fixed.** Eager
  `SymbolLibraryManager._warm_cache()` (which parsed all 200 + `.kicad_sym`
  files on every startup) is now **opt-in via
  `KICAD_MCP_EAGER_SYMBOL_CACHE=1`**. The default path is lazy — libraries
  are parsed on first `list_symbols(nickname)` call, which is bounded by
  what the user actually searches.

- **Persistent on-disk symbol cache** at
  `~/.kicad-mcp/cache/symbol_libraries.pickle`. `list_symbols()` validates
  each cached entry against the live `.kicad_sym` mtime so libraries
  edited by the KiCAD UI / PCM update silently re-parse; everything else
  hits the disk cache in roughly 100 ms instead of re-parsing. An atexit
  hook flushes the cache only when it was modified (PCB-only sessions
  don't write anything).

### Bug Fixes

- **MCP-protocol-level gaps fixed.** End-to-end testing against a real
  KiCAD 10 + a JSON-RPC client surfaced four tools the docs/registry
  referenced but the TypeScript layer never registered:
  `execute_tool` (router pass-through), `get_backend_info`, and the seven
  `ipc_*` tools (`ipc_add_track`, `ipc_add_via`, `ipc_add_text`,
  `ipc_list_components`, `ipc_get_tracks`, `ipc_get_vias`,
  `ipc_save_board`). Tool count grew from 142 → **151 MCP tools**.

- **`get_backend_state` now reports the correct project path under IPC.**
  Previously `_current_board_path` only inspected the SWIG board, so an
  IPC-only session returned `loadedBoard: false` even with a board open
  in the UI. The fix stitches `document.project.path` +
  `document.board_filename` rather than relying on the bare filename
  resolved against the MCP server's cwd.

- **Auto-detect IPC socket in sandboxed installs.** The Flathub Flatpak
  build of KiCAD puts the IPC socket under
  `~/.var/app/org.kicad.KiCad/cache/tmp/kicad/api.sock` (not the
  documented `/tmp/kicad/api.sock`); macOS sandbox installs use
  `~/Library/Caches/kicad/api.sock`. Both are now in the auto-detect
  list, so most installs no longer need `KICAD_API_SOCKET` set manually.

- **KiCAD 10 version detection.** `get_backend_info` returned `"unknown"`
  when the connected KiCAD was newer than the installed kipy
  (`FutureVersionError`). Now uses `KiCad.get_version().full_version`
  (the modern kipy API) and falls back to the older `check_version()` /
  `get_api_version()` path only when the new one isn't present. KiCAD
  10.0.3 now reports correctly.

- **Library tables in Flatpak / macOS sandbox installs.**
  `fp-lib-table`, `sym-lib-table`, and the `KICAD10_FOOTPRINT_DIR` /
  `KICAD10_SYMBOL_DIR` references now resolve correctly across the
  native + sandbox layouts. Verified end-to-end on a KiCAD 10.0.3 Flathub
  install: stock `Resistor_SMD:R_0603_1608Metric` placement works without
  manually exporting any env vars.

- **CI failure masking removed.** `.github/workflows/ci.yml` used to
  trail every step with `|| echo "... not configured yet"`, so pytest
  exit code 5 (no tests collected), eslint failures, mypy errors all
  turned the run green. CI also ran `pytest python/` while real tests
  live in `tests/` — discovery found 0 tests there. Both fixed; the
  19 pre-existing pollution failures that this used to hide were
  root-caused (unconditional `import pcbnew` instead of conditional)
  and the suite is now 856 / 0 / 11 (passed / failed / skipped).

- **Python `LOG_LEVEL` is now honored.** Previously hardcoded to
  `DEBUG` regardless of env, flooding `~/.kicad-mcp/logs/` and (on the
  no-write fallback) stderr — which the TS layer re-logs as ERROR. The
  same `LOG_LEVEL` env var the TS server already respects now applies
  to the Python subprocess too.

- **`SWIGBoardAPI.get_size()` implemented.** Used to raise
  `NotImplementedError` — now delegates to `BoardCommands.get_board_info`
  the same way the rest of the SWIG wrapper methods do.

### Tooling / DX

- **`execute_tool` works.** Every router tool's response told you to
  "use `execute_tool` to run any of these tools", but `execute_tool`
  itself was never registered — calling it threw `MCP error -32602:
Tool not found`. Registered.

- **`scripts/swig_smoke_test.py`** added. Runs the full
  `create_project` → board outline → mounting holes → place_component →
  routing → save chain in-process against a real `pcbnew`, prints a
  per-step status. Complements pytest (which uses a MagicMock `pcbnew`)
  by catching regressions that only surface against the real C++
  bindings — SWIG dehydration, library URI resolution, layer enum drift,
  …

- **`exec()` → `execFile()` for Python prerequisite checks** in
  `src/server.ts`. Avoids shell-quoting edge cases on paths with spaces
  or `$` while keeping the same error reporting.

- **Repo-root scripts moved into `scripts/`.** `download_jlcpcb.py` and
  `test-router.js` lived at the repo root; both are now under
  `scripts/` next to the other one-off helpers.

- **MAX_PARTS probe bumped 30 → 60** in
  `scripts/download_jlcpcb.py`. JLCPCB's pre-built cache has grown past
  30 split volumes since the old limit was set, so downloads were
  silently truncated.

- **`exception Exception` audit across `python/commands/`.** ~40 broad
  `except Exception` clauses in `pin_locator.py`, `freerouting.py`,
  `component.py`, `wire_manager.py`, `connection_schematic.py`,
  `library*.py` were narrowed to specific exception types where the
  catch was masking real bugs; the verbose
  `import traceback; logger.error(traceback.format_exc())` pattern was
  replaced with `logger.exception(...)` in API-boundary catches.

### Docs / Repo Hygiene

- **`CONTRIBUTING.md`** project-structure block + tool count brought
  in line with reality (122 → **151 tools across 16 categories**;
  removed dead references to `python/integrations/`, `tests/unit/`,
  `tests/integration/`, missing `docs/REBUILD_PLAN.md`).
- **`README.md`** broken link to missing `docs/WINDOWS_SETUP.md`
  replaced with a pointer to `setup-windows.ps1` +
  `docs/WINDOWS_TROUBLESHOOTING.md`.
- **`tests/conftest.py`** mock `pcbnew.__file__` was advertising
  `cpython-313`; tightened to `cpython-39` (the project's lowest
  supported Python).
- **Three `mil` unit enums** in `python/schemas/tool_schemas.py`
  were missing — `mounting_hole.position`, plus two more — now
  consistent with the rest of the schema.

### Bug Fixes

- **`rotate_component` now treats `angle` as an absolute target rotation**,
  matching its schema description. Previously the IPC backend added the
  supplied angle to the current rotation, so two consecutive
  `rotate_component(angle=90)` calls would rotate the part to 180° instead
  of leaving it at 90°. Workflows that relied on the additive behavior will
  need to be updated.

- **Project-scope `sym-lib-table` is now visible to symbol-discovery tools**:
  `search_symbols`, `list_symbol_libraries`, `list_library_symbols`, and
  `get_symbol_info` previously only consulted the global `sym-lib-table`. A
  library registered with project scope (i.e. an entry in
  `<project>/sym-lib-table`) was therefore invisible — even right after
  `open_project` succeeded — making `add_schematic_component` the only tool
  that could see it. Two changes:
  1. `open_project` and `create_project` now rebuild the
     `SymbolLibraryManager` against the project directory so subsequent
     search/list/info calls see project-scope libraries automatically.
  2. The four discovery tools also accept an optional `projectPath`
     parameter (a project directory, `.kicad_pro`, `.kicad_pcb`, or
     `.kicad_sch` path) for stateless callers, so project libraries can be
     resolved without first calling `open_project`.

- **IPC backend runtime reconnect**: MCP no longer stays on SWIG for the
  entire process when it starts before KiCAD. IPC-capable board tools now retry
  the IPC connection when KiCAD is running, refresh the live board API when a
  board becomes available, and report `_backend: "ipc"` when they actually use
  the IPC path. `check_kicad_ui`, `launch_kicad_ui`, and `get_backend_info`
  now include live backend status instead of only reflecting startup state.

- **Windows KiCAD Python discovery**: Windows startup now scans per-user KiCAD
  installs under `%LOCALAPPDATA%\Programs\KiCad` in addition to machine-wide
  installs under `C:\Program Files\KiCad` and `C:\Program Files (x86)\KiCad`,
  so user-scope installs no longer require a manual `KICAD_PYTHON` override.

- **IPC board size on KiCAD 10**: `get_board_info` now handles KiCAD 10 IPC
  `Box2` objects that expose `pos` / `size` instead of `min` / `max`, avoiding
  a zero-size board result with an attribute error.

- **Schematic symbol lookup**: `get_schematic_component`,
  `edit_schematic_component`, `set_schematic_component_property`,
  `remove_schematic_component_property`, and `delete_schematic_component`
  no longer fail with `Component '<ref>' not found in schematic` when the
  placed symbol uses KiCad's rescued / locally-customised serialisation
  form `(symbol (lib_name "...") (lib_id "...") ...)`. The block-matching
  regex now accepts any opening paren after `(symbol`, and the
  parent-position lookup uses the first `(at ...)` inside the symbol
  block, so newly-added properties anchor to the symbol origin instead of
  silently falling back to `(0, 0)`. Added 7 regression tests reproducing
  the failure on a real-world user schematic.

### New MCP Tools

- `add_gnd_stitching_vias` — Drop GND stitching vias across the board with
  collision checking against every non-GND segment, via, and pad on every
  copper layer. PTH vias penetrate the full stackup, so an F.Cu-only check
  (the most common shortcut) silently creates shorts on inner / B.Cu
  copper — this implementation explicitly walks all layers.

  Combines three placement strategies, freely composable:
  - `grid` — regular grid across the board interior.
  - `around_refs` — densify around named footprints (good for tucking
    extra ground under MCUs, switching regulators, or RF parts).
  - `in_zones` — restrict candidates to points inside the filled
    polygons of GND copper zones, so each new via actually stitches
    real ground polygons together rather than floating on silkscreen.

  Also supports per-via geometry control (`viaSize`, `viaDrill`,
  `clearance`, `edgeMargin`), an `maxVias` cap for incremental work,
  auto-detection of the GND net (tries `GND` / `GROUND` / `VSS` /
  `/GND`), and a `dryRun` mode that returns the placements that
  _would_ be made without modifying the board — useful for previewing
  before committing.

  Returns `{ placed: [{x, y, unit}, ...], summary: {placed_count,
candidates_evaluated, skipped_by_zone_membership,
skipped_by_collision, ...} }`.

  Approach ported from
  [morningfire-pcb-automation](https://github.com/NiNjA-CodE/morningfire-pcb-automation)
  (`scripts/ground/add_gnd_vias.py`). The original parses the PCB
  text with regex and writes new vias by string concatenation; this
  port reads obstacles via the pcbnew API so it handles rotated
  footprints correctly, integrates with the in-memory board (two
  sequential calls see each other's placements), picks up net codes
  from the live board, and adds the `in_zones` strategy + the
  `maxVias` cap + dry-run.

- `check_courtyard_overlaps` — Detect courtyard overlaps between footprints
  and (optionally) flag courtyards that extend past the board outline.
  Returns overlap pairs with intersection extents (mm), per-component
  boundary violations, and a placement summary. Accepts a `positions` dict
  of hypothetical placements (with optional rotation) so an AI agent can
  validate a proposed `move_component` / `place_component` before
  committing it — closing the feedback loop that previously required
  writing the move, running DRC, parsing violations, and reverting.

  Approach ported from
  [morningfire-pcb-automation](https://github.com/NiNjA-CodE/morningfire-pcb-automation)
  (`scripts/placement/check_overlaps.py`). The original uses a static
  per-footprint-type courtyard lookup table; this implementation reads
  the real courtyard polygons (or pad bounding box fallback) from the
  loaded board for accuracy on custom and rotated footprints, and adds
  virtual placement + clearance margin support.

- `query_zones` — Query copper zones (filled pours) on the board with optional
  filters by net, layer, or bounding box. Returns one entry per zone with its
  net, layers, priority, fill state, min thickness, bounding box, and filled
  area. Complements `query_traces`, which only reports tracks/vias and silently
  omits power-plane and GND pours — making layer-usage audits incomplete on any
  board that uses copper zones.

- `set_schematic_component_property` — Add or update a single custom property
  (BOM / sourcing field) on a placed schematic symbol. Convenience wrapper
  around `edit_schematic_component` for the common case of attaching one MPN /
  Manufacturer / DigiKey_PN / LCSC / JLCPCB_PN / Voltage / Tolerance /
  Dielectric value at a time. Newly created properties default to hidden so
  they do not clutter the schematic canvas.

- `remove_schematic_component_property` — Delete a custom property from a
  placed schematic symbol. The four built-in fields (Reference, Value,
  Footprint, Datasheet) are protected and cannot be removed; clear them by
  setting their value to `""` via `edit_schematic_component` instead.

### Tool Enhancements

- `autoroute`: best-of-N support. New optional parameters `attempts`,
  `targetNets`, and `passSchedule`. When `attempts > 1`, Freerouting is
  invoked multiple times with varied `--max-passes` values, each result
  is scored by `(nets_routed * 1000) + segments` plus a 50,000-point
  bonus when every `targetNets` entry is routed, and the winning SES is
  imported into the board. Single-attempt behaviour is unchanged when
  `attempts` is omitted, so existing callers don't need updates.

  Motivation: on dense boards a single Freerouting run routinely leaves
  1–7 nets unrouted. Cycling through a few `-mp` values typically drives
  the unrouted count to zero. Empirically, 3 attempts is usually enough
  for 4-layer designs; 5–8 for stubborn cases.

  The scoring approach and the default `passSchedule` are ported from
  [morningfire-pcb-automation](https://github.com/NiNjA-CodE/morningfire-pcb-automation)
  (`scripts/routing/freeroute_runner.py`). The MCP version adds:
  cleaner per-attempt result reporting, automatic single-thread
  optimisation (`-mt 1`) during scored attempts so the multi-threaded
  optimiser's known clearance-violation bug doesn't distort the
  comparison, and graceful degradation when one attempt errors out
  (the run continues and the best of the remainder wins).

- `edit_schematic_component`: extended with two new optional parameters that
  promote arbitrary custom properties to first-class citizens:
  - **`properties`** — map of property name to either a string value or a full
    spec object `{ value, x?, y?, angle?, hide?, fontSize? }`. Adds the
    property if it does not yet exist on the symbol, otherwise updates the
    existing value (and optionally its label position / visibility). Lets a
    single tool call attach an entire BOM / sourcing payload to a component:
    `properties: { MPN: "RC0603FR-0710KL", Manufacturer: "Yageo", Tolerance: "1%" }`.
  - **`removeProperties`** — list of custom property names to delete in the
    same call.
  - String values written through any of the property paths are now properly
    backslash-escaped so descriptions containing `"` or `\` no longer
    corrupt the .kicad_sch file.

- `get_schematic_component`: clarified description — it already returns every
  field on the symbol (built-in + custom). The tool description now spells
  this out explicitly so agents know they can use it to inspect MPN,
  Manufacturer, Distributor PN and other BOM fields without a separate call.

- `query_traces`: added to the IPC-capable board command path so trace reads
  can use live KiCAD board data when IPC is connected.

### New MCP Prompt

- `component_sourcing_properties` — Guides the LLM through attaching BOM and
  sourcing metadata (MPN, Manufacturer, distributor part numbers, parametric
  fields like Voltage / Tolerance / Dielectric) to schematic components. Lists
  the conventional property names recognised by downstream BOM tooling and the
  recommended call sequence (`list_schematic_components` →
  `get_schematic_component` → `set_schematic_component_property` /
  `edit_schematic_component`).

### Tests

- `tests/test_schematic_component_properties.py`: 32 new tests covering custom
  property add / update / remove (single + batched), full spec dicts, position
  defaults, `(hide yes)` defaulting, protected built-in field rejection,
  no-op removal, special-character escaping, UUID preservation, and the two
  new convenience tools.

- `tests/test_backend_metadata.py`: regression coverage for backend metadata,
  runtime IPC reconnect after KiCAD starts, IPC-backed `query_traces`, and
  KiCAD 10 IPC `Box2` board-size compatibility.

### Removed

- `add_schematic_junction` MCP tool has been removed. Junctions are now
  inserted and removed automatically via `WireManager.sync_junctions` whenever
  wires are added, deleted, or moved.
- Junction placement is pin-aware: `sync_junctions` consults component pin
  positions so that T-junctions at component pins are correctly recognised.

---

## [2.2.3] - 2026-03-11

### Merged: PR #57 (Kletternaut/demo/rpiCSI-videotest → main)

This release incorporates 28 commits developed and live-tested during a full
Raspberry Pi CSI adapter PCB design session. All tools listed below were validated
end-to-end using Claude Desktop + KiCAD 9 on Windows.

### New MCP Tools

- `connect_passthrough` — Schematic-only tool that wires all pins of one connector
  directly to the matching pins of another (e.g. J1 pin N → J2 pin N). Creates nets
  named with a configurable prefix (`netPrefix`). Designed for FFC/ribbon cable
  passthrough adapters. **Schematic only — do not call for PCB routing.**

- `sync_schematic_to_board` — Imports all net/pad assignments from the schematic
  into the open PCB file. Required after `connect_passthrough` before routing can
  start. Returns `pads_assigned` count for verification.

- `snapshot_project` — Saves a named checkpoint of the entire project folder into a
  `snapshots/` subdirectory inside the project. Allows resuming from a known-good
  state without redoing earlier steps. Accepts `step`, `label`, and optional `prompt`
  parameters.

- `run_erc` — Runs KiCAD's Electrical Rules Check on the schematic and returns
  violations as structured JSON.

- `import_svg_logo` — Converts an SVG file to PCB silkscreen polygons and places
  them on a specified layer.

### Bug Fixes

- `route_pad_to_pad`: **Critical fix for B.Cu footprints in KiCAD 9.** `pad.GetLayerName()`
  always returned `F.Cu` for SMD pads on flipped footprints (KiCAD 9 SWIG bug).
  Fix: use `footprint.GetLayer()` instead, which correctly reflects the placed layer
  after `Flip()`. Without this fix, no vias were inserted for back-to-back connectors.

- `route_pad_to_pad`: Via was placed at the geometric midpoint between the two pads.
  For back-to-back mirrored connectors (J1 F.Cu / J2 B.Cu) this caused all 15 vias
  to stack at the same X coordinate (board center). Fix: via is now placed at the
  X coordinate of the start pad (`via_x = start_pos.x`), producing 15 parallel
  vertical traces.

- `place_component` (B.Cu footprints): `Flip()` was called before `board.Add()`,
  causing KiCAD 9 to hang for ~30 seconds. Fix: `board.Add()` first, then `Flip()`.

- `add_board_outline`: Three separate bugs fixed — incorrect cornerRadius fallback,
  wrong top-left origin default, and broken arc delegation for IPC rounded rectangles.

- `snapshot_project`: Snapshots were saved one level above the project directory,
  cluttering the parent folder. Fix: snapshots now go into `<project>/snapshots/`.

- MCP server log timestamp was always UTC/ISO. Fix: now uses local system time.

- `search_tools` (router pattern): direct tools like `snapshot_project` were invisible
  to the router. Fix: direct tool names added to the router's known-tool list.

### Developer Mode (`KICAD_MCP_DEV=1`)

Set the environment variable `KICAD_MCP_DEV=1` in your Claude Desktop config to
enable developer features:

```json
"env": {
  "KICAD_MCP_DEV": "1"
}
```

**What it does:**

- `export_gerber` automatically copies the current MCP session log into the project's
  `logs/` subdirectory as `mcp_log_<timestamp>.txt`.
- `snapshot_project` copies the MCP session log into `logs/` at every checkpoint as
  `mcp_log_step<N>_<timestamp>.txt`.
- If a `prompt` parameter is passed to `snapshot_project`, it is saved as
  `PROMPT_step<N>_<timestamp>.md` alongside the log.

**Purpose:** Makes it easy to include the full tool call history when filing a bug
report or GitHub issue — just attach the log file from the project's `logs/` folder.

> ⚠️ **Privacy warning:** The MCP session log contains the **complete conversation
> history** between Claude and the MCP server, including all tool parameters and
> responses. When sharing a project directory (e.g. as a ZIP attachment in a GitHub
> issue), **review or delete the `logs/` folder first** to avoid accidentally
> disclosing sensitive file paths, component names, or design details.

### Snapshot Logging (always active)

Regardless of dev mode, `snapshot_project` now always saves a copy of the current
MCP session log into `<project>/logs/` at each checkpoint. This means every project
automatically retains a traceable record of which tools were called and in what order.

> ⚠️ **Same privacy note applies:** the `logs/` directory inside your project folder
> contains tool call history. Do not share it publicly without reviewing its contents.

---

## [2.2.2-alpha] - 2026-03-01

### New MCP Tools

- `route_pad_to_pad` – Convenience wrapper around `route_trace` that looks up pad positions
  automatically. Accepts `fromRef`/`fromPad`/`toRef`/`toPad` instead of raw XY coordinates.
  Auto-detects net from pad assignment (overridable via `net` param). Saves ~2 tool calls per
  connection (~64 calls for a full TMC2209 board compared to the 3-step get_pad_position flow).
  Live tested: ESP32 ↔ TMC2209 STEP/DIR traces routed without prior coordinate lookup. ✅

- `copy_routing_pattern` – Now registered as MCP tool in TypeScript layer (`routing.ts`).
  Was previously implemented in Python but missing from the MCP tool registry.
  Parameters: `sourceRefs`, `targetRefs`, `includeVias?`, `traceWidth?`.

### Bug Fixes

- `add_schematic_component` / `DynamicSymbolLoader`: ignored project-local `sym-lib-table`.
  `find_library_file()` only searched global KiCAD install directories, causing "library not
  found" errors for any symbol in a project-local `.kicad_sym` file. Fix: added `project_path`
  parameter; reads project `sym-lib-table` first via new `_resolve_library_from_table()` helper
  before falling back to global dirs. `project_path` is auto-derived from the schematic path.

- `place_component`: ignored project-local `fp-lib-table`. `FootprintLibraryManager` was
  initialised once at server start without a project path, so self-created `.kicad_mod`
  footprints were never found. Fix: new `boardPath` parameter in TypeScript + Python;
  `_handle_place_component` wrapper recreates `FootprintLibraryManager(project_path=…)` whenever
  the active project changes (cached to avoid redundant recreation).

- `copy_routing_pattern`: copied 0 traces when pads had no net assignments. The filter
  `track.GetNetname() in source_nets` always returned empty when pads were placed without net
  assignment. Fix: geometric fallback using bounding box of source footprint pads ±5mm
  tolerance. Response includes `filterMethod` field indicating which mode was used
  (`"net-based"` or `"geometric (pads have no nets)"`).

- `template_with_symbols.kicad_sch`, `template_with_symbols_expanded.kicad_sch`: restored
  format version `20250114` (KiCAD 9) after upstream commit `2b38796` accidentally downgraded
  both files to `20240101`. KiCAD 9 rejects schematics with outdated version numbers.

- **CRITICAL: `template_with_symbols_expanded.kicad_sch`**: removed 7 invalid `;;` comment
  lines introduced by upstream commit `b98c94b`. KiCAD's S-expression parser does not support
  any comment syntax — it expects every non-empty, non-whitespace line to start with `(`.
  The comments (`;; PASSIVES`, `;; SEMICONDUCTORS`, `;; INTEGRATED CIRCUITS`, `;; CONNECTORS`,
  `;; POWER/REGULATORS`, `;; MISC`, `;; TEMPLATE INSTANCES (...)`) caused KiCAD 9 to reject
  every schematic created from this template with a hard parse error:

  > `Expecting '(' in <file>.kicad_sch, line 8, offset 5`
  > **Action required for existing projects:** delete every line beginning with `;;` from any
  > `.kicad_sch` file created between upstream commit `b98c94b` and this fix.

- `add_schematic_component` / `inject_symbol_into_schematic`: symbol definition in
  `lib_symbols` was never refreshed after editing via `create_symbol` / `edit_symbol`.
  If the symbol was already present in the schematic's embedded `lib_symbols` section,
  the function returned immediately — `delete + re-add` still pulled in the stale cached
  definition. Fix: always read the current definition from the `.kicad_sym` file; if a
  stale entry exists in `lib_symbols`, remove it first, then inject the fresh one.
  Verified live. ✅

- `template_with_symbols_expanded.kicad_sch`: removed 13 legacy `_TEMPLATE_*` offscreen
  instances (`_TEMPLATE_R`, `_TEMPLATE_C`, `_TEMPLATE_U`, etc.) that were placed at
  `x=-100` as clone-sources for the old `ComponentManager` approach. `DynamicSymbolLoader`
  (the current implementation) injects symbols directly and never needs these placeholders.
  They appeared as dangling reference designators in KiCAD's component navigator and in
  the schematic canvas when zoomed far out.

### Maintenance

- `.gitignore`: added `*.kicad_pcb.bak`, `*.kicad_pro.bak` alongside existing `-bak` variants;
  consolidated personal/local files under `myContribution/`.

---

## [2.2.1-alpha] - 2026-02-28

### New MCP Tools

- `edit_schematic_component` – Update properties of a placed symbol in-place (footprint,
  value, reference rename). More efficient than delete + re-add: preserves position and UUID.

### Bug Fixes

- `add_schematic_component`: `footprint` parameter was accepted but silently ignored – the
  value was never passed through to `DynamicSymbolLoader.add_component()` /
  `create_component_instance()`. All newly placed symbols always had an empty Footprint
  field. Fix: added `footprint: str = ""` to both functions and threaded it through every
  call site including the TypeScript tool schema.

- `delete_schematic_component`: only deleted the first matching instance when duplicate
  references existed (e.g. after an aborted add attempt). Root cause: loop used `break`
  after the first match. Fix: collect all matching blocks first, then delete them all back-
  to-front (to preserve line indices). Response now includes `deleted_count`.

- `templates/*.kicad_sch`, `project.py`, `schematic.py`: Update KiCAD schematic format
  version from `20230121` (KiCAD 7) to `20250114` (KiCAD 9). The MCP server targets
  KiCAD 9 exclusively (`pcbnew.pyd` compiled for KiCAD 9.0, Python 3.11.5) – generating
  files in an outdated format caused a spurious "This file was created with an older
  KiCAD version" warning on every newly created schematic.

- `template_with_symbols_expanded.kicad_sch`: Remove 13 corrupt `_TEMPLATE_*` placed-symbol
  blocks with `(lib_id -100)` – an integer caused by old sexpdata serializer (same bug
  PR #40 fixed for the add path). KiCAD crashed with a null-pointer when selecting these
  symbols. They appeared as grey `_TEMPLATE_R?`, `_TEMPLATE_U_REG?` etc. labels far
  outside the sheet boundary (~5000mm off-sheet).

  **Discovered via:** live testing on a real JLCPCB/KiCAD 9 project.
  **Affected users:** schematics created from this template before this fix contain the
  same corrupt blocks – remove all `(symbol (lib_id -100) ...)` blocks whose Reference
  starts with `_TEMPLATE_`.

---

---

## [2.2.0-alpha] - 2026-02-27

### New MCP Tools (TypeScript layer – previously Python-only)

**Routing tools:**

- `delete_trace` - Delete traces by UUID, position or net name
- `query_traces` - Query/filter traces on the board
- `get_nets_list` - List all nets with net code and class
- `modify_trace` - Modify trace width or layer
- `create_netclass` - Create or update a net class
- `route_differential_pair` - Route a differential pair between two points
- `refill_zones` - Refill all copper zones ⚠️ SWIG segfault risk, prefer IPC/UI

**Component tools:**

- `get_component_pads` - Get all pad data for a component
- `get_component_list` - List all components on the board
- `get_pad_position` - Get absolute position of a specific pad
- `place_component_array` - Place components in a grid array
- `align_components` - Align components along an axis
- `duplicate_component` - Duplicate a component with offset

### Bug Fixes

- `routing.py`: Fix SwigPyObject UUID comparison (`str()` → `m_Uuid.AsString()`)
- `routing.py`: Fix SWIG iterator invalidation after `board.Remove()` by snapshotting `list(board.Tracks())`
- `routing.py`: Add `board.SetModified()` + `track = None` after `Remove()` to prevent dangling SWIG pointer crashes
- `routing.py`: Per-track `try/except` in `query_traces()` to skip invalid objects after bulk delete
- `routing.py`: Add missing return statement (mypy)
- `library.py`: Fix `search_footprints` parameter mapping (`search_term` → `pattern`)
- `library.py`: Fix field access (`fp.name` → `fp.full_name`)
- `library.py`: Accept both `pattern` and `search_term` parameter names
- `library.py`: Fix loop variable shadowing `Path` object (mypy)
- `design_rules.py`: Add type annotation for `violation_counts` (mypy)

### New MCP Tools (cont.)

**Datasheet tools:**

- `get_datasheet_url` - Return LCSC datasheet PDF URL and product page URL for a given
  LCSC number (e.g. `C179739` → `https://www.lcsc.com/datasheet/C179739.pdf`).
  No API key required – URL is constructed directly from the LCSC number.
- `enrich_datasheets` - Scan a `.kicad_sch` file and write LCSC datasheet URLs into
  every symbol that has an `LCSC` property but an empty `Datasheet` field. After
  enrichment the URL appears natively in KiCAD's symbol properties, footprint browser
  and any other tool that reads the standard KiCAD `Datasheet` field.
  Supports `dry_run=true` for preview without writing.
  Implementation: `python/commands/datasheet_manager.py` (text-based, no `skip` writes)

**Schematic tools:**

- `delete_schematic_component` - Remove a placed symbol from a `.kicad_sch` file by
  reference designator (e.g. `R1`, `U3`).

### Bug Fixes (cont.)

- `schematic.ts` / `kicad_interface.py`: Fix missing `delete_schematic_component` MCP tool.

  **Root cause (two separate issues):**
  1. No MCP tool named `delete_schematic_component` existed. Claude had no way to call
     it, so any "delete schematic component" request fell through to the PCB-only
     `delete_component` tool, which searches `pcbnew.BOARD` and always returned
     "Component not found" for schematic symbols.
  2. `component_schematic.py::remove_component()` still used `skip` for writes.
     PR #40 rewrote `DynamicSymbolLoader` (add path) to avoid `skip`-induced schematic
     corruption, but `remove_component` (delete path) was not touched by that PR.

  **Fix:**
  - Added `delete_schematic_component` to the TypeScript tool layer (`schematic.ts`)
    with clear docstring distinguishing it from the PCB `delete_component`.
  - Implemented `_handle_delete_schematic_component` in `kicad_interface.py` using
    direct text manipulation (parenthesis-depth tracking, same approach as PR #40).
    Does not call `component_schematic.py::remove_component()` at all.
  - Error message explicitly guides the user when the wrong tool is used:
    _"note: this tool removes schematic symbols, use delete_component for PCB footprints"_

### Additional Bug Fixes

- `connection_schematic.py` / `kicad_interface.py`: Fix `generate_netlist` missing
  `schematic_path` parameter – without it `get_net_connections` always fell back to
  proximity matching which only returns one connection per component (first wire hit,
  then `break`). PinLocator was never invoked. Fix: added `schematic_path: Optional[Path]`
  to `generate_netlist` signature and threaded it through to `get_net_connections`,
  and updated `_handle_generate_netlist` in `kicad_interface.py` to pass `schematic_path`.
- `server.ts`: Fix KiCAD bundled Python (3.11.5) not being selected on Windows – the
  detection condition `process.env.PYTHONPATH?.includes("KiCad")` was fragile and failed
  in some environments, causing System Python 3.12 to be used instead. Since `pcbnew.pyd`
  is compiled for KiCAD's Python 3.11.5, this resulted in `No module named 'pcbnew'`.
  Fix: removed the condition, KiCAD bundled Python is now always preferred on Windows
  when it exists at `C:\Program Files\KiCad\9.0\bin\python.exe`.
  Also added `KICAD_PYTHON` to `claude_desktop_config.json` as explicit override.
- `pin_locator.py`: Fix `generate_netlist` timeout – `get_pin_location` and
  `get_all_symbol_pins` called `Schematic(schematic_path)` on every single pin lookup,
  causing O(nets × components × pins) schematic file loads (e.g. 400+ loads for a
  medium schematic). Fix: added `_schematic_cache` dict to `PinLocator.__init__`,
  schematic is now loaded once per path and reused.

---

## [2.1.0-alpha] - 2026-01-10

### Phase 1: Intelligent Schematic Wiring System - Core Infrastructure

**Major Features:**

- Automatic pin location discovery with rotation support
- Smart wire routing (direct, orthogonal horizontal/vertical)
- Net label management (local, global, hierarchical)
- S-expression-based wire creation
- Professional right-angle routing

**New Components:**

- `python/commands/wire_manager.py` - S-expression wire creation engine
- `python/commands/pin_locator.py` - Intelligent pin discovery with rotation
- Updated `python/commands/connection_schematic.py` - High-level connection API
- `docs/SCHEMATIC_WIRING_PLAN.md` - Implementation roadmap

**MCP Tools Enhanced:**

- `add_schematic_wire` - Create wires with stroke customization
- `add_schematic_connection` - Auto-connect pins with routing options (NEW)
- `add_schematic_net_label` - Add labels with type and orientation control (NEW)
- `connect_to_net` - Connect pins to named nets (ENHANCED)

**Technical Implementation:**

- Rotation transformation matrix for pin coordinates
- S-expression injection for guaranteed format compliance
- Pin definition caching for performance
- Orthogonal path generation for professional schematics

**Testing:**

- End-to-end integration test: 100% passing
- MCP handler integration test: 100% passing
- Pin discovery with rotation: Verified working
- KiCad-skip verification: All wires/labels correctly formed

---

### Phase 2: Power Nets & Wire Connectivity - COMPLETE

**Major Features:**

- Power symbol support (VCC, GND, +3V3, +5V, etc.) via dynamic loading
- Wire graph analysis for net connectivity tracking
- Geometric wire tracing with tolerance-based point matching
- Accurate netlist generation with component/pin connections
- Critical template mapping bug fixes

**Updates:**

- `connect_to_net()` - Migrated to WireManager + PinLocator
- `get_net_connections()` - Complete rewrite with geometric wire tracing
- `generate_netlist()` - Now uses wire graph analysis for connectivity
- `get_or_create_template()` - Fixed special character handling, auto-reload after dynamic loading
- `add_component()` - Fixed template lookup with symbol iteration

**Bug Fixes:**

- CRITICAL: Template mapping after dynamic symbol loading
- Special character handling in symbol names (+ prefix in +3V3, +5V)
- Schematic reload synchronization after S-expression injection
- Multi-format template reference detection

**Wire Graph Analysis Algorithm:**

1. Find all labels matching target net name
2. Trace wires connected to label positions (point coincidence)
3. Collect all wire endpoints and polyline segments
4. Match component pins at wire connection points using PinLocator
5. Return accurate component/pin connection pairs

**Technical Implementation:**

- Tolerance-based point matching (0.5mm for grid alignment)
- Multi-segment wire (polyline) support
- Rotation-aware pin location matching via PinLocator
- Fallback proximity detection (10mm threshold)
- Template existence checking via symbol iteration (handles special characters)

**Testing:**

- Power symbols: 4/4 loaded (VCC, GND, +3V3, +5V)
- Components: 4/4 placed
- Connections: 8/8 created successfully
- Net connectivity: 100% accurate (VCC: 2, GND: 4, +3V3: 1, +5V: 1)
- Netlist generation: 4 nets with accurate connections
- Comprehensive integration test: 100% PASSING

**Commits:**

- `c67f400` - Updated connect_to_net to use WireManager
- `b77f008` - Fixed template mapping bug (critical)
- `a5a542b` - Implemented wire graph analysis

**Addresses:**

- Issue #26 - Schematic workflow wiring functionality (Phase 2)

---

### Phase 2: JLCPCB Integration Complete

**Major Features:**

- ✅ Complete JLCPCB parts integration via JLCSearch public API
- ✅ Access to ~100k JLCPCB parts catalog
- ✅ Real-time stock and pricing data
- ✅ Parametric component search
- ✅ Cost optimization (Basic vs Extended library)
- ✅ KiCad footprint mapping
- ✅ Alternative part suggestions

**New Components:**

- `python/commands/jlcsearch.py` - JLCSearch API client (no auth required)
- `python/commands/jlcpcb_parts.py` - Enhanced with `import_jlcsearch_parts()`
- `docs/JLCPCB_INTEGRATION.md` - Comprehensive integration guide

**MCP Tools Available:**

- `download_jlcpcb_database` - Download full parts catalog
- `search_jlcpcb_parts` - Parametric search with filters
- `get_jlcpcb_part` - Part details + footprint suggestions
- `get_jlcpcb_database_stats` - Database statistics
- `suggest_jlcpcb_alternatives` - Find similar/cheaper parts

**Technical Improvements:**

- SQLite database with full-text search (FTS5)
- Package-to-footprint mapping for standard SMD packages
- Price comparison and cost optimization algorithms
- HMAC-SHA256 authentication support (for official JLCPCB API)

**Testing:**

- All integration tests passing
- Database operations validated
- Live API connectivity confirmed
- End-to-end MCP tool testing complete

**Documentation:**

- Complete API reference with examples
- Package mapping tables (0402, 0603, 0805, SOT-23, etc.)
- Best practices guide
- Troubleshooting section

---

## [2.1.0-alpha] - 2025-11-30

### Phase 1: Schematic Workflow Fix

**Critical Bug Fix:**

- ✅ Fixed completely broken schematic workflow (Issue #26)
- Created template-based symbol cloning approach
- All schematic tests now passing

**Root Cause:**

- kicad-skip library limitation: cannot create symbols from scratch, only clone existing ones

**Solution:**

- Template schematic with cloneable R, C, LED symbols
- Updated `create_project` to create both PCB and schematic
- Rewrote `add_schematic_component` to use `clone()` API
- Proper UUID generation and position setting

**Files Modified:**

- `python/commands/project.py` - Now creates schematic files
- `python/commands/schematic.py` - Uses template approach
- `python/commands/component_schematic.py` - Complete rewrite

**Files Created:**

- `python/templates/template_with_symbols.kicad_sch`
- `python/templates/empty.kicad_sch`
- `docs/SCHEMATIC_WORKFLOW_FIX.md`

**Testing:**

- Created comprehensive test suite
- All 7 tests passing
- KiCad CLI validation successful

---

## [2.0.0-alpha] - 2025-11-05

### Router Pattern & Tool Organization

**Major Architecture Change:**

- Implemented tool router pattern (70% context reduction)
- 12 direct tools, 47 routed tools in 7 categories
- Smart tool discovery system

**New Router Tools:**

- `list_tool_categories` - Browse available categories
- `get_category_tools` - View tools in category
- `search_tools` - Find tools by keyword
- `execute_tool` - Run any routed tool

**Benefits:**

- Dramatically reduced AI context usage
- Maintained full functionality (64 tools)
- Improved tool discoverability
- Better organization for users

---

## [2.0.0-alpha] - 2025-11-01

### IPC Backend Integration

**Experimental Feature:**

- KiCad 9.0 IPC API integration for real-time UI sync
- Changes appear immediately in KiCad (no manual reload)
- Hybrid backend: IPC + SWIG fallback
- 20+ commands with IPC support

**Implementation:**

- Routing operations (interactive push-and-shove)
- Component placement and modification
- Zone operations and fills
- DRC and verification

**Status:**

- Under active development
- Enable via KiCad: Preferences > Plugins > Enable IPC API Server
- Automatic fallback to SWIG when IPC unavailable

---

## [2.0.0-alpha] - 2025-10-26

### Initial JLCPCB Integration (Local Libraries)

**Features:**

- Local JLCPCB symbol library search
- Integration with KiCad Plugin and Content Manager
- Search by LCSC part number, manufacturer, description

**Credit:**

- Contributed by [@l3wi](https://github.com/l3wi)

**Components:**

- `python/commands/symbol_library.py`
- Basic library search functionality

---

## [1.0.0] - 2025-10-01

### Initial Release

**Core Features:**

- 64 fully-documented MCP tools
- JSON Schema validation for all tools
- 8 dynamic resources for project state
- Cross-platform support (Linux, Windows, macOS)
- Comprehensive error handling
- Detailed logging

**Tool Categories:**

- Project Management (4 tools)
- Board Operations (9 tools)
- Component Management (8 tools)
- Routing (6 tools)
- Export & Manufacturing (5 tools)
- Design Rule Checking (4 tools)
- Schematic Operations (6 tools)
- Symbol Library (3 tools)
- JLCPCB Integration (5 tools)

**Platform Support:**

- Linux (KiCad 7.x, 8.x, 9.x)
- Windows (KiCad 9.x)
- macOS (KiCad 9.x)

**Documentation:**

- Complete README with setup instructions
- Platform-specific guides
- Tool reference documentation
- Contributing guidelines

---

## Version Numbering

- **2.1.0-alpha**: Current development version with JLCPCB integration
- **2.0.0-alpha**: Router pattern and IPC backend
- **1.0.0**: Initial stable release

## Breaking Changes

### 2.1.0-alpha

- None (additive changes only)

### 2.0.0-alpha

- Tool execution now requires router for 47 tools
- Direct tool access limited to 12 high-frequency tools
- Schema validation stricter (catches errors earlier)

## Deprecations

### 2.1.0-alpha

- `docs/JLCPCB_USAGE_GUIDE.md` - Superseded by `docs/JLCPCB_INTEGRATION.md`
- `docs/JLCPCB_INTEGRATION_PLAN.md` - Implementation complete

## Migration Guide

### Upgrading to 2.1.0-alpha from 2.0.0-alpha

**New Dependencies:**

- No new system dependencies
- Python packages: `requests` (already in requirements.txt)

**Database Setup:**

1. Run `download_jlcpcb_database` tool (one-time, ~5-10 minutes)
2. Database created at `data/jlcpcb_parts.db`
3. Subsequent searches use local database (instant)

**API Changes:**

- All existing tools remain compatible
- 5 new JLCPCB tools available
- No breaking changes to existing functionality

### Upgrading to 2.0.0-alpha from 1.0.0

**Router Pattern:**

- Some tools now accessed via `execute_tool` instead of direct calls
- Use `list_tool_categories` to discover available tools
- Search with `search_tools` to find specific functionality

**IPC Backend (Optional):**

- Enable in KiCad: Preferences > Plugins > Enable IPC API Server
- Set `KICAD_BACKEND=ipc` environment variable
- Falls back to SWIG if unavailable

---

## Credits

- **JLCSearch API**: [@tscircuit](https://github.com/tscircuit/jlcsearch)
- **JLCParts Database**: [@yaqwsx](https://github.com/yaqwsx/jlcparts)
- **Local JLCPCB Search**: [@l3wi](https://github.com/l3wi)
- **KiCad**: KiCad Development Team
- **MCP Protocol**: Anthropic

## License

See LICENSE file for details.
