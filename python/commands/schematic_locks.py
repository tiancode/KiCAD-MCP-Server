"""Serialization + atomic writes for ``.kicad_sch`` mutations.

Every schematic writer in this package does a read-modify-write on the
same file (kicad-skip ``write()``, the ``sexpdata`` round-trips in
``wire_manager`` / ``schematic_component``, and the raw text insertion in
``dynamic_symbol_loader``).  Without coordination, two operations that
touch the same path race:

* **Lost updates** — both read version V0, both write their own edit, the
  last writer wins and the other edit vanishes.
* **Partial reads / catastrophic nesting** — a reader (or the next
  writer) opens the file mid-write and sees a truncated tree; writing
  that back can stuff a whole ``(kicad_sch ...)`` into the previous
  file's ``lib_symbols`` block, exploding it to hundreds of nested
  roots.  This is the "file balloons to 564 KB / 497 roots" failure.

Two primitives close the gap:

* :func:`schematic_path_lock` — a process-wide, per-path reentrant lock.
  Wrap an entire read-modify-write in it so concurrent operations on the
  same file run one at a time.
* :func:`atomic_write_text` — write to a temp file in the same directory
  then ``os.replace`` it into place.  ``os.replace`` is atomic on a
  single filesystem, so a concurrent reader always sees either the whole
  old file or the whole new one — never a half-written one.

The lock is in-process only (a ``threading.RLock``); it does **not**
guard against a *different* process (e.g. the KiCad UI) writing the same
file — that cross-process case is the job of the backend reconcile gate.
"""

from __future__ import annotations

import functools
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Union

_PathLike = Union[str, Path]

# Registry of per-path locks.  Guarded by ``_registry_guard`` so two
# threads asking for the same path's lock for the first time get the
# same object.
_registry_guard = threading.Lock()
_locks: Dict[str, "threading.RLock"] = {}


def _canonical_key(path: _PathLike) -> str:
    """Normalize a path so different spellings of the same file share a lock."""
    try:
        return os.path.realpath(os.fspath(path))
    except (OSError, ValueError, TypeError):
        return str(path)


@contextmanager
def schematic_path_lock(path: _PathLike) -> Iterator[None]:
    """Hold the process-wide lock for ``path`` for the duration of the block.

    Reentrant: the same thread may acquire it again (e.g. a high-level
    ``add_component`` that itself calls ``inject`` then
    ``create_instance``) without deadlocking.
    """
    key = _canonical_key(path)
    with _registry_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
    with lock:
        yield


def serialize_on_path(arg_index: int) -> Callable:
    """Decorator: hold :func:`schematic_path_lock` on a positional arg for the call.

    ``arg_index`` is the position of the ``.kicad_sch`` path in the
    wrapped callable's argument list — 0 for a ``@staticmethod`` whose
    first parameter is the path, 1 for an instance method (after
    ``self``).  When the path can't be located the call runs unlocked
    (no worse than before), so the decorator never breaks an unusual
    call shape.
    """

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            path = None
            if len(args) > arg_index:
                path = args[arg_index]
            if path is None:
                return fn(*args, **kwargs)
            with schematic_path_lock(path):
                return fn(*args, **kwargs)

        return wrapper

    return decorator


def serialize_on_param(param_key: str) -> Callable:
    """Decorator for ``handle_*(iface, params)`` tools that mutate a schematic.

    Holds :func:`schematic_path_lock` on ``params[param_key]`` (typically
    ``"schematicPath"``) for the whole call, so a handler's read-modify-write
    runs atomically with respect to other schematic writers.  When the
    param is absent the call runs unlocked.
    """

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(iface: Any, params: Any = None, *args: Any, **kwargs: Any) -> Any:
            path = None
            if isinstance(params, dict):
                path = params.get(param_key)
            if path is None:
                return fn(iface, params, *args, **kwargs)
            with schematic_path_lock(path):
                return fn(iface, params, *args, **kwargs)

        return wrapper

    return decorator


def atomic_write_text(
    path: _PathLike,
    content: str,
    *,
    encoding: str = "utf-8",
    newline: Union[str, None] = None,
) -> None:
    """Write ``content`` to ``path`` atomically (temp file + ``os.replace``).

    A crash or concurrent reader never observes a partially written file:
    the rename either happened (new content) or did not (old content).
    The temp file is created in the target's directory so the rename
    stays on one filesystem and is therefore atomic.
    """
    target = Path(path)
    directory = target.parent if str(target.parent) else Path(".")
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as handle:
            handle.write(content)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
