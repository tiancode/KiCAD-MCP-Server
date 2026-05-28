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
