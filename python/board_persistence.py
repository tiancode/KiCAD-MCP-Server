"""Board auto-save / on-disk-signature tracking, extracted from
``kicad_interface.py``.

``BoardPersistenceMixin`` is mixed into ``KiCADInterface``; the methods run with
``self`` bound to the interface instance, so they keep using ``self.board``,
``self._board_disk_signature`` (initialised in ``KiCADInterface.__init__``),
``self._is_board_healthy`` / ``self._safe_load_board`` /
``self._update_command_handlers``, etc. exactly as before. Pulling them here
keeps the SWIG-path persistence concern in one cohesive place instead of
scattered through the 2700-line interface module.
"""

import hashlib
import logging
import os
import shutil
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

# Use the same log channel as kicad_interface so existing log output is
# unchanged.
logger = logging.getLogger("kicad_interface")


class BoardPersistenceMixin:
    """SWIG-path board auto-save + on-disk content-signature tracking."""

    @staticmethod
    def _disk_signature(path: str) -> Optional[Tuple[int, str]]:
        """Return (mtime_ns, sha256_hex) for the file, or None if missing/unreadable.

        The sha256 is always recomputed from disk: the conflict guard in
        ``_auto_save_board`` compares hashes (content), not mtime, so we
        cannot use mtime as a cache key without re-introducing the bug
        where two writes inside one mtime tick on a coarse-resolution
        filesystem (FAT32, network mounts, etc.) would mask a real
        content change.
        """
        try:
            st = os.stat(path)
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            return (st.st_mtime_ns, h.hexdigest())
        except OSError:
            return None

    def _record_board_signature(self, path: Optional[str] = None) -> None:
        """Record the current on-disk signature of the board file.

        Call this after a fresh load (open_project / create_project) or after
        any save we perform ourselves, so that _auto_save_board() can detect
        when an external actor has modified the file in between.

        Handlers that write the board file directly (board.Save, kicad-cli
        subprocess) MUST call this with the file's path before returning —
        otherwise the next mutation will see the post-write hash and refuse
        the auto-save thinking the file was touched externally.
        """
        if path is None:
            if not self.board:
                self._board_disk_signature = None
                return
            try:
                path = self.board.GetFileName()
            except Exception:
                path = None
        self._board_disk_signature = self._disk_signature(path) if path else None

    def _save_board_and_record(self, board: Any, board_path: str) -> None:
        """Save a SWIG board to disk and align the in-memory signature.

        Single choke-point for handlers that need to persist board changes
        outside the dispatcher's auto-save flow. Without recording the new
        signature after the write, the dispatcher's follow-up auto-save
        sees a hash mismatch and refuses with a false 'changed externally'
        warning — the exact bug that broke add_board_outline →
        sync_schematic_to_board → place_component chains.
        """
        board.Save(board_path)
        self._record_board_signature(board_path)

    def _prune_auto_save_backups(self, backup_dir: str, base_name: str) -> None:
        """Keep only the most recent `_auto_save_backup_keep` backups for `base_name`."""
        try:
            entries = [
                os.path.join(backup_dir, f)
                for f in os.listdir(backup_dir)
                if f.startswith(base_name + ".")
            ]
            entries.sort(key=os.path.getmtime, reverse=True)
            for old in entries[self._auto_save_backup_keep :]:
                try:
                    os.remove(old)
                except OSError:
                    pass
        except OSError as e:
            logger.debug(f"Backup pruning skipped: {e}")

    def _auto_save_board(self) -> Dict[str, Any]:
        """Save the in-memory board to disk after a SWIG-path mutation.

        Behaviour:
          * If the file's on-disk signature has diverged from the one we
            recorded at load (or at our last successful save), refuse to
            overwrite — an external actor (KiCad GUI, another process, git)
            has touched the file and saving would clobber their changes.
          * Otherwise, copy the existing file to ``<dir>/.mcp-backups/<name>.<ts>``
            (rotating, keeps the most recent `_auto_save_backup_keep`),
            then call pcbnew.SaveBoard().
          * Update the recorded signature on success.
          * If SaveBoard leaves the in-memory BOARD dehydrated (observed on
            KiCAD nightlies after delete_trace + auto-save), reload from disk
            so the next command sees a usable proxy instead of a SwigPyObject.

        Returns a status dict that handle_command merges into the caller's
        response so warnings about refused saves are visible:
          {"saved": True,  "boardPath": ..., "backup": <path-or-None>}
          {"saved": False, "skipped": <reason>}                      -- nothing to save
          {"saved": False, "warning": ..., "diskChangedExternally": True, ...}
          {"saved": False, "error": ...}                             -- pcbnew error
        """
        # Read pcbnew through the kicad_interface module namespace (lazily, at
        # call time): that owns the KiCAD-path setup before importing pcbnew
        # — avoiding a module-load import-ordering hazard here — and it is the
        # attribute tests patch via patch("kicad_interface.pcbnew").
        import kicad_interface

        if not self.board:
            return {"saved": False, "skipped": "no board loaded"}

        try:
            board_path = self.board.GetFileName()
        except Exception as e:
            return {"saved": False, "skipped": f"GetFileName failed: {e}"}

        if not board_path:
            return {"saved": False, "skipped": "no board path"}

        expected = self._board_disk_signature
        current = self._disk_signature(board_path)

        # Only refuse if the file's CONTENT (sha256) has actually diverged
        # from what we recorded. mtime alone is not a conflict signal —
        # `touch`, atime-driven backups, or even some MCP read paths can
        # advance mtime without changing content, and refusing on that
        # basis traps users in a state where every write needs an explicit
        # save_project workaround.
        #
        # If expected is None, treat this as "first save" and proceed —
        # otherwise pre-existing setups (open_project ran before this guard
        # was introduced) would never be able to save.
        if expected is not None and current is not None and expected[1] != current[1]:
            warning = (
                "Auto-save refused: the on-disk PCB file's contents changed "
                "externally since this MCP session loaded it. To avoid "
                "clobbering those changes, the in-memory mutation has NOT "
                "been written to disk. Reload via open_project to refresh, "
                "then re-apply the change."
            )
            logger.warning(f"{warning} ({board_path})")
            logger.warning(f"  expected sha256={expected[1][:12]}… mtime_ns={expected[0]}")
            logger.warning(f"  current  sha256={current[1][:12]}… mtime_ns={current[0]}")
            return {
                "saved": False,
                "warning": warning,
                "boardPath": board_path,
                "diskChangedExternally": True,
                "expectedMtimeNs": expected[0],
                "currentMtimeNs": current[0],
                "memChangesUnsaved": True,
            }

        # Content matches but mtime advanced (e.g. external `touch`): refresh
        # the recorded mtime so we don't re-hash on every subsequent call.
        if expected is not None and current is not None and expected != current:
            self._board_disk_signature = current

        # Make a rotating backup of the existing file (best-effort).
        backup_path: Optional[str] = None
        if current is not None:
            try:
                backup_dir = os.path.join(os.path.dirname(board_path) or ".", ".mcp-backups")
                os.makedirs(backup_dir, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
                base = os.path.basename(board_path)
                backup_path = os.path.join(backup_dir, f"{base}.{stamp}")
                shutil.copy2(board_path, backup_path)
                self._prune_auto_save_backups(backup_dir, base)
            except OSError as e:
                logger.warning(f"Auto-save backup failed (continuing): {e}")
                backup_path = None

        # Write the board.
        try:
            kicad_interface.pcbnew.SaveBoard(board_path, self.board)
            logger.debug(f"Auto-saved board to: {board_path}")
            self._board_disk_signature = self._disk_signature(board_path)
        except Exception as e:
            logger.warning(f"Auto-save failed: {e}")
            return {"saved": False, "error": str(e), "backup": backup_path}

        # Post-save dehydration check. If the BOARD lost its bindings during
        # save, reload from disk while we still know the path. board_path is
        # guaranteed non-empty here (we returned early above otherwise).
        if not self._is_board_healthy():
            logger.warning(
                "Board became dehydrated during auto-save; reloading from %s",
                board_path,
            )
            recovered = self._safe_load_board(board_path)
            if recovered is not None:
                self.board = recovered
                self._update_command_handlers()
            else:
                logger.error(
                    "Board dehydration after auto-save is unrecoverable — "
                    "subsequent commands will fail until MCP restart"
                )

        return {"saved": True, "boardPath": board_path, "backup": backup_path}
