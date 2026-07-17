"""
Tests for platform_helper utility

These are unit tests that work on all platforms.
"""

import os
import platform
import sys
from pathlib import Path

import pytest

# Add parent directory to path to import utils
sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from utils.platform_helper import PlatformHelper, detect_platform


class TestPlatformDetection:
    """Test platform detection functions"""

    def test_exactly_one_platform_detected(self):
        """Ensure exactly one platform is detected"""
        platforms = [
            PlatformHelper.is_windows(),
            PlatformHelper.is_linux(),
            PlatformHelper.is_macos(),
        ]
        assert sum(platforms) == 1, "Exactly one platform should be detected"

    def test_platform_name_is_valid(self):
        """Test platform name is human-readable"""
        name = PlatformHelper.get_platform_name()
        assert name in ["Windows", "Linux", "macOS"], f"Unknown platform: {name}"

    def test_platform_name_matches_detection(self):
        """Ensure platform name matches detection functions"""
        name = PlatformHelper.get_platform_name()
        if name == "Windows":
            assert PlatformHelper.is_windows()
        elif name == "Linux":
            assert PlatformHelper.is_linux()
        elif name == "macOS":
            assert PlatformHelper.is_macos()


class TestPathGeneration:
    """Test path generation functions"""

    def test_config_dir_exists_after_ensure(self):
        """Test that config directory is created"""
        PlatformHelper.ensure_directories()
        config_dir = PlatformHelper.get_config_dir()
        assert config_dir.exists(), f"Config dir should exist: {config_dir}"
        assert config_dir.is_dir(), f"Config dir should be a directory: {config_dir}"

    def test_log_dir_exists_after_ensure(self):
        """Test that log directory is created"""
        PlatformHelper.ensure_directories()
        log_dir = PlatformHelper.get_log_dir()
        assert log_dir.exists(), f"Log dir should exist: {log_dir}"
        assert log_dir.is_dir(), f"Log dir should be a directory: {log_dir}"

    def test_cache_dir_exists_after_ensure(self):
        """Test that cache directory is created"""
        PlatformHelper.ensure_directories()
        cache_dir = PlatformHelper.get_cache_dir()
        assert cache_dir.exists(), f"Cache dir should exist: {cache_dir}"
        assert cache_dir.is_dir(), f"Cache dir should be a directory: {cache_dir}"

    def test_data_dir_exists_after_ensure(self):
        """Test that data directory is created"""
        PlatformHelper.ensure_directories()
        data_dir = PlatformHelper.get_data_dir()
        assert data_dir.exists(), f"Data dir should exist: {data_dir}"
        assert data_dir.is_dir(), f"Data dir should be a directory: {data_dir}"

    def test_data_dir_is_platform_appropriate(self):
        """Test that data directory follows platform conventions"""
        data_dir = PlatformHelper.get_data_dir()

        if PlatformHelper.is_linux():
            # Should be ~/.local/share/kicad-mcp or $XDG_DATA_HOME/kicad-mcp
            xdg = os.environ.get("XDG_DATA_HOME")
            if xdg and Path(xdg).is_absolute():
                expected = Path(xdg) / "kicad-mcp"
            else:
                expected = Path.home() / ".local" / "share" / "kicad-mcp"
            assert data_dir == expected

        elif PlatformHelper.is_windows():
            # Should be %USERPROFILE%\.kicad-mcp\data
            expected = Path.home() / ".kicad-mcp" / "data"
            assert data_dir == expected

        elif PlatformHelper.is_macos():
            # Should be ~/Library/Application Support/kicad-mcp
            expected = Path.home() / "Library" / "Application Support" / "kicad-mcp"
            assert data_dir == expected

    def test_data_dir_ignores_relative_xdg_data_home(self, monkeypatch):
        """Relative XDG_DATA_HOME should be ignored on Linux."""
        monkeypatch.setattr(PlatformHelper, "is_linux", staticmethod(lambda: True))
        monkeypatch.setattr(PlatformHelper, "is_windows", staticmethod(lambda: False))
        monkeypatch.setattr(PlatformHelper, "is_macos", staticmethod(lambda: False))
        monkeypatch.setenv("XDG_DATA_HOME", "relative/data")

        assert PlatformHelper.get_data_dir() == Path.home() / ".local" / "share" / "kicad-mcp"

    def test_config_dir_is_platform_appropriate(self):
        """Test that config directory follows platform conventions"""
        config_dir = PlatformHelper.get_config_dir()

        if PlatformHelper.is_linux():
            # Should be ~/.config/kicad-mcp or $XDG_CONFIG_HOME/kicad-mcp
            if "XDG_CONFIG_HOME" in os.environ:
                expected = Path(os.environ["XDG_CONFIG_HOME"]) / "kicad-mcp"
            else:
                expected = Path.home() / ".config" / "kicad-mcp"
            assert config_dir == expected

        elif PlatformHelper.is_windows():
            # Should be %USERPROFILE%\.kicad-mcp
            expected = Path.home() / ".kicad-mcp"
            assert config_dir == expected

        elif PlatformHelper.is_macos():
            # Should be ~/Library/Application Support/kicad-mcp
            expected = Path.home() / "Library" / "Application Support" / "kicad-mcp"
            assert config_dir == expected

    def test_config_dir_ignores_relative_xdg_config_home(self, monkeypatch):
        """Relative XDG_CONFIG_HOME should be ignored on Linux."""
        monkeypatch.setattr(PlatformHelper, "is_linux", staticmethod(lambda: True))
        monkeypatch.setattr(PlatformHelper, "is_windows", staticmethod(lambda: False))
        monkeypatch.setattr(PlatformHelper, "is_macos", staticmethod(lambda: False))
        monkeypatch.setenv("XDG_CONFIG_HOME", "relative/path")

        assert PlatformHelper.get_config_dir() == Path.home() / ".config" / "kicad-mcp"

    def test_cache_dir_ignores_relative_xdg_cache_home(self, monkeypatch):
        """Relative XDG_CACHE_HOME should be ignored on Linux."""
        monkeypatch.setattr(PlatformHelper, "is_linux", staticmethod(lambda: True))
        monkeypatch.setattr(PlatformHelper, "is_windows", staticmethod(lambda: False))
        monkeypatch.setattr(PlatformHelper, "is_macos", staticmethod(lambda: False))
        monkeypatch.setenv("XDG_CACHE_HOME", "relative/cache")

        assert PlatformHelper.get_cache_dir() == Path.home() / ".cache" / "kicad-mcp"

    def test_python_executable_is_valid(self):
        """Test that Python executable path is valid"""
        exe = PlatformHelper.get_python_executable()
        assert exe.exists(), f"Python executable should exist: {exe}"
        assert str(exe) == sys.executable

    def test_kicad_library_search_paths_returns_list(self):
        """Test that library search paths returns a list"""
        paths = PlatformHelper.get_kicad_library_search_paths()
        assert isinstance(paths, list)
        assert len(paths) > 0
        # All paths should be strings (glob patterns)
        assert all(isinstance(p, str) for p in paths)


class TestDetectPlatform:
    """Test the detect_platform convenience function"""

    def test_detect_platform_returns_dict(self):
        """Test that detect_platform returns a dictionary"""
        info = detect_platform()
        assert isinstance(info, dict)

    def test_detect_platform_has_required_keys(self):
        """Test that detect_platform includes all required keys"""
        info = detect_platform()
        required_keys = [
            "system",
            "platform",
            "is_windows",
            "is_linux",
            "is_macos",
            "python_version",
            "python_executable",
            "config_dir",
            "log_dir",
            "cache_dir",
            "data_dir",
            "kicad_python_paths",
        ]
        for key in required_keys:
            assert key in info, f"Missing key: {key}"

    def test_detect_platform_python_version_format(self):
        """Test that Python version is in correct format"""
        info = detect_platform()
        version = info["python_version"]
        # Should be like "3.12.3"
        parts = version.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)


class TestFootprintSearchRoots:
    """C6: list_footprint_libraries returned 0 on macOS because its hardcoded
    default paths omitted the macOS app-bundle root and KiCad 10. The single
    shared resolver must cover every platform + KiCad 8/9/10 so both
    list_footprint_libraries and LibraryManager consult the same roots."""

    def test_returns_nonempty_list_of_strings(self):
        roots = PlatformHelper.kicad_footprint_search_roots()
        assert isinstance(roots, list)
        assert len(roots) > 0
        assert all(isinstance(r, str) for r in roots)

    def test_includes_macos_app_bundle_root(self):
        """The exact root the old footprint.py list omitted (155 libs live here)."""
        roots = PlatformHelper.kicad_footprint_search_roots()
        assert (
            "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints" in roots
        ), "macOS app-bundle footprint root must be a default search root"

    def test_includes_linux_system_roots(self):
        roots = PlatformHelper.kicad_footprint_search_roots()
        assert "/usr/share/kicad/footprints" in roots
        assert "/usr/local/share/kicad/footprints" in roots

    def test_includes_kicad_10_windows_root(self):
        """KiCad 10 was missing from the old default list entirely."""
        roots = PlatformHelper.kicad_footprint_search_roots()
        assert any("10.0" in r and "KiCad" in r for r in roots)

    def test_includes_kicad_10_user_documents_root(self):
        roots = PlatformHelper.kicad_footprint_search_roots()
        expected = str(Path.home() / "Documents" / "KiCad" / "10.0" / "footprints")
        assert expected in roots

    def test_env_override_is_first_and_present(self, monkeypatch):
        monkeypatch.setenv("KICAD10_FOOTPRINT_DIR", "/custom/kicad/footprints")
        roots = PlatformHelper.kicad_footprint_search_roots()
        assert "/custom/kicad/footprints" in roots
        # Env overrides take precedence (first existing root wins for _locate).
        assert roots[0] == "/custom/kicad/footprints"


class TestListFootprintLibrariesUsesSharedRoots:
    """C6 behavioural regression: list_footprint_libraries with no searchPaths
    must scan the shared roots. Before the fix it used a private hardcoded list,
    so monkeypatching the shared resolver had no effect and the call found 0."""

    def test_no_args_scans_shared_roots(self, tmp_path, monkeypatch):
        from commands.footprint import FootprintCreator

        # A populated root the OLD hardcoded default list would never have looked at.
        root = tmp_path / "SharedSupport" / "footprints"
        pretty = root / "MyConn.pretty"
        pretty.mkdir(parents=True)
        (pretty / "Conn_01x02.kicad_mod").write_text(
            '(footprint "Conn_01x02" (layer "F.Cu"))\n', encoding="utf-8"
        )

        monkeypatch.setattr(
            PlatformHelper, "kicad_footprint_search_roots", staticmethod(lambda: [str(root)])
        )

        out = FootprintCreator().list_footprint_libraries()
        assert out["success"] is True
        assert out["library_count"] == 1
        assert "MyConn" in out["libraries"]
        assert out["libraries"]["MyConn"]["footprints"] == ["Conn_01x02"]

    def test_explicit_search_paths_still_honored(self, tmp_path):
        from commands.footprint import FootprintCreator

        pretty = tmp_path / "Lib.pretty"
        pretty.mkdir()
        (pretty / "R_0603.kicad_mod").write_text(
            '(footprint "R_0603" (layer "F.Cu"))\n', encoding="utf-8"
        )

        out = FootprintCreator().list_footprint_libraries(search_paths=[str(tmp_path)])
        assert out["library_count"] == 1
        assert "Lib" in out["libraries"]


@pytest.mark.integration
class TestKiCADPathDetection:
    """Tests that require KiCAD to be installed"""

    def test_kicad_python_paths_exist(self):
        """Test that at least one KiCAD Python path exists (if KiCAD is installed)"""
        paths = PlatformHelper.get_kicad_python_paths()
        # This test only makes sense if KiCAD is installed
        # In CI, KiCAD should be installed
        if paths:
            assert all(p.exists() for p in paths), "All returned paths should exist"

    def test_can_import_pcbnew_after_adding_paths(self):
        """Test that pcbnew can be imported after adding KiCAD paths"""
        PlatformHelper.add_kicad_to_python_path()
        try:
            import pcbnew

            # If we get here, pcbnew is available
            assert pcbnew is not None
            version = pcbnew.GetBuildVersion()
            assert version is not None
            print(f"Found KiCAD version: {version}")
        except ImportError:
            pytest.skip("KiCAD pcbnew module not available (KiCAD not installed)")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
