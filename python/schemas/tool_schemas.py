"""
Comprehensive tool schema definitions for all KiCAD MCP commands

Following MCP 2025-06-18 specification for tool definitions.
Each tool includes:
- name: Unique identifier
- title: Human-readable display name
- description: Detailed explanation of what the tool does
- inputSchema: JSON Schema for parameters
- outputSchema: Optional JSON Schema for return values (structured content)

The per-category ``*_TOOLS`` lists now live in the ``schemas.categories``
sub-package. This module re-imports them, re-exports each list (so existing
``from schemas.tool_schemas import SCHEMATIC_TOOLS`` imports keep working),
and aggregates them into the combined ``TOOL_SCHEMAS`` lookup.
"""

from typing import Any, Dict

from .categories.project import PROJECT_TOOLS
from .categories.board import BOARD_TOOLS
from .categories.component import COMPONENT_TOOLS
from .categories.routing import ROUTING_TOOLS
from .categories.library import LIBRARY_TOOLS
from .categories.design_rule import DESIGN_RULE_TOOLS
from .categories.export import EXPORT_TOOLS
from .categories.schematic import SCHEMATIC_TOOLS
from .categories.ui import UI_TOOLS

# =============================================================================
# COMBINED TOOL SCHEMAS
# =============================================================================

TOOL_SCHEMAS: Dict[str, Any] = {}

# Combine all tool categories
for tool in (
    PROJECT_TOOLS
    + BOARD_TOOLS
    + COMPONENT_TOOLS
    + ROUTING_TOOLS
    + LIBRARY_TOOLS
    + DESIGN_RULE_TOOLS
    + EXPORT_TOOLS
    + SCHEMATIC_TOOLS
    + UI_TOOLS
):
    TOOL_SCHEMAS[tool["name"]] = tool

__all__ = [
    "TOOL_SCHEMAS",
    "PROJECT_TOOLS",
    "BOARD_TOOLS",
    "COMPONENT_TOOLS",
    "ROUTING_TOOLS",
    "LIBRARY_TOOLS",
    "DESIGN_RULE_TOOLS",
    "EXPORT_TOOLS",
    "SCHEMATIC_TOOLS",
    "UI_TOOLS",
]
