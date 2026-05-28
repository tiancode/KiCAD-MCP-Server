# KiCAD MCP Server Architecture

This document describes the system architecture for contributors who want to understand, modify, or extend the server.

---

## System Overview

```
AI Assistant (Claude, etc.)
        |
        | MCP Protocol (JSON-RPC 2.0 over STDIO)
        v
  TypeScript MCP Server (src/)
        |
        | Spawn Python subprocess, pass JSON commands
        v
  Python KiCAD Interface (python/)
        |
        | pcbnew SWIG API or KiCAD IPC API
        v
    KiCAD 9.0+
```

The server has two layers:

1. **TypeScript layer** -- implements the MCP protocol, registers tools with schemas, validates input, manages the Python subprocess
2. **Python layer** -- interfaces with KiCAD's pcbnew API (SWIG bindings) or IPC API for actual PCB/schematic operations

---

## Directory Structure

```
KiCAD-MCP-Server/
  src/                          # TypeScript MCP server
    server.ts                   # Server lifecycle, queue, READY/warm-up handshake
    index.ts                    # Entry point — wires together server + config
    config.ts                   # Config loader (LOG_LEVEL, optional --config file)
    logger.ts                   # Logging
    tools/                      # MCP tool registrations (one file per category)
      project.ts, board.ts, component.ts, routing.ts, design-rules.ts,
      export.ts, schematic.ts, library.ts, library-symbol.ts,
      footprint.ts, symbol-creator.ts, datasheet.ts, jlcpcb-api.ts,
      freerouting.ts, ui.ts     # Each calls server.tool(...) for its commands
    resources/                  # kicad:// resource handlers
    prompts/                    # MCP prompt templates

  python/
    kicad_interface.py          # ~2 800-line dispatcher: command_routes,
                                # _HANDLER_MAP, __getattr__ shim, auto-save,
                                # SWIG dehydration recovery, IPC reconnect
    handlers/                   # Per-tool handler implementations
      __init__.py               # Calling convention docstring
      ui.py                     # check_kicad_ui, launch_kicad_ui,
                                # get_backend_info, get_backend_state
      project.py                # open / create / snapshot_project
      board.py                  # place_component, import_svg_logo
      footprint.py              # 4 custom-footprint handlers
      symbol_creator.py         # 4 custom-symbol handlers
      jlcpcb.py                 # 5 JLCPCB DB handlers
      datasheet.py              # enrich_datasheets, get_datasheet_url
      ipc.py                    # 7 ipc_* real-time IPC handlers
      routing.py                # refill_zones (only non-trivial routing handler)
      schematic_component.py    # 9 component CRUD
      schematic_wire.py         # 10 wire/label/connection handlers
      schematic_query.py        # 13 list_/get_/find_ handlers
      schematic_io.py           # 8 IO/export/erc/netlist/sync handlers
      schematic_view.py         # 8 view/analysis handlers
    commands/                   # Lower-level command classes (BoardCommands,
                                # ComponentCommands, RoutingCommands, …) plus
                                # pcbnew helpers (wire_manager, pin_locator,
                                # dynamic_symbol_loader, freerouting, jlcpcb,
                                # jlcsearch, datasheet_manager, …).  Most
                                # handlers/<m>.py modules delegate to these.
    kicad_api/                  # IPC backend
      base.py                   # KiCADBackend + BoardAPI abstract bases
      ipc_backend.py            # kipy IPC client (KiCAD 9.0+ / 10.0+)
                                # (SWIG path is direct pcbnew via
                                #  command_routes, not a backend object)
    schemas/tool_schemas.py     # JSON Schema definitions for every tool
    annotations/                # IPC-annotation loader for tool descriptions
    resources/                  # Resource read handlers
    templates/                  # Pre-built schematic / project templates
    parsers/                    # KiCAD file format parsers (kicad_mod)
    utils/                      # platform_helper, kicad_process

  tests/                        # Flat test layout; pytest discovers test_*.py
    conftest.py                 # pcbnew + skip MagicMock stubbing
    fixtures/                   # .kicad_sym fixtures
    test_*.py                   # ~80 test files

  scripts/
    swig_smoke_test.py          # End-to-end against real pcbnew
    download_jlcpcb.py          # JLCPCB parts DB downloader
    install-linux.sh, auto_refresh_kicad.sh, generate_tool_annotations.py

  docs/                         # Documentation
  config/                       # Configuration examples
```

---

## TypeScript Layer

### Server Startup (`src/server.ts`)

1. Creates an MCP server instance
2. Registers all tools from each tool file (registerProjectTools, registerBoardTools, etc.)
3. Registers resources and prompts
4. Starts the STDIO transport for MCP communication
5. On first tool call, spawns the Python subprocess

### Tool Registration

Each tool file exports a `register*Tools(server, callKicadScript)` function that:

- Defines tool name, description, and Zod schema for parameters
- Registers a handler that calls `callKicadScript(command, args)`

Example from `src/tools/project.ts`:

```typescript
server.tool(
  "create_project",
  "Create a new KiCAD project",
  { name: z.string(), path: z.string() },
  async (args) => {
    const result = await callKicadScript("create_project", args);
    return { content: [{ type: "text", text: JSON.stringify(result) }] };
  },
);
```

### Tool Registration

Every tool is registered directly as an MCP tool — one registrar function per
file in `src/tools/`, all wired up in `KiCADMcpServer.registerAll` (`server.ts`).
Clients receive the full tool list and call tools by name; there is no
router/registry indirection or `execute_tool` gateway.

### Python Subprocess Communication

`callKicadScript(command, args)` in `server.ts`:

1. Spawns `python3 python/kicad_interface.py` (if not already running)
2. Sends a JSON message: `{"command": "...", "params": {...}}`
3. Reads the JSON response
4. Returns the result to the MCP tool handler

---

## Python Layer

### Main Entry Point (`python/kicad_interface.py`)

- Reads JSON commands from stdin
- Routes commands to the appropriate handler
- Manages the pcbnew board object lifecycle
- Handles backend selection (SWIG vs IPC)
- Auto-saves after board-modifying operations

### Command Routing — `_HANDLER_MAP` + `__getattr__`

The dispatcher used to carry 81 inline `_handle_<command>` methods that
imported the matching `handlers/<module>.py` and forwarded. That's
collapsed into a single `__getattr__` shim driven by a `_HANDLER_MAP`
class attribute on `KiCADInterface`:

```python
class KiCADInterface:
    _HANDLER_MAP: Dict[str, str] = {
        "check_kicad_ui": "ui",
        "place_component": "board",
        "add_schematic_wire": "schematic_wire",
        # … one entry per MCP command
    }

    def __getattr__(self, name):
        if name.startswith("_handle_"):
            cmd = name[len("_handle_"):]
            module_name = type(self)._HANDLER_MAP.get(cmd)
            if module_name is not None:
                module = importlib.import_module(f"handlers.{module_name}")
                handler = getattr(module, f"handle_{cmd}")
                return lambda params, _h=handler: _h(self, params)
        raise AttributeError(name)
```

Tests that call `iface._handle_check_kicad_ui({})` continue to work
through `__getattr__`. Each handler module exposes free functions of
the form `handle_<command>(iface, params) -> dict`; the `iface`
parameter gives them access to shared state (`iface.board`,
`iface.ipc_board_api`, `iface._safe_load_board`, …).

### Backend System (`python/kicad_api/`)

Two ways of interacting with KiCAD:

**SWIG path** (default):

- Direct Python bindings to KiCAD's C++ API via `pcbnew`
- Operates on files -- loads .kicad_pcb, modifies in memory, saves back
- Works without KiCAD running
- Requires manual UI reload to see changes
- Dispatched directly through `KiCADInterface.command_routes` →
  `commands/*.py`; it is **not** wrapped in a backend object

**IPC Backend** (`ipc_backend.py`):

- Communicates with running KiCAD via IPC API socket (kipy)
- Changes appear in the UI immediately
- Requires KiCAD 9.0+ running with IPC enabled
- Falls back to the SWIG path when unavailable

Backend selection happens in `kicad_interface.py` at import time: it tries to
attach the IPC backend first (honoring `KICAD_BACKEND`), otherwise commands run
against `pcbnew` directly.

### Schematic System

Schematic manipulation uses a different stack than PCB operations:

- **kicad-skip** library for reading/modifying schematic files
- **S-expression parsing** for direct file manipulation (wires, symbols)
- **DynamicSymbolLoader** for injecting any KiCad symbol into a schematic
- **WireManager** for creating wires via S-expression injection
- **PinLocator** for discovering pin positions with rotation support

---

## Adding a New Tool

### Step 1: Define the TypeScript Schema

Create or edit a file in `src/tools/`. Register the tool with `server.tool()`:

```typescript
server.tool(
  "my_new_tool",
  "Description of what the tool does",
  {
    param1: z.string().describe("Description of param1"),
    param2: z.number().optional().describe("Optional param2"),
  },
  async (args) => {
    const result = await callKicadScript("my_new_tool", args);
    return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
  },
);
```

### Step 2: Import in server.ts

Import and call the registration function in `src/server.ts`:

```typescript
import { registerMyTools } from "./tools/my-tools.js";
registerMyTools(server, callKicadScript);
```

### Step 3: Implement the Python Handler

Pick the right handler module in `python/handlers/<module>.py` (or
create a new one). Add a free function with the standard signature:

```python
def handle_my_new_tool(iface: "KiCADInterface", params: Dict[str, Any]) -> Dict[str, Any]:
    # Implementation using iface.board / iface.ipc_board_api / pcbnew /
    # the appropriate commands.* module.
    return {"success": True, "message": "Done", "data": result}
```

Then register the routing in `python/kicad_interface.py` by adding an
entry to `_HANDLER_MAP` and to `command_routes`:

```python
class KiCADInterface:
    _HANDLER_MAP: Dict[str, str] = {
        # …
        "my_new_tool": "my_module",
    }

    # In __init__, command_routes table:
    self.command_routes = {
        # …
        "my_new_tool": self._handle_my_new_tool,
    }
```

`self._handle_my_new_tool` materialises via `__getattr__` — no
trampoline method to write.

### Step 4: Build and Test

```bash
npm run build          # Compile TypeScript
npm run test:py        # Run Python tests
```

---

## Testing

### Python Tests

Located in `python/tests/`. Run with:

```bash
pytest python/tests/ -v
```

Key test files:

- `test_schematic_tools.py` -- schematic tool tests
- `test_freerouting.py` -- autorouter tests
- `test_delete_schematic_component.py` -- component deletion tests
- `test_schematic_component_fields.py` -- field inspection tests
- `test_platform_helper.py` -- platform detection tests

### Manual Testing

1. Build the server: `npm run build`
2. Configure in Claude Desktop or Claude Code
3. Test tools interactively through your MCP client

---

## Key Design Decisions

- **TypeScript + Python split**: TypeScript handles MCP protocol (well-supported SDK), Python handles KiCAD (only available API)
- **Auto-save**: Every board-modifying SWIG operation auto-saves to prevent data loss
- **Dynamic symbol loading**: Works around kicad-skip's inability to create symbols from scratch
- **S-expression wire injection**: Works around kicad-skip's inability to create wires

---

## Source Files Reference

| File                                       | Purpose                             |
| ------------------------------------------ | ----------------------------------- |
| `src/server.ts`                            | MCP server, subprocess management   |
| `python/kicad_interface.py`                | Python entry point, command routing |
| `python/commands/dynamic_symbol_loader.py` | Symbol injection system             |
| `python/commands/wire_manager.py`          | Wire creation engine                |
| `python/commands/pin_locator.py`           | Pin position discovery              |
