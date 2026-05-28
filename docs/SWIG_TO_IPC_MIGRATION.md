# Migrating PCB operations from SWIG to IPC

A working playbook for moving PCB operations off the deprecated `pcbnew` SWIG
bindings onto the IPC API (`kipy`), **incrementally and safely**, one command at
a time, without ever losing a working fallback.

> Schematic editing is **not** part of this — it is file-based (kicad-skip +
> sexpdata, atomic writes to `.kicad_sch`) and touches neither SWIG nor IPC.
> This document is only about PCB (`.kicad_pcb`) operations.

## Why

- KiCad is deprecating the `pcbnew` SWIG Python bindings in favour of the IPC
  API (`kipy`). Eventually `import pcbnew` may stop working.
- In this server SWIG is currently **load-bearing**, not optional:
  - `src/server.ts` `validatePrerequisites` hard-gates startup on
    `python -c "import pcbnew"` — if pcbnew can't import, the server won't boot.
  - `sync_schematic_to_board` (F8) writes the board via `pcbnew.LoadBoard` /
    `board.Save()` (`python/handlers/schematic_io.py`).
  - The IPC fast-path only covers ~24 commands; the rest of the PCB surface runs
    through `pcbnew` (the SWIG `*_commands` classes).
- Goal: shift PCB operations onto IPC so a KiCad 10 install without pcbnew can
  eventually run this server.

## How the migration switch already works

The codebase is built for this — no new framework needed:

- `python/kicad_interface.py` → `IPC_CAPABLE_COMMANDS` maps
  `"<cmd>" -> "_ipc_<cmd>"`.
- `handle_command` dispatch logic: when a command is in `IPC_CAPABLE_COMMANDS`
  **and** `self.use_ipc` **and** `self.ipc_board_api` **and** a `.kicad_pcb` is
  open over IPC (`_ipc_has_open_board_document()`), it routes to the IPC
  handler; **otherwise it falls through to the SWIG `command_routes` handler.**
- IPC handlers live in `python/handlers/ipc_fastpath.py` as
  `handle_<cmd>(iface, params)`, reached through the `_ipc_<cmd>` trampoline in
  `KiCADInterface.__getattr__`.

**Therefore, migrating one command =**

1. write `handle_<cmd>` in `ipc_fastpath.py`, and
2. add `"<cmd>": "_ipc_<cmd>"` to `IPC_CAPABLE_COMMANDS`.

The SWIG handler in `command_routes` stays untouched as the automatic fallback.
If the IPC handler is missing, buggy, or IPC is unavailable, dispatch degrades
to SWIG — it never hard-fails.

## Scope: what actually needs migrating

The full SWIG-only list (commands with no IPC fast-path) is ~41, but most are
not live-board operations. Categorise before touching anything:

| Category | Commands | Target |
| --- | --- | --- |
| **Library / symbol lookups** — parse `fp-lib-table` / `sym-lib-table` / `.kicad_sym` files, never touch the board | `list_libraries`, `search_footprints`, `list_library_footprints`, `get_footprint_info`, `list_symbol_libraries`, `search_symbols`, `list_library_symbols`, `get_symbol_info`, `refresh_symbol_libraries` | **Do not migrate** — already headless/file-based |
| **Export / batch** — currently `pcbnew.PLOT_CONTROLLER` for gerbers | `export_gerber`, `export_pdf`, `export_svg`, `export_3d`, `export_bom` | **Move to `kicad-cli`** (subprocess), not IPC. `run_drc` already uses `kicad-cli` |
| **Freerouting** — subprocess + DSN/SES files (some touch the board) | `autoroute`, `export_dsn`, `import_ses`, `check_freerouting` | Low priority; mostly cli/file |
| **Live-board ops** — the real SWIG→IPC backlog (~19) | Component: `edit_component`, `duplicate_component`, `align_components`, `place_component_array`, `check_courtyard_overlaps`, `find_component`, `get_component_pads`, `get_pad_position`. Routing: `route_pad_to_pad`, `route_differential_pair`, `modify_trace`, `add_gnd_stitching_vias`, `copy_routing_pattern`, `query_zones`, `create_netclass`. Board: `add_layer`, `set_active_layer`, `get_board_extents`, `get_board_2d_view` | **IPC** |
| **Keystone** | `sync_schematic_to_board` (F8) | **IPC** — hardest, biggest payoff |

## Migration order (phases)

Migrate in this order so risk only rises after the easy wins are proven:

1. **Phase 1 — read-only IPC queries** (no mutation; verify by diffing output
   against the SWIG handler): `query_zones`, `get_board_extents`,
   `get_component_pads`, `get_pad_position`, `find_component`,
   `check_courtyard_overlaps`, `get_board_2d_view`.
2. **Phase 2 — simple mutations** (direct kipy primitives exist):
   `set_active_layer`, `add_layer`, `edit_component`, `route_pad_to_pad`.
3. **Phase 3 — composite mutations** (build from kipy primitives; confirm kipy
   support first): `align_components`, `duplicate_component`,
   `place_component_array`, `route_differential_pair`, `add_gnd_stitching_vias`,
   `copy_routing_pattern`, `create_netclass`.
4. **Phase 4 — `sync_schematic_to_board` via IPC (keystone).** This removes the
   `swig_to_ipc` cross-backend conflict (SWIG writing the `.kicad_pcb` while
   KiCad has it open over IPC). Requires kipy to add footprints + nets from a
   netlist; verify that capability before starting.
5. **Phase 5 — exports → `kicad-cli`, then relax the pcbnew gate.** Only after
   every board op + F8 has a validated IPC path and exports no longer need
   `pcbnew`, make `import pcbnew` optional in `src/server.ts`
   `validatePrerequisites` (e.g. allow an IPC-only startup mode).

## Per-command recipe

Do this for **one** command per change:

1. **Check kipy capability.** Confirm `kipy` 10 can actually perform the
   operation. If it can't, **stop** and leave the command on SWIG — do not force
   a half-working IPC path.
2. **Write the IPC handler.** Add `handle_<cmd>(iface, params)` to
   `python/handlers/ipc_fastpath.py`, using `iface.ipc_board_api`. Return the
   **same response shape** as the SWIG handler (read the matching
   `python/commands/<x>.py` method and mirror its keys) so callers can't tell
   the difference.
3. **Register it.** Add `"<cmd>": "_ipc_<cmd>"` to `IPC_CAPABLE_COMMANDS` in
   `python/kicad_interface.py`.
4. **Wire the conflict gate:**
   - Read-only command → add it to `_IPC_READ_ONLY_COMMANDS` so the
     cross-backend gate lets it through even when SWIG has pending writes.
   - Mutating command → make sure the board API fires `iface._on_ipc_change`
     (the existing IPCBoardAPI mutations already do) so `_ipc_writes_pending`
     stays accurate.
5. **Add a mocked unit test.** Follow the `_FakeIPCBoardAPI` /
   `IPCBoardAPI.__new__(...)` pattern in `tests/test_ipc_*.py` /
   `tests/test_backend_metadata.py`.
6. **Validate live.** Run against a real KiCad 10 with the PCB editor open
   (see the testing note below).
7. **Keep the SWIG handler.** Never delete it in the same change — it is the
   fallback.

## Safety rules

1. **Never delete a SWIG handler when adding its IPC counterpart.** A
   missing/broken IPC path must degrade to SWIG, not fail.
2. **One command per change**, independently revertible.
3. **Response parity** — IPC output keys match SWIG output keys.
4. **Gate on kipy capability** — skip what kipy 10 can't do; leave it on SWIG.
5. The existing cross-backend conflict gate
   (`_swig_writes_landed` / `_ipc_writes_pending` / `reconcile_backends`)
   keeps mixed usage safe — don't bypass it.

## Testing limitation (read this)

This repository's test suite **mocks `kipy`** — there is no real KiCad/kipy in
CI or the dev container. That means:

- You can write IPC handlers and mocked unit tests here, but mocks do **not**
  prove the handler works against real KiCad.
- **Every IPC handler must be validated on a running KiCad 10** with the PCB
  editor open before it is trusted.
- The safety of the whole migration rests on two things: the SWIG fallback stays
  in place, and each IPC handler is validated live before you rely on it.

## Exit criteria — when `pcbnew` can finally be dropped

All of the following must be true:

- Every command in the **Live-board ops** backlog has a validated IPC path (or a
  documented reason it stays on SWIG).
- `sync_schematic_to_board` (F8) works over IPC.
- Exports run through `kicad-cli`, not `pcbnew.PLOT_CONTROLLER`.

Only then relax the `import pcbnew` startup gate in `src/server.ts` so the
server can run on a KiCad 10 install without the SWIG bindings.
