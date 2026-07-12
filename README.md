# KiCAD MCP Server

A Model Context Protocol (MCP) server that enables AI assistants like Claude to interact with KiCAD for PCB design automation. Built on the MCP 2025-06-18 specification, this server provides comprehensive tool schemas and real-time project state access for intelligent PCB design workflows.

> This project began as a fork of [mixelpixx/KiCAD-MCP-Server](https://github.com/mixelpixx/KiCAD-MCP-Server) and is now developed and maintained independently. See [Acknowledgments](#acknowledgments) for credits to the original project.

## Overview

The [Model Context Protocol](https://modelcontextprotocol.io/) is an open standard from Anthropic that allows AI assistants to securely connect to external tools and data sources. This implementation provides a standardized bridge between AI assistants and KiCAD, enabling natural language control of PCB design operations.

**Key Capabilities:**

- 125 tools with JSON Schema validation, each registered directly as an MCP tool
- 8 dynamic resources exposing project state
- Complete schematic workflow with 34 tools, hierarchical sheets, and dynamic symbol loading (~10,000 symbols)
- Freerouting autorouter integration (Java, Docker, or Podman)
- Custom footprint and symbol creation tools
- JLCPCB parts integration with 2.5M+ component catalog and local library search
- Datasheet enrichment via LCSC
- Full MCP 2025-06-18 protocol compliance
- Cross-platform support (Linux, Windows, macOS)
- Real-time KiCAD UI integration via IPC API (experimental)
- Comprehensive error handling and logging

## Release Notes

See [CHANGELOG.md](CHANGELOG.md) for the full release history and what changed in each version.

## Available Tools

The server provides 125 tools, each registered directly as an MCP tool -- just ask Claude what you want to accomplish. The authoritative source is the `server.tool(...)` registrations in `src/tools/`; the list below is generated from those registrations.

### Project Management (5 tools)

- `create_project` - Create a new KiCAD project
- `open_project` - Open an existing KiCAD project
- `save_project` - Save the current KiCAD project
- `get_project_info` - Get information about the current KiCAD project
- `snapshot_project` - Save a named checkpoint snapshot of the current project state (renders board to PDF and records step label)

### Board Operations (12 tools)

- `set_board_size` - Set the PCB board dimensions by drawing a rectangular Edge.Cuts outline
- `add_layer` - Add a new copper or technical layer to the PCB stackup
- `set_active_layer` - Set the currently active PCB layer by name (e.g. F.Cu, B.Silkscreen)
- `get_board_info` - Retrieve board info: dimensions, full layer list, and bounding-box extents
- `add_board_outline` - Draw the PCB board outline (Edge.Cuts layer) as a rectangle, rounded rectangle, circle or polygon
- `add_mounting_hole` - Place a mounting hole (NPTH or PTH) at the specified position on the PCB
- `add_board_text` - Add a text label to a PCB layer (silkscreen, copper, fab)
- `get_board_2d_view` - Render the current board as a PNG or SVG image, with optional layer selection and region crop
- `import_svg_logo` - Import an SVG file as filled graphic polygons onto a PCB layer (default front silkscreen)
- `get_pcb_overview` - One-shot snapshot of the loaded PCB: components, tracks, zones, nets, layers in a single response
- `board_origin` - Read or move the board's grid or drill/place origin (IPC-only); pass position to write
- `title_block` - Read or partial-update the board's title block (title, date, revision, company, comment slots; IPC-only)

### Component Management (14 tools)

- `place_component` - Add a NEW footprint instance to the PCB at the given position
- `move_component` - Move a PCB component to a new position
- `rotate_component` - Rotate a PCB component to an absolute angle in degrees
- `delete_component` - Remove a component from the PCB by its reference designator
- `edit_component` - Edit properties of an existing PCB component (reference, value, footprint)
- `find_component` - Search for a PCB component by reference designator or value and return its position and properties
- `get_component_properties` - Return all properties of a PCB component (position, rotation, layer, value, footprint)
- `get_component_pads` - Return pads of a PCB component with exact positions, nets and sizes (pass pad for a single one)
- `get_component_list` - Return a list of all components on the PCB, optionally filtered by layer or bounding box region
- `place_component_array` - Place a rectangular grid array of identical components on the PCB with configurable row/column spacing
- `align_components` - Align multiple PCB components horizontally, vertically or on a grid with optional spacing
- `check_courtyard_overlaps` - Detect courtyard overlaps between footprints, and optionally flag courtyards past the board outline
- `duplicate_component` - Duplicate an existing PCB component at an offset position, optionally with a new reference designator
- `auto_place_components` - Auto-place components with a connectivity-driven greedy heuristic (strongly connected parts cluster together)

### Routing (15 tools)

- `add_net` - Create a new net on the PCB
- `route_trace` - Route a copper trace between two XY points on a fixed layer: straight, or an arc via the optional mid point
- `add_via` - Add a via to the PCB
- `copper_pour` - Manage copper pours (zones): action=add|edit|delete|refill
- `delete_trace` - Delete traces from the PCB
- `query_copper` - Query copper on the board: trace segments (kind=traces, optionally vias) or zones/pours (kind=zones), filtered by net/layer/region
- `add_gnd_stitching_vias` - Drop GND stitching vias with collision checking against every non-GND segment/via/pad on all copper layers (PTH vias span the full stackup)
- `get_nets_list` - Get a list of all nets in the PCB with optional statistics
- `modify_trace` - Modify an existing trace (change width, layer, or net)
- `create_netclass` - Create (or update) a net class with custom design rules and persist it to the .kicad_pro project file
- `assign_netclass_pattern` - Append a wildcard pattern -> net-class rule to the .kicad_pro (net_settings.netclass_patterns)
- `route_differential_pair` - Route a differential pair between two sets of points
- `route_smart` - Route between two pads (or points): grid A\* obstacle avoidance by default, or strategy=direct for one straight segment
- `report_net_lengths` - Report routed copper length per net (mm) with segment/via counts, layers, and max skew across matched nets
- `copy_routing_pattern` - Copy routing pattern (traces and vias) from a group of source components to a matching group of target components

### Graphic Shapes (4 tools)

- `add_shape` - Draw a graphic shape (no net) on any layer: segment, arc, circle, rectangle, or polygon
- `list_shapes` - List graphic shapes on the board (id, kind, layer, width, filled, bounding box) with optional layer / kind / boundingBox filters
- `delete_shape` - Delete graphic shape(s)
- `edit_shape` - Edit one graphic shape (by id from list_shapes): move it by dx/dy, change layer, stroke width, or fill

### Schematic (34 tools)

- `create_schematic` - Create a new schematic
- `add_schematic_component` - Add a component to the schematic
- `delete_schematic_component` - Remove a component from the schematic
- `edit_schematic_component` - Update a placed schematic symbol in place: footprint, value, reference, field positions, custom properties (add and remove)
- `get_schematic_component` - Get full component info from a schematic: position plus EVERY field's value and label position
- `move_schematic_component` - Move a placed symbol
- `rotate_schematic_component` - Rotate a placed symbol in the schematic
- `annotate_schematic` - Assign reference designators to unannotated components (placeholder refs ending in '?')
- `add_schematic_wire` - Draw a wire between two or more points
- `add_schematic_net_label` - Add a net label
- `set_no_connect` - Add (or with remove=true, delete) a no-connect flag on a pin that is intentionally left unconnected
- `connect_to_net` - Connect a component pin to a named net by adding a wire stub and net label at the exact pin endpoint
- `get_net_connections` - Get all connections for a named net
- `get_wire_connections` - Return the net name plus all wires and pins connected at a point, given reference + pin OR x/y in mm
- `get_schematic_pin_locations` - Returns the exact x/y coordinates of every pin on a schematic component
- `connect_passthrough` - Wire all pins of a source connector to the matching pins of a target connector (FFC/ribbon adapters)
- `delete_schematic_wire` - Remove a wire from the schematic by start and end coordinates
- `edit_schematic_net_label` - Edit, move, or delete an existing net label (action=edit|move|delete)
- `add_schematic_hierarchical_label` - Add a hierarchical label (sheet interface port) to a sub-sheet schematic
- `export_schematic_pdf` - Export schematic to PDF format using kicad-cli
- `run_erc` - Run ERC on a schematic and return all violations
- `generate_netlist` - Return a structured JSON netlist — components (reference, value, footprint) and nets (name + connected component/pin pairs)
- `run_simulation` - Run a SPICE analysis (op / tran / ac / dc) on the schematic via ngspice batch mode
- `sync_schematic_to_board` - Import the schematic netlist into the PCB (= F8 / Tools → Update PCB from Schematic)
- `get_schematic_overview` - One-shot snapshot of a schematic: components, wires, labels, and nets in a single response
- `list_schematic_items` - List schematic items of one kind: components, nets, wires, labels, or texts
- `check_schematic_layout` - Run schematic layout health checks: overlaps, wires crossing symbols, floating labels, orphaned wires
- `get_schematic_view` - Return a rasterized image of the schematic (PNG or SVG), optionally cropped to a region
- `get_elements_in_region` - List all symbols, wires, and labels within a rectangular region of the schematic
- `snap_to_grid` - Snap schematic element coordinates to the nearest grid point
- `get_net_at_point` - Returns the net name at a given (x, y) coordinate in a schematic, or null if no net label
- `add_schematic_text` - Add a free-form text annotation to the schematic
- `create_hierarchical_sheet` - Create a hierarchical sheet in a parent schematic, optionally creating the child .kicad_sch, interface pins, or a pinned page number
- `add_sheet_pin` - Add a pin to a sheet symbol block on the parent schematic

### Design Rules / DRC (3 tools)

- `design_rules` - Read or update PCB design rules: call with no parameters to read; pass any parameter to update
- `run_drc` - Run the KiCAD Design Rule Check (DRC) on the current PCB and return violations
- `assign_net_to_class` - Assign a net to an existing net class to apply its specific design rules

### Export (6 tools)

- `export_gerber` - Export PCB Gerber manufacturing files to a directory
- `export_pdf` - Export the PCB layout as a PDF document, optionally selecting layers, page size and colour mode
- `export_3d` - Export the PCB as a 3D model (STEP, STL, VRML or OBJ) including optional copper, solder mask, silkscreen and component 3D models
- `export_bom` - Export a Bill of Materials (BOM) from the PCB in CSV, XML, HTML or JSON format
- `export_netlist` - Export the schematic netlist to a file via kicad-cli (KiCad XML default, plus Spice, Cadstar, OrcadPCB2)
- `export_position_file` - Export a component placement/position file (pick-and-place) for PCB assembly in CSV or ASCII format

### Libraries (footprints and symbols) (7 tools)

- `list_libraries` - List the names of all installed footprint or symbol libraries (type=footprint|symbol)
- `search_library_parts` - Search footprints or symbols across all installed libraries (type=footprint|symbol)
- `list_library_contents` - List the footprints or symbols contained in one named library (type=footprint|symbol)
- `get_library_part_info` - Get detailed information about one footprint or symbol (type=footprint|symbol)
- `register_library` - Register a .pretty footprint library or .kicad_sym symbol library in KiCAD's lib-table
- `refresh_symbol_libraries` - Force-rebuild the symbol library index from sym-lib-table on disk
- `refresh_schematic_lib_symbols` - Re-inject every embedded lib_symbols entry in a .kicad_sch from the on-disk .kicad_sym

### Footprint and Symbol Creators (6 tools)

- `create_footprint` - Create a new KiCAD footprint (.kicad_mod) inside a .pretty library directory
- `edit_footprint_pad` - Edit an existing pad inside a .kicad_mod footprint file
- `list_footprint_libraries` - Discover footprint libraries by scanning the filesystem for .pretty directories
- `create_symbol` - Create a schematic symbol in a .kicad_sym library (file created if missing); register with register_library afterwards
- `delete_symbol` - Remove a symbol from a .kicad_sym library file
- `list_symbols_in_library` - List the SYMBOL names in a single .kicad_sym library FILE given its path (libraryPath)

### Datasheets (1 tools)

- `enrich_datasheets` - Fill in missing Datasheet URLs from LCSC part numbers

### JLCPCB Integration (8 tools)

- `download_jlcpcb_database` - Download the JLCPCB parts catalog into a local SQLite database (one-time setup)
- `search_jlcpcb_parts` - Parametric search over the local JLCPCB parts database (package, category, stock, Basic/Extended)
- `get_jlcpcb_part` - Get detailed information about a specific JLCPCB part by LCSC number
- `download_jlcpcb_datasheet` - Download a part's datasheet PDF via its LCSC part number
- `get_jlcpcb_database_stats` - Get statistics about the local JLCPCB parts database
- `suggest_jlcpcb_alternatives` - Suggest cheaper or better-stocked JLCPCB alternatives for a part
- `import_jlcpcb_symbols` - Import schematic symbols (with footprints) from the EasyEDA/JLCPCB library into the shared local cache library
- `check_bom_availability` - Check every BOM line of the loaded board against the local JLCPCB parts catalog (stock, pricing, Basic vs Extended)

### Freerouting Autorouter (2 tools)

- `autoroute` - Run Freerouting autorouter on the current PCB: exports Specctra DSN, runs the Freerouting CLI, imports the routed SES
- `check_freerouting` - Check if Java and Freerouting JAR are available on the system

### UI and Backend Management (8 tools)

- `get_backend_info` - Return the active backend identifier, version, and a human-readable mode description
- `manage_kicad_ui` - Check whether the KiCAD UI is running (action=status) or launch it (action=launch)
- `reconcile_backends` - Flush pending changes between the SWIG and IPC backends
- `run_action` - Invoke any KiCad internal TOOL_ACTION by name (escape hatch via IPC)
- `manage_selection` - Manage the KiCAD board editor selection (IPC-only)
- `hit_test` - Find board items at (x, y) (IPC-only)
- `interactive_move` - Start KiCad's interactive move tool on the supplied items (IPC-only)
- `transaction` - Manage a KiCad transaction / undo group (IPC-only)

## Prerequisites

### Required Software

**KiCAD 9.0 or higher**

- Download from [kicad.org/download](https://www.kicad.org/download/)
- Must include Python module (pcbnew)
- Verify installation:
  ```bash
  python3 -c "import pcbnew; print(pcbnew.GetBuildVersion())"
  ```

**Node.js 18 or Higher**

- Download from [nodejs.org](https://nodejs.org/)
- Verify: `node --version` and `npm --version`

**Python 3.9 or Higher**

- Comes bundled with KiCAD (macOS builds ship Python 3.9; Linux/Windows builds ship Python 3.11)
- Required packages (auto-installed):
  - kicad-python (kipy) >= 0.5.0 (IPC API support, optional but recommended)
  - kicad-skip >= 0.1.0 (schematic support)
  - Pillow >= 9.0.0 (image processing)
  - cairosvg >= 2.7.0 (SVG rendering)
  - colorlog >= 6.7.0 (logging)
  - pydantic >= 2.5.0 (validation)
  - requests >= 2.32.5 (HTTP client)
  - python-dotenv >= 1.0.0 (environment)

**MCP Client**
Choose one:

- [Claude Desktop](https://claude.ai/download) - Official Anthropic desktop app
- [Claude Code](https://docs.claude.com/claude-code) - Official CLI tool
- [Cline](https://github.com/cline/cline) - VSCode extension

### Supported Platforms

- **Linux** (Ubuntu 22.04+, Fedora, Arch) - Primary platform, fully tested
- **Windows 10/11** - Fully supported with automated setup
- **macOS** - Experimental support

## Installation

### Linux (Ubuntu/Debian)

```bash
# Install KiCAD 9.0 or higher
sudo add-apt-repository --yes ppa:kicad/kicad-9.0-releases
sudo apt-get update
sudo apt-get install -y kicad kicad-libraries

# Install Node.js
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs

# Clone and build
git clone https://github.com/tiancode/KiCAD-MCP-Server.git
cd KiCAD-MCP-Server
npm install
pip3 install -r requirements.txt
npm run build

# Verify
python3 -c "import pcbnew; print(pcbnew.GetBuildVersion())"
```

### Windows 10/11

**Automated Setup (Recommended):**

```powershell
git clone https://github.com/tiancode/KiCAD-MCP-Server.git
cd KiCAD-MCP-Server
.\setup-windows.ps1
```

The script will:

- Detect KiCAD installations, including both machine-wide installs under
  `C:\Program Files\KiCad` and per-user installs under
  `%LOCALAPPDATA%\Programs\KiCad`
- Verify prerequisites
- Install dependencies
- Build project
- Generate configuration
- Run diagnostics

**Manual Setup:**
Run `.\setup-windows.ps1` from PowerShell — the script auto-detects your
KiCAD install, configures Python paths, builds the project, and writes
your Claude Desktop MCP config.

### macOS

**Important:** On macOS, use KiCAD's bundled Python to ensure proper access to the `pcbnew` module.

#### Manual Setup

```bash
# Install KiCAD 9.0 from kicad.org/download/macos

# Install Node.js
brew install node@20

# Clone repository
git clone https://github.com/tiancode/KiCAD-MCP-Server.git
cd KiCAD-MCP-Server

# Create virtual environment using KiCAD's bundled Python
/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 -m venv venv --system-site-packages

# Activate virtual environment
source venv/bin/activate

# Install dependencies
npm install
pip install -r requirements.txt
npm run build
```

**Note:** The `--system-site-packages` flag is required to access KiCAD's `pcbnew` module from the virtual environment.

#### Automated Setup

To simplify configuration with Claude Desktop, this repository provides a macOS setup script:

```bash
./setup-macos.sh
```

In case of error `zsh: permission denied: ./setup-macos.sh` you can either:

- always allow the script to be executed by running: `chmod +x setup-macos.sh`.
- alternatively explicitly run it with bash: `bash setup-macos.sh` so no chmod change needed.

This script does **not replace the manual setup above** — it assumes dependencies are already installed and the project is built. Instead, it automates:

- detection of your environment (Node.js, KiCad Python, `pcbnew`)
- resolving the correct macOS `PYTHONPATH`
- generating the correct Claude Desktop MCP configuration
- safely merging the configuration into your existing Claude config
- optionally writing the configuration with backup support

##### Basic Usage

###### Verify setup (no changes)

```bash
./setup-macos.sh --verify
```

###### Preview configuration (dry run)

```bash
./setup-macos.sh --dry-run
```

###### Apply configuration

```bash
./setup-macos.sh --apply
```

After applying, restart Claude Desktop.

##### Parameters

###### Required parameters

None. The script works out-of-the-box using sensible defaults.

###### Optional parameters

##### `--name NAME`

Specify the MCP server name in Claude Desktop.

Default:

```text
kicad
```

Example:

```bash
./setup-macos.sh --apply --name kicad-dev
```

Use this when:

- running multiple MCP configurations
- testing forks or development versions
- avoiding overwriting an existing setup

##### `--claude-config PATH`

Specify a custom Claude Desktop configuration file.

Default:

```text
~/Library/Application Support/Claude/claude_desktop_config.json
```

Example:

```bash
./setup-macos.sh --dry-run --claude-config ~/tmp/claude_config.json
```

Use this when:

- testing configurations safely
- using non-standard config locations
- debugging without modifying your main setup

##### `--yes`

Skip confirmation prompt when applying changes.

Example:

```bash
./setup-macos.sh --apply --yes
```

##### After Setup

1. Fully quit Claude Desktop
2. Reopen Claude Desktop
3. Open a new chat
4. Click **+ → Connectors**
5. Verify the server appears (e.g. `kicad` or your custom name)

Test with prompt in Claude Desktop:

```text
Use the kicad MCP server to run check_kicad_ui.
```

##### Notes

- The script only modifies the `mcpServers` section and leaves all other configuration untouched
- Existing configurations are automatically backed up before changes
- macOS support relies on KiCad’s bundled Python; system Python will not work correctly
- If KiCad is updated or moved, re-run the script to refresh paths

---

## Configuration

### Claude Desktop

Edit configuration file:

- **Linux:** `~/.config/Claude/claude_desktop_config.json`
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

**Configuration:**

```json
{
  "mcpServers": {
    "kicad": {
      "command": "node",
      "args": ["/path/to/KiCAD-MCP-Server/dist/index.js"],
      "env": {
        "PYTHONPATH": "/path/to/kicad/python",
        "LOG_LEVEL": "info"
      }
    }
  }
}
```

**Platform-specific PYTHONPATH:**

- **Linux:** `/usr/lib/kicad/lib/python3/dist-packages`
- **Windows:** `C:\Program Files\KiCad\10.0\lib\python3\dist-packages` or
  `%LOCALAPPDATA%\Programs\KiCad\10.0\lib\python3\dist-packages`
- **macOS:** `/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/lib/python3.9/site-packages`

#### Linux Python Detection

The server automatically detects Python on Linux in this priority order:

1. **Virtual environment** - `venv/bin/python` or `.venv/bin/python` (highest priority)
2. **KICAD_PYTHON env var** - User override for non-standard installations
3. **KiCad bundled Python** - `/usr/lib/kicad/bin/python3`, `/usr/local/lib/kicad/bin/python3`, `/opt/kicad/bin/python3`
4. **System Python via which** - Resolves `which python3` to absolute path (e.g., `/usr/bin/python3`)
5. **Common system paths** - `/usr/bin/python3`, `/bin/python3`

**For most standard Linux installations (Ubuntu, Debian, Fedora, Arch), no KICAD_PYTHON configuration is needed** - the server will automatically find your Python installation.

**Troubleshooting:**

If you see "Python executable not found: python3", you can manually specify the Python path:

```json
{
  "mcpServers": {
    "kicad": {
      "command": "node",
      "args": ["/path/to/KiCAD-MCP-Server/dist/index.js"],
      "env": {
        "KICAD_PYTHON": "/usr/bin/python3",
        "PYTHONPATH": "/usr/lib/kicad/lib/python3/dist-packages"
      }
    }
  }
}
```

To find your Python path:

```bash
which python3  # Example output: /usr/bin/python3
python3 -c "import pcbnew; print(pcbnew.GetBuildVersion())"  # Verify pcbnew access
```

### GitHub Copilot (VS Code)

Copy the template to your workspace:

```bash
cp config/vscode-mcp.example.json .vscode/mcp.json
```

VS Code will auto-detect `.vscode/mcp.json` and register the server. The template uses `${workspaceFolder}` so no path editing is needed.

> **Note:** `.vscode/mcp.json` is listed in `.gitignore` — your local configuration won't be committed.

### Cline (VSCode)

Edit: `~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json`

Use the same configuration format as Claude Desktop above.

### Claude Code

Claude Code automatically detects MCP servers in the current directory. No additional configuration needed.

### OpenCode (Windows)

OpenCode uses a different MCP configuration schema than Claude Desktop. Use
`setup-windows-opencode.ps1` to verify the local setup and write the correct
OpenCode `mcp` entry.

OpenCode project configuration is written to `opencode.json` in the target
project root. The script keeps the KiCAD MCP server repository separate from the
target project:

- `McpServerPath` is this repository, where `dist/index.js` is built
- `ProjectPath` is the project that should receive `opencode.json`

**When this is useful:**

- You use [OpenCode](https://opencode.ai/) as your MCP client on Windows
- You want a project-local MCP server available only in one project
- You want a global OpenCode MCP server available from any workspace
- You need to verify KiCAD Python (`pcbnew`), Node.js, and `dist/index.js`
  before changing OpenCode configuration

#### Verify setup without changes

Use this first when diagnosing installation or path problems. It detects KiCAD,
tests `pcbnew`, checks Node.js, and verifies the built MCP entrypoint.

```powershell
.\setup-windows-opencode.ps1 -Verify -SkipInstall -SkipBuild
```

#### Preview OpenCode configuration

Use dry run mode when you want to inspect the exact JSON before writing it.

```powershell
.\setup-windows-opencode.ps1 -DryRun -SkipInstall -SkipBuild
```

Example generated OpenCode shape:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "kicad": {
      "type": "local",
      "command": ["node", "C:\\path\\to\\KiCAD-MCP-Server\\dist\\index.js"],
      "environment": {
        "NODE_ENV": "production",
        "LOG_LEVEL": "info",
        "KICAD_AUTO_LAUNCH": "false",
        "KICAD_MCP_DEV": "0",
        "PYTHONPATH": "C:\\Program Files\\KiCad\\10.0\\bin\\Lib\\site-packages"
      },
      "enabled": true,
      "timeout": 30000
    }
  }
}
```

A copyable template is also provided at `config/opencode.json`. Replace the
placeholder paths before using it directly.

#### Apply project-local configuration

Use this when you only want KiCAD MCP enabled for one project. The script writes
`opencode.json` in the target project root and backs up an existing file before
changing it.

```powershell
.\setup-windows-opencode.ps1 -Apply -Scope project
```

By default, `ProjectPath` is the current working directory.

To configure another project, pass `-ProjectPath`:

```powershell
.\setup-windows-opencode.ps1 -Apply -Scope project -ProjectPath "C:\path\to\your-project"
```

If the setup script is not located in the KiCAD MCP Server repository, pass
`-McpServerPath` so the generated config points to the correct `dist/index.js`:

```powershell
.\setup-windows-opencode.ps1 `
  -Apply `
  -Scope project `
  -ProjectPath "C:\path\to\your-project" `
  -McpServerPath "C:\path\to\KiCAD-MCP-Server"
```

#### Apply global OpenCode configuration

Use this when you want the KiCAD MCP server available from any OpenCode
workspace. The script writes `%USERPROFILE%\.config\opencode\opencode.json`.

```powershell
.\setup-windows-opencode.ps1 -Apply -Scope global
```

#### Use a custom MCP server name

Use this when testing multiple forks or keeping separate development and stable
KiCAD MCP entries.

```powershell
.\setup-windows-opencode.ps1 -Apply -Scope project -Name kicad-dev
```

#### Use a custom KiCAD installation path

Use this when KiCAD is installed outside the standard Windows locations.

```powershell
.\setup-windows-opencode.ps1 -Apply -Scope project -KiCadRoot "D:\Apps\KiCad\10.0"
```

#### Skip install or build steps

Use these flags when dependencies are already installed or the project is
already built.

```powershell
.\setup-windows-opencode.ps1 -Apply -Scope project -SkipInstall -SkipBuild
```

#### After applying configuration

1. Fully quit OpenCode.
2. Start OpenCode again so it reloads `opencode.json`.
3. Ask OpenCode to use the `kicad` MCP server and run `check_kicad_ui`.

#### Disable the OpenCode MCP server

To disable the server without removing the full configuration, set the entry to
`enabled: false` and restart OpenCode.

```json
{
  "mcp": {
    "kicad": {
      "enabled": false
    }
  }
}
```

If OpenCode is running, the MCP server process is managed by OpenCode and
normally stops when OpenCode exits.

### JLCPCB Integration Setup (Optional)

The JLCPCB integration provides two modes that can be used independently or together:

**Mode 1: JLCSearch Public API (Recommended - No Setup Required)**

The easiest way to access JLCPCB's parts catalog:

- No API credentials needed
- No JLCPCB account required
- Access to 2.5M+ parts with pricing and stock data
- Download time: 40-60 minutes for full catalog (100-part batches due to API limit)

To download the database:

```
Ask Claude: "Download the JLCPCB parts database"
```

This creates a local SQLite database at `data/jlcpcb_parts.db` (3-5 GB for full 2.5M+ part catalog).

**Mode 2: Local Symbol Libraries (No Setup Required)**

Install JLCPCB libraries via KiCAD's Plugin and Content Manager:

1. Open KiCAD
2. Go to Tools > Plugin and Content Manager
3. Search for "JLCPCB" or "JLC"
4. Install libraries like `JLCPCB-KiCAD-Library` or `EDA_MCP`
5. Use `search_symbols` to find components with pre-configured footprints and LCSC IDs

**Mode 3: Official JLCPCB API (Advanced - Requires Enterprise Account)**

For users with JLCPCB enterprise accounts and order history:

1. **Get API Credentials**
   - Log in to [JLCPCB](https://jlcpcb.com/)
   - Navigate to Account > API Management (requires enterprise approval)
   - Create API Key and save your `appKey` and `appSecret`
   - Note: This requires prior order history and enterprise account approval

2. **Configure Environment Variables**

   Add to your shell profile (`~/.bashrc`, `~/.zshrc`, or `~/.profile`):

   ```bash
   export JLCPCB_API_KEY="your_app_key_here"
   export JLCPCB_API_SECRET="your_app_secret_here"
   ```

   Or create a `.env` file in the project root:

   ```
   JLCPCB_API_KEY=your_app_key_here
   JLCPCB_API_SECRET=your_app_secret_here
   ```

## Usage Examples

### Basic PCB Design Workflow

```text
Create a new KiCAD project named 'LEDBoard' in my Documents folder.
Set the board size to 50mm x 50mm and add a rectangular outline.
Place a mounting hole at each corner, 3mm from the edges, with 3mm diameter.
Add text 'LED Controller v1.0' on the front silkscreen at position x=25mm, y=45mm.
```

### Component Placement

```text
Place an LED at x=10mm, y=10mm using footprint LED_SMD:LED_0805_2012Metric.
Create a grid of 4 resistors (R1-R4) starting at x=20mm, y=20mm with 5mm spacing.
Align all resistors horizontally and distribute them evenly.
```

### Routing

```text
Create a net named 'LED1' and route a 0.3mm trace from R1 pad 2 to LED1 anode.
Add a copper pour for GND on the bottom layer covering the entire board.
Create a differential pair for USB_P and USB_N with 0.2mm width and 0.15mm gap.
```

### Autoroute with Freerouting

Automatically route all unconnected nets using the [Freerouting](https://github.com/freerouting/freerouting) autorouter.

**Setup (one-time):**

```bash
# 1. Download the Freerouting JAR
mkdir -p ~/.kicad-mcp
curl -L -o ~/.kicad-mcp/freerouting.jar \
  https://github.com/freerouting/freerouting/releases/download/v2.0.1/freerouting-2.0.1-executable.jar

# 2. Runtime — pick ONE:
#    Option A: Docker (recommended, no Java install needed)
docker pull eclipse-temurin:21-jre

#    Option B: Install Java 21+ locally
#    (Ubuntu/Debian) sudo apt install openjdk-21-jre
```

The autorouter auto-detects which runtime is available (Java 21+ direct, or Docker/Podman fallback).

```text
Check if Freerouting is ready on my system.
Autoroute the current board using Freerouting with a 5-minute timeout.
```

**Step-by-step workflow:**

```text
1. Open the project at ~/Projects/LEDBoard/LEDBoard.kicad_pcb
2. Check Freerouting dependencies are installed
3. Run autoroute with max 10 passes
4. Run DRC to verify the autorouted result
5. Export Gerbers to the fabrication folder
```

**Manual DSN/SES workflow** (for advanced users or external autorouters):

```text
Export the board to Specctra DSN format.
# ... run Freerouting GUI or another autorouter externally ...
Import the routed SES file from ~/Projects/LEDBoard/LEDBoard.ses
```

### Design Verification

```text
Set design rules with 0.15mm clearance and 0.2mm minimum track width.
Run a design rule check and show me any violations.
Export Gerber files to the 'fabrication' folder.
```

### Using Resources

Resources provide read-only access to project state:

- `kicad://project/current/info` - Project metadata
- `kicad://project/current/board` - Board properties
- `kicad://project/current/components` - Component list (JSON)
- `kicad://project/current/nets` - Electrical nets
- `kicad://project/current/layers` - Layer stack configuration
- `kicad://project/current/design-rules` - Current DRC settings
- `kicad://project/current/drc-report` - Design rule violations
- `kicad://board/preview.png` - Board visualization (PNG)

```text
Show me the current component list.
What are the current design rules?
Display the board preview.
List all electrical nets.
```

### JLCPCB Component Selection

**Finding Components with Local Libraries:**

```text
Search for ESP32 modules in JLCPCB libraries.
Find a 10k resistor in 0603 package from installed libraries.
Show me details for LCSC part C2934196.
```

**Optimizing Costs with JLCPCB API:**

```text
Search for 10k ohm resistors in 0603 package, only Basic parts.
Find the cheapest capacitor 10uF 25V in 0805 package with good stock.
Show me pricing and stock for JLCPCB part C25804.
Suggest cheaper alternatives to C25804.
```

**Complete Design Workflow:**

```text
I'm designing a board with an ESP32 and need to select components for JLCPCB assembly.
Search JLCPCB for ESP32-C3 modules.
Find Basic parts for: 10k resistor 0603, 100nF capacitor 0603, LED 0805.
For each component, show me the cheapest option with good stock availability.
Place these components on my board using the suggested footprints.
```

**Database Management:**

```text
Download the JLCPCB parts database (first time setup).
Show me JLCPCB database statistics.
How many Basic parts are available?
```

## Architecture

### MCP Protocol Layer

- **JSON-RPC 2.0 Transport:** Bi-directional communication via STDIO
- **Protocol Version:** MCP 2025-06-18
- **Capabilities:** Tools (125), Resources (8)
- **Error Handling:** Standard JSON-RPC error codes

### TypeScript Server (`src/`)

- Implements MCP protocol specification
- Manages Python subprocess lifecycle
- Handles message routing and validation
- Provides logging and error recovery
- All tools registered directly as MCP tools (one file per category in `src/tools/`)

### Python Interface (`python/`)

- **kicad_interface.py:** Main entry point, MCP message handler, command routing
- **kicad_api/:** IPC backend implementation
  - `base.py` - Abstract base classes (`KiCADBackend` / `BoardAPI`)
  - `ipc_backend.py` - KiCAD IPC API backend (real-time UI sync)
  - (The SWIG path is not a backend object — it is direct `pcbnew` access
    behind `KiCADInterface.command_routes` in `kicad_interface.py`.)
- **resources/resource_definitions.py:** Resource handlers and URIs
- **handlers/:** Thin per-category dispatch modules (one `handle_<command>` per tool), bridging `kicad_interface.py` to the command implementations
- **commands/:** Modular command implementations (highlights)
  - `project.py` - Project operations
  - `board/` - Board manipulation, 2D views
  - `component/` - Component placement and auto-placement
  - `routing/` - Trace routing, grid A\* smart routing, net length reports
  - `design_rules.py` - DRC operations
  - `export/` - Gerber, PDF, 3D, BOM, netlist generation
  - `schematic.py`, `wire_manager/`, `hierarchy_sheet.py` - Schematic authoring
  - `simulation.py` - ngspice SPICE simulation
  - `library.py` / `library_symbol/` - Footprint and symbol library search
  - `jlcpcb_parts.py`, `bom_check.py` - JLCPCB parts database and BOM availability

### KiCAD Integration

- **pcbnew API (SWIG):** Direct Python bindings to KiCAD for file operations
- **IPC API (kipy):** Real-time communication with running KiCAD instance (experimental)
- **Hybrid Backend:** Automatically uses IPC when available, falls back to SWIG
- **kicad-skip:** Schematic file manipulation
- **Platform Detection:** Cross-platform path handling
- **UI Management:** Automatic KiCAD UI launch/detection

## Development

### Building from Source

```bash
# Install dependencies
npm install
pip3 install -r requirements.txt

# Build TypeScript
npm run build

# Watch mode for development
npm run dev
```

### Running Tests

```bash
# TypeScript tests
npm run test:ts

# Python tests
npm run test:py

# All tests with coverage
npm run test:coverage
```

### Linting and Formatting

```bash
# Lint TypeScript and Python
npm run lint

# Format code
npm run format
```

## Troubleshooting

### Server Not Appearing in Client

**Symptoms:** MCP server doesn't show up in Claude Desktop or Cline

**Solutions:**

1. Verify build completed: `ls dist/index.js`
2. Check configuration paths are absolute
3. Restart MCP client completely
4. Check client logs for error messages

### Python Module Import Errors

**Symptoms:** `ModuleNotFoundError: No module named 'pcbnew'`

**Solutions:**

1. Verify KiCAD installation: `python3 -c "import pcbnew"`
2. Check PYTHONPATH in configuration matches your KiCAD installation
3. Ensure KiCAD was installed with Python support

### Tool Execution Failures

**Symptoms:** Tools fail with unclear errors

**Solutions:**

1. Check server logs: `~/.kicad-mcp/logs/kicad_interface.log`
2. Verify a project is loaded before running board operations
3. Ensure file paths are absolute, not relative
4. Check tool parameter types match schema requirements

### Windows-Specific Issues

**Symptoms:** Server fails to start on Windows

**Solutions:**

1. Run automated diagnostics: `.\setup-windows.ps1`
2. Verify Python path uses double backslashes: `C:\\Program Files\\KiCad\\10.0`
3. Check Windows Event Viewer for Node.js errors

### Getting Help

1. Check the [GitHub Issues](https://github.com/tiancode/KiCAD-MCP-Server/issues)
2. Review server logs: `~/.kicad-mcp/logs/kicad_interface.log`
3. Open a new issue with:
   - Operating system and version
   - KiCAD version (`python3 -c "import pcbnew; print(pcbnew.GetBuildVersion())"`)
   - Node.js version (`node --version`)
   - Full error message and stack trace
   - Relevant log excerpts

## Project Status

**Current Version:** 2.2.3

See [CHANGELOG.md](CHANGELOG.md) for detailed release notes.

**Working Features (125 tools):**

- Project management with snapshot checkpointing
- Complete board design (outline, layers, zones, mounting holes, text, SVG logos)
- Component placement with arrays, alignment, and duplication
- Advanced routing (pad-to-pad with auto-via, differential pairs, pattern copying)
- Complete schematic workflow with dynamic symbol loading (~10,000 symbols)
- Intelligent wiring system with pin discovery and smart routing
- FFC/ribbon cable passthrough workflow
- Schematic-to-board synchronization
- Design rule checking (DRC and ERC)
- Export to Gerber, PDF, SVG, 3D, BOM, netlist, position file
- Custom footprint and symbol creation
- JLCPCB parts integration (2.5M+ parts catalog)
- Datasheet enrichment via LCSC
- Freerouting autorouter integration (Java, Docker, Podman)
- UI auto-launch and management
- Full MCP 2025-06-18 protocol compliance

**IPC Backend (Experimental):**

- Real-time UI synchronization via the KiCAD IPC API
- 21 IPC-enabled commands with automatic SWIG fallback
- Hybrid footprint loading (SWIG for library access, IPC for placement)

**Developer Mode:**
Set `KICAD_MCP_DEV=1` to capture MCP session logs for debugging. See CHANGELOG v2.2.3 for details.

## What Do You Want to See Next?

We are actively developing new features. Your feedback directly shapes development priorities.

**Share your ideas:**

1. [Open a feature request](https://github.com/tiancode/KiCAD-MCP-Server/issues/new?labels=enhancement&template=feature_request.md)
2. [Join the discussion](https://github.com/tiancode/KiCAD-MCP-Server/discussions)
3. Star the repo if you find it useful

## Contributing

Contributions are welcome! Please follow these guidelines:

1. **Report Bugs:** Open an issue with reproduction steps
2. **Suggest Features:** Describe use case and expected behavior
3. **Submit Pull Requests:**
   - Fork the repository
   - Create a feature branch
   - Follow existing code style
   - Add tests for new functionality
   - Update documentation
   - Submit PR with clear description

See [CONTRIBUTING.md](CONTRIBUTING.md) for detailed guidelines.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

## Acknowledgments

- Originally forked from [mixelpixx/KiCAD-MCP-Server](https://github.com/mixelpixx/KiCAD-MCP-Server) by [@mixelpixx](https://github.com/mixelpixx) (MIT). This project has since diverged and is maintained independently, but the original work made it possible.
- Built on the [Model Context Protocol](https://modelcontextprotocol.io/) by Anthropic
- Powered by [KiCAD](https://www.kicad.org/) open-source PCB design software
- Uses [kicad-skip](https://github.com/kicad-skip) for schematic manipulation
- [JLCSearch API](https://jlcsearch.tscircuit.com/) by [@tscircuit](https://github.com/tscircuit/jlcsearch) - Public JLCPCB parts API
- [JLCParts Database](https://github.com/yaqwsx/jlcparts) by [@yaqwsx](https://github.com/yaqwsx) - JLCPCB parts data

### Contributors to the original project

These contributions were made to the upstream [mixelpixx/KiCAD-MCP-Server](https://github.com/mixelpixx/KiCAD-MCP-Server) and live on in this fork:

- [@Kletternaut](https://github.com/Kletternaut) - Routing/component tools, footprint/symbol creators, passthrough workflow, template fixes (PRs #44, #48, #49, #51, #53, #57, #59)
- [@Mehanik](https://github.com/Mehanik) - Schematic inspection/editing tools, component field positions (PRs #60, #66, #67)
- [@jflaflamme](https://github.com/jflaflamme) - Freerouting autorouter integration with Docker/Podman support (PR #68)
- [@l3wi](https://github.com/l3wi) - Local symbol library search, JLCPCB third-party library support (PR #25)
- [@gwall-ceres](https://github.com/gwall-ceres) - MCP protocol compliance, Windows compatibility (PR #10)
- [@fariouche](https://github.com/fariouche) - Bug fixes (PR #17)
- [@shuofengzhang](https://github.com/shuofengzhang) - XDG relative path handling (PR #58)
- [@sid115](https://github.com/sid115) - Windows setup script improvements (PR #13)
- [@pasrom](https://github.com/pasrom) - MCP server bug fixes (PR #50)

## Citation

If you use this project in your research or publication, please cite:

```bibtex
@software{kicad_mcp_server,
  title = {KiCAD MCP Server: AI-Assisted PCB Design},
  author = {tiancode and mixelpixx},
  year = {2026},
  url = {https://github.com/tiancode/KiCAD-MCP-Server},
  note = {Independently maintained fork of mixelpixx/KiCAD-MCP-Server},
  version = {2.2.3}
}
```
