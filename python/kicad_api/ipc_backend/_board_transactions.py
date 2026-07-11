"""IPCBoardAPI transaction + save/revert operations.

Split out of the former monolithic kicad_api/ipc_backend.py.
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("kicad_interface")


class _TransactionMixin:
    # Transaction state, declared for mypy across the package split (set on the
    # instance by begin_transaction / commit / rollback). Annotation-only.
    _current_commit: Any

    def begin_transaction(self, description: Optional[str] = None) -> Dict[str, Any]:
        """Open a transaction. Subsequent mutating calls fold into one undo step.

        Refuses to nest — a second begin without an intervening commit /
        rollback would leak the original commit handle and orphan the
        first batch of changes.  Callers should commit or rollback the
        existing transaction first.

        ``description`` of ``None`` (or key omitted) gets the default
        label.  An explicit empty string is preserved — KiCad will show
        a blank undo entry, but that's the caller's choice.

        Note: only mutations that go through ``_apply_create / update /
        remove`` participate.  Property mutations like ``set_origin`` and
        ``set_title_block_info`` are sent as direct kipy commands and are
        NOT part of the undo step (kipy treats them as out-of-band).
        """
        if self._current_commit is not None:
            return {
                "success": False,
                "message": (
                    "A transaction is already open — commit or rollback it "
                    "before starting a new one."
                ),
            }
        label = description if description is not None else self._DEFAULT_COMMIT_LABEL
        try:
            board = self._get_board()
            self._current_commit = board.begin_commit()
            self._current_commit_description = label
            logger.debug(f"Started transaction: {label}")
            return {"success": True, "description": label}
        except Exception as e:
            logger.error(f"Failed to begin transaction: {e}")
            return {"success": False, "message": str(e)}

    def commit_transaction(self, description: Optional[str] = None) -> Dict[str, Any]:
        """Push the open transaction as one undo step. ``description`` of
        ``None`` keeps the label set at ``begin_transaction``; an explicit
        empty string overrides to blank."""
        if self._current_commit is None:
            return {
                "success": False,
                "message": "No open transaction to commit.",
            }
        # Three-state precedence: explicit override (incl. "") > begin label > default.
        if description is not None:
            msg = description
        elif self._current_commit_description is not None:
            msg = self._current_commit_description
        else:
            msg = self._DEFAULT_COMMIT_LABEL
        try:
            board = self._get_board()
            board.push_commit(self._current_commit, msg)
            self._current_commit = None
            self._current_commit_description = None
            logger.debug(f"Committed transaction: {msg}")
            return {"success": True, "description": msg}
        except Exception as e:
            logger.error(f"Failed to commit transaction: {e}")
            # Leave _current_commit set — caller may want to retry or
            # rollback explicitly rather than us silently clearing state.
            return {"success": False, "message": str(e)}

    def rollback_transaction(self) -> Dict[str, Any]:
        """Drop the open transaction — everything done since begin is undone."""
        if self._current_commit is None:
            return {
                "success": False,
                "message": "No open transaction to roll back.",
            }
        try:
            board = self._get_board()
            board.drop_commit(self._current_commit)
            self._current_commit = None
            self._current_commit_description = None
            logger.debug("Rolled back transaction")
            return {"success": True}
        except Exception as e:
            logger.error(f"Failed to rollback transaction: {e}")
            return {"success": False, "message": str(e)}

    def get_transaction_status(self) -> Dict[str, Any]:
        """Whether a transaction is currently open and its description."""
        return {
            "success": True,
            "open": self._current_commit is not None,
            "description": self._current_commit_description,
        }

    def save(self) -> bool:
        """Save the board immediately."""
        try:
            board = self._get_board()
            board.save()
            self._notify("save", {})
            return True
        except Exception as e:
            logger.error(f"Failed to save board: {e}")
            return False

    def revert(self) -> bool:
        """Discard KiCad's in-memory board and reload it from the .kicad_pcb
        on disk (the IPC equivalent of File → Revert).

        Used by ``reconcile_backends(swig_to_ipc)`` to pull SWIG-written disk
        content into the running KiCad instance — the direction we long
        (wrongly) documented as impossible.  kipy *does* expose this via
        ``Board.revert()`` → ``RevertDocument`` (kicad-python ≥ 0.7, KiCad
        ≥ 10.0.1).

        WARNING: this throws away any *unsaved* IPC changes in KiCad memory,
        so callers must only invoke it when the IPC side is known clean
        (``_ipc_writes_pending`` is False).  We deliberately do NOT fire the
        change callback here: ``_on_ipc_change`` would mark the IPC side dirty
        for any non-``save`` event, but a revert leaves KiCad memory == disk.
        The reconcile handler resets the gate flags explicitly instead.
        """
        try:
            board = self._get_board()
            board.revert()
            # Drop the cached Board handle so the next query re-fetches against
            # the freshly-reloaded document.
            self._board = None
            return True
        except Exception as e:
            logger.error(f"Failed to revert board: {e}")
            return False
