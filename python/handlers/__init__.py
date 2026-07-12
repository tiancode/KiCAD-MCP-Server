"""
Per-domain MCP command handlers.

Each submodule exposes one or more `handle_*` functions of the form

    def handle_<command>(iface: "KiCADInterface", params: dict) -> dict

`kicad_interface.KiCADInterface` instantiates each domain and routes
commands via thin trampolines, keeping the main module focused on
lifecycle (board reference, auto-save guard, backend selection, IPC
recovery) rather than per-tool logic.

This split exists because the dispatcher used to be a 6000-line file
that mixed every tool's logic with every piece of lifecycle code.
"""
