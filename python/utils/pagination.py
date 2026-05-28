"""Shared list-pagination helper for MCP command/handler responses.

Many query commands return per-item lists (components, nets, symbols,
footprints, DRC violations, …) that can run to hundreds or thousands of
entries on a real design. Returning the whole list inflates every tool
response — a cost that accumulates across an agent loop. ``paginate`` caps
the returned slice and reports enough metadata (``total`` + ``truncated``)
that the caller knows there is more and can page through it with ``offset``.
"""

from typing import Any, Dict, List, Tuple

DEFAULT_LIMIT = 100


def paginate(
    items: List[Any], params: Dict[str, Any], *, default_limit: int = DEFAULT_LIMIT
) -> Tuple[List[Any], Dict[str, Any]]:
    """Slice ``items`` per ``limit``/``offset`` in ``params``.

    Returns ``(page, meta)`` where ``meta`` is a dict meant to be merged into
    the response: ``total`` (full count), ``count`` (len of this page),
    ``offset``, ``limit`` (``None`` when uncapped) and ``truncated``.

    Param semantics (all optional):
      - ``offset``: skip this many items (default 0; negatives clamped to 0).
      - ``limit``:  max items to return (default ``default_limit``). A value
        ``<= 0`` means "no cap" — return everything from ``offset`` onward.
    Non-numeric values fall back to the defaults rather than erroring.
    """
    total = len(items)

    try:
        offset = int(params.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    if offset < 0:
        offset = 0

    try:
        limit = int(params.get("limit", default_limit))
    except (TypeError, ValueError):
        limit = default_limit

    if limit <= 0:
        page = items[offset:]
        applied_limit = None
    else:
        page = items[offset : offset + limit]
        applied_limit = limit

    meta = {
        "total": total,
        "count": len(page),
        "offset": offset,
        "limit": applied_limit,
        "truncated": offset + len(page) < total,
    }
    return page, meta
