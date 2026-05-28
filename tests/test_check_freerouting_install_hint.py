"""Regression tests for ``check_freerouting`` install-hint output.

User report: response said ``jar_found: false`` and left them to
discover the install URL on their own.  The response now carries a
structured ``install.steps[]`` section with concrete download
instructions whenever a prerequisite is missing.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _cmds():
    from commands.freerouting import FreeroutingCommands

    obj = FreeroutingCommands.__new__(FreeroutingCommands)
    obj.board = None
    return obj


# ---------------------------------------------------------------------------
# JAR missing → install step with download URL + target path
# ---------------------------------------------------------------------------
def test_install_steps_surface_when_jar_missing(monkeypatch, tmp_path):
    from commands import freerouting as fr

    nonexistent = tmp_path / "freerouting.jar"  # never created

    monkeypatch.setattr(fr, "DEFAULT_FREEROUTING_JAR", str(nonexistent))
    # Java OK so we isolate the jar-missing branch.
    monkeypatch.setattr(fr, "_find_java", lambda: "/usr/bin/java")
    monkeypatch.setattr(fr, "_java_version_ok", lambda exe: True)
    # Docker absence shouldn't matter when java is OK; force False for clarity.
    monkeypatch.setattr(fr, "_find_docker", lambda: None)
    monkeypatch.setattr(fr, "_docker_available", lambda: False)

    fake_proc = MagicMock(stderr='openjdk version "21.0.1" 2024-10-15', stdout="")
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: fake_proc)

    out = _cmds().check_freerouting({})

    assert out["ready"] is False
    assert out["freerouting"]["jar_found"] is False
    assert out["execution_mode"] == "direct"  # java is fine, jar isn't

    install = out["install"]
    assert install["needed"] is True
    [step] = install["steps"]
    assert step["missing"] == "freerouting.jar"
    assert step["target_path"] == str(nonexistent)
    # Concrete URLs so the user can copy-paste.
    assert "github.com/freerouting/freerouting/releases" in step["download_page"]
    # Platform-specific shell hints.
    assert any(str(nonexistent.parent) in cmd for cmd in step["shell_unix"])
    assert step["override_with_env"].startswith("FREEROUTING_JAR=")
    # After-install hint points at the next tool to call.
    assert "check_freerouting" in install["after_install"]
    assert "autoroute" in install["after_install"]


# ---------------------------------------------------------------------------
# No Java AND no Docker → second install step covers both options
# ---------------------------------------------------------------------------
def test_install_step_when_neither_java_nor_docker_available(monkeypatch, tmp_path):
    from commands import freerouting as fr

    jar = tmp_path / "freerouting.jar"
    jar.write_text("placeholder", encoding="utf-8")  # jar OK
    monkeypatch.setattr(fr, "DEFAULT_FREEROUTING_JAR", str(jar))
    monkeypatch.setattr(fr, "_find_java", lambda: None)
    monkeypatch.setattr(fr, "_java_version_ok", lambda exe: False)
    monkeypatch.setattr(fr, "_find_docker", lambda: None)
    monkeypatch.setattr(fr, "_docker_available", lambda: False)

    out = _cmds().check_freerouting({})

    assert out["ready"] is False
    assert out["execution_mode"] == "none"

    install = out["install"]
    runtime_step = next(s for s in install["steps"] if "java" in s["missing"])
    assert "java_install" in runtime_step
    # Per-platform install commands are mentioned.
    assert "apt install" in runtime_step["java_install"]
    assert "brew" in runtime_step["java_install"]
    assert "adoptium" in runtime_step["java_install"]
    # Docker alternative is documented too.
    assert "docker_alt" in runtime_step
    assert "Docker" in runtime_step["docker_alt"] or "podman" in runtime_step["docker_alt"]


# ---------------------------------------------------------------------------
# Everything OK → no install section
# ---------------------------------------------------------------------------
def test_no_install_section_when_ready(monkeypatch, tmp_path):
    from commands import freerouting as fr

    jar = tmp_path / "freerouting.jar"
    jar.write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(fr, "DEFAULT_FREEROUTING_JAR", str(jar))
    monkeypatch.setattr(fr, "_find_java", lambda: "/usr/bin/java")
    monkeypatch.setattr(fr, "_java_version_ok", lambda exe: True)
    monkeypatch.setattr(fr, "_find_docker", lambda: None)
    monkeypatch.setattr(fr, "_docker_available", lambda: False)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: MagicMock(stderr='openjdk version "21.0.1"', stdout=""),
    )

    out = _cmds().check_freerouting({})

    assert out["ready"] is True
    assert out["execution_mode"] == "direct"
    # No install section when nothing's missing.
    assert "install" not in out


# ---------------------------------------------------------------------------
# Custom JAR path via param surfaces correctly in install step
# ---------------------------------------------------------------------------
def test_custom_jar_path_appears_in_install_step(monkeypatch, tmp_path):
    from commands import freerouting as fr

    custom = tmp_path / "different/place/fr.jar"
    monkeypatch.setattr(fr, "_find_java", lambda: "/usr/bin/java")
    monkeypatch.setattr(fr, "_java_version_ok", lambda exe: True)
    monkeypatch.setattr(fr, "_find_docker", lambda: None)
    monkeypatch.setattr(fr, "_docker_available", lambda: False)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: MagicMock(stderr='openjdk version "21"', stdout=""),
    )

    out = _cmds().check_freerouting({"freeroutingJar": str(custom)})

    [step] = out["install"]["steps"]
    assert step["target_path"] == str(custom)
    # mkdir line points at the right parent.
    assert any("different/place" in cmd for cmd in step["shell_unix"])


# ---------------------------------------------------------------------------
# Auto-discovery of versioned filenames (GitHub release ships
# freerouting-X.Y.Z.jar, not the bare freerouting.jar the default path
# expects).
# ---------------------------------------------------------------------------
def test_versioned_jar_filename_auto_discovered(monkeypatch, tmp_path):
    """The user's reproduction: they downloaded
    ``freerouting-2.2.4.jar`` (the actual GitHub release filename) into
    ``~/.kicad-mcp/`` but the default path expects ``freerouting.jar``.
    The resolver now globs ``freerouting-*.jar`` in the same dir and
    picks the newest match — no rename required."""
    from commands import freerouting as fr

    # Bare freerouting.jar does NOT exist; a versioned variant does.
    bare = tmp_path / "freerouting.jar"  # missing
    versioned = tmp_path / "freerouting-2.2.4.jar"
    versioned.write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(fr, "_find_java", lambda: "/usr/bin/java")
    monkeypatch.setattr(fr, "_java_version_ok", lambda exe: True)
    monkeypatch.setattr(fr, "_find_docker", lambda: None)
    monkeypatch.setattr(fr, "_docker_available", lambda: False)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: MagicMock(stderr='openjdk version "26"', stdout=""),
    )

    out = _cmds().check_freerouting({"freeroutingJar": str(bare)})

    assert out["ready"] is True
    assert out["freerouting"]["jar_found"] is True
    # jar_path is the actually-resolved file, not the requested name.
    assert out["freerouting"]["jar_path"] == str(versioned)
    # requested_path reports what the caller asked for, so the user
    # sees both the lookup target and the auto-discovered file.
    assert out["freerouting"]["requested_path"] == str(bare)
    # No install section — auto-discovery covered the gap.
    assert "install" not in out


def test_versioned_jar_picks_newest_when_multiple_present(monkeypatch, tmp_path):
    """Multiple versioned jars in the same dir → resolver picks the
    lexicographically-latest (= newest version under upstream's
    semver-ish scheme)."""
    from commands import freerouting as fr

    (tmp_path / "freerouting-1.9.0.jar").write_text("old", encoding="utf-8")
    newest = tmp_path / "freerouting-2.2.4.jar"
    newest.write_text("new", encoding="utf-8")
    (tmp_path / "freerouting-2.0.0.jar").write_text("middle", encoding="utf-8")

    resolved = fr._resolve_freerouting_jar(str(tmp_path / "freerouting.jar"))
    assert resolved == str(newest)


def test_resolver_returns_none_when_directory_has_no_jars(tmp_path):
    """Defensive: dir exists but contains no ``freerouting-*.jar`` and
    no exact match → None.  Caller then surfaces the install hint."""
    from commands.freerouting import _resolve_freerouting_jar

    assert _resolve_freerouting_jar(str(tmp_path / "freerouting.jar")) is None


def test_resolver_returns_none_when_parent_directory_missing(tmp_path):
    from commands.freerouting import _resolve_freerouting_jar

    missing = tmp_path / "does/not/exist/freerouting.jar"
    assert _resolve_freerouting_jar(str(missing)) is None
