"""Read-modify-write helpers for the KiCad ``.kicad_pro`` project JSON.

In KiCad 9/10 the **net classes**, **net-class membership patterns/assignments**,
and **design-rule minimums** live in the ``.kicad_pro`` project JSON, *not* in
the ``.kicad_pcb`` board that the SWIG ``pcbnew`` API mutates.  Calling the SWIG
setters (or even ``board.Save()``) therefore never persists any of these — the
canonical store is the project file.

These helpers do a careful read-modify-write so the three affected commands
(``create_netclass``, ``set_design_rules``, ``assign_net_to_class``) can persist
their changes to disk.  All existing keys are preserved and the file is written
back with the *same* JSON formatting it already used (KiCad 9 emits tab indent;
KiCad 10 emits 2-space indent — we detect and match it instead of hardcoding one)
so the on-disk diff stays minimal.

Note on units: the project JSON stores lengths as **mm floats** (e.g.
``"track_width": 0.5``), unlike the SWIG API which uses integer nanometres.  Do
*not* scale by 1e6 when writing here.
"""

from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:  # stdlib, but guard so import never explodes a handler
    import json
except Exception:  # pragma: no cover - json is always present
    json = None  # type: ignore[assignment]


def project_path_for_board(board: Any) -> Optional[str]:
    """Return the ``.kicad_pro`` path that is a sibling of the loaded board.

    The project file shares the board's stem with a ``.kicad_pro`` extension.
    Returns ``None`` when no board/filename is available so callers can surface
    the existing "No board is loaded" error rather than guessing a path.
    """
    if board is None:
        return None
    try:
        board_file = board.GetFileName()
    except Exception:
        return None
    if not board_file:
        return None
    return str(Path(board_file).with_suffix(".kicad_pro"))


def _detect_indent(text: str) -> str:
    """Infer the indent unit KiCad used for this file.

    KiCad 9 writes tab-indented JSON; KiCad 10 writes 2-space-indented JSON.
    We sniff the first indented line and mirror it so the rewrite produces a
    minimal diff.  Defaults to 2 spaces (KiCad 10) when undetectable.
    """
    for line in text.splitlines():
        if not line or line[0] not in (" ", "\t"):
            continue
        if line[0] == "\t":
            return "\t"
        stripped = line.lstrip(" ")
        n = len(line) - len(stripped)
        if n > 0:
            return " " * n
    return "  "


def load_kicad_pro(path: str) -> Tuple[Dict[str, Any], str]:
    """Read a ``.kicad_pro`` file, returning ``(data, indent)``.

    ``indent`` is the detected indent unit, to be passed back to
    :func:`save_kicad_pro` so the formatting round-trips.
    """
    text = Path(path).read_text(encoding="utf-8")
    data = json.loads(text)
    return data, _detect_indent(text)


def save_kicad_pro(path: str, data: Dict[str, Any], indent: str = "  ") -> None:
    """Write ``data`` back to ``path`` preserving KiCad's formatting.

    KiCad terminates the file with a trailing newline; ``json.dump`` does not,
    so we append one explicitly.
    """
    # KiCad writes UTF-8 literally (e.g. non-ASCII labels), so disable
    # ensure_ascii to avoid turning them into \uXXXX escapes and churning the
    # diff.
    p = Path(path)
    with p.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)
        f.write("\n")


def _net_settings(data: Dict[str, Any]) -> Dict[str, Any]:
    """Return the (created-if-missing) ``net_settings`` block."""
    ns = data.get("net_settings")
    if not isinstance(ns, dict):
        ns = {}
        data["net_settings"] = ns
    return ns


def _default_class_template(net_settings: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of the ``Default`` net class to use as a template.

    Copying the existing ``Default`` class guarantees every field KiCad
    expects (``priority``, colors, diff-pair settings, …) is present so the
    project file stays valid.  Falls back to a hardcoded minimal template if no
    ``Default`` class exists yet.
    """
    classes = net_settings.get("classes")
    if isinstance(classes, list):
        for cls in classes:
            if isinstance(cls, dict) and cls.get("name") == "Default":
                return dict(cls)
    # Minimal KiCad 10 default if the project somehow lacks one.
    return {
        "bus_width": 12,
        "clearance": 0.2,
        "diff_pair_gap": 0.25,
        "diff_pair_via_gap": 0.25,
        "diff_pair_width": 0.2,
        "line_style": 0,
        "microvia_diameter": 0.3,
        "microvia_drill": 0.1,
        "name": "Default",
        "pcb_color": "rgba(0, 0, 0, 0.000)",
        "priority": 2147483647,
        "schematic_color": "rgba(0, 0, 0, 0.000)",
        "track_width": 0.25,
        "tuning_profile": "",
        "via_diameter": 0.6,
        "via_drill": 0.3,
        "wire_width": 6,
    }


def _next_custom_priority(net_settings: Dict[str, Any]) -> int:
    """Pick a priority for a new custom class (lower int = higher priority).

    ``Default`` uses ``2147483647`` (lowest).  Custom classes use small ints;
    we hand out the next value above the current max custom priority so a fresh
    class is the lowest-priority custom class without colliding.
    """
    classes = net_settings.get("classes")
    used = []
    if isinstance(classes, list):
        for cls in classes:
            if not isinstance(cls, dict) or cls.get("name") == "Default":
                continue
            pr = cls.get("priority")
            if isinstance(pr, int):
                used.append(pr)
    return (max(used) + 1) if used else 0


def upsert_netclass(
    net_settings: Dict[str, Any], name: str, overrides: Dict[str, Any]
) -> Dict[str, Any]:
    """Create or update the named class in ``net_settings.classes``.

    ``overrides`` maps ``.kicad_pro`` class keys (e.g. ``track_width``,
    ``clearance``) to mm floats; ``None`` values are ignored.  Returns the
    resulting class dict.
    """
    classes = net_settings.get("classes")
    if not isinstance(classes, list):
        classes = []
        net_settings["classes"] = classes

    target = None
    for cls in classes:
        if isinstance(cls, dict) and cls.get("name") == name:
            target = cls
            break

    if target is None:
        target = _default_class_template(net_settings)
        target["name"] = name
        target["priority"] = _next_custom_priority(net_settings)
        classes.append(target)

    for key, value in overrides.items():
        if value is not None:
            target[key] = value

    return target


def assign_net_to_class(net_settings: Dict[str, Any], net_name: str, class_name: str) -> None:
    """Record an explicit net -> class assignment in ``netclass_assignments``.

    KiCad stores explicit per-net membership as a ``{net_name: class_name}``
    map (separate from the wildcard ``netclass_patterns`` list).  ``null`` is a
    valid serialized value when empty, so coerce it to a dict first.
    """
    assignments = net_settings.get("netclass_assignments")
    if not isinstance(assignments, dict):
        assignments = {}
        net_settings["netclass_assignments"] = assignments
    assignments[net_name] = class_name


def add_netclass_pattern(net_settings: Dict[str, Any], class_name: str, pattern: str) -> bool:
    """Append a wildcard pattern -> class rule to ``netclass_patterns``.

    Returns ``True`` if a new rule was added, ``False`` if an identical rule
    already existed (idempotent).
    """
    patterns = net_settings.get("netclass_patterns")
    if not isinstance(patterns, list):
        patterns = []
        net_settings["netclass_patterns"] = patterns
    for entry in patterns:
        if (
            isinstance(entry, dict)
            and entry.get("netclass") == class_name
            and entry.get("pattern") == pattern
        ):
            return False
    patterns.append({"netclass": class_name, "pattern": pattern})
    return True
