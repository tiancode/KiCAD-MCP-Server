"""Component commands package.

Re-exports ComponentCommands so existing
``from commands.component import ComponentCommands`` imports keep working
after the split from a single module into this package.
"""

from ._core import ComponentCommands

__all__ = ["ComponentCommands"]
