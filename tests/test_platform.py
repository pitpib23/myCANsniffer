"""Cross-platform behaviour: paths, config location, no OS-specific assumptions.

These run on every platform. Where a test needs to reason about another OS it
patches ``sys.platform`` rather than skipping, so Linux behaviour is checked
from Windows and vice versa.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cansniff import config as config_module  # noqa: E402
from cansniff.config import (  # noqa: E402
    APP_DIRNAME, CONFIG_FILENAME, Config, default_config_path, user_config_dir,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _PlatformPatch(object):
    """Temporarily present a different sys.platform to the module under test."""

    def __init__(self, platform, environ=None):
        self.platform = platform
        self.environ = environ or {}
        self._saved_platform = None
        self._saved_env = {}

    def __enter__(self):
        self._saved_platform = config_module.sys.platform
        config_module.sys.platform = self.platform
        for key, value in self.environ.items():
            self._saved_env[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return self

    def __exit__(self, *exc):
        config_module.sys.platform = self._saved_platform
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


class UserConfigDirTests(unittest.TestCase):
    def test_linux_uses_xdg_config_home(self):
        with _PlatformPatch("linux", {"XDG_CONFIG_HOME": os.path.join("/tmp", "xdg")}):
            path = user_config_dir()
        self.assertEqual(path, os.path.join("/tmp", "xdg", APP_DIRNAME))

    def test_linux_falls_back_to_dot_config(self):
        with _PlatformPatch("linux", {"XDG_CONFIG_HOME": None}):
            path = user_config_dir()
        self.assertEqual(
            path, os.path.join(os.path.expanduser("~"), ".config", APP_DIRNAME))

    def test_windows_uses_appdata(self):
        with _PlatformPatch("win32", {"APPDATA": os.path.join("C:", "Roaming")}):
            path = user_config_dir()
        self.assertEqual(path, os.path.join("C:", "Roaming", APP_DIRNAME))

    def test_macos_uses_application_support(self):
        with _PlatformPatch("darwin"):
            path = user_config_dir()
        self.assertIn("Application Support", path)

    def test_no_separator_is_hard_coded(self):
        """Paths must be built with os.path.join, not string concatenation."""
        for platform in ("linux", "win32", "darwin"):
            with _PlatformPatch(platform, {"XDG_CONFIG_HOME": None,
                                           "APPDATA": None}):
                path = user_config_dir()
            self.assertNotIn("\\/", path)
            self.assertNotIn("//", path.replace("://", ":/"))


class DefaultConfigPathTests(unittest.TestCase):
    def test_existing_local_config_wins_for_backward_compatibility(self):
        """An existing install must keep its filters and rules where they are."""
        cwd = os.getcwd()
        temp = os.path.join(os.environ.get("TEMP", "/tmp"), "cansniff_cfgtest")
        os.makedirs(temp, exist_ok=True)
        local = os.path.join(temp, CONFIG_FILENAME)
        with open(local, "w", encoding="utf-8") as fh:
            fh.write("{}")
        self.addCleanup(lambda: os.path.exists(local) and os.remove(local))
        os.chdir(temp)
        try:
            self.assertEqual(default_config_path(), os.path.abspath(local))
        finally:
            os.chdir(cwd)

    def test_fresh_install_uses_the_platform_directory(self):
        cwd = os.getcwd()
        temp = os.path.join(os.environ.get("TEMP", "/tmp"), "cansniff_cfgtest_empty")
        os.makedirs(temp, exist_ok=True)
        stray = os.path.join(temp, CONFIG_FILENAME)
        if os.path.exists(stray):
            os.remove(stray)
        os.chdir(temp)
        try:
            self.assertEqual(os.path.dirname(default_config_path()),
                             user_config_dir())
        finally:
            os.chdir(cwd)

    def test_config_creates_its_directory_on_save(self):
        target = os.path.join(os.environ.get("TEMP", "/tmp"),
                              "cansniff_nested", "deeper", CONFIG_FILENAME)
        parent = os.path.dirname(os.path.dirname(target))
        Config.defaults(target).save()
        self.addCleanup(lambda: os.path.exists(target) and os.remove(target))
        self.assertTrue(os.path.exists(target))
        self.assertTrue(os.path.isdir(parent))


class SourceTreePlatformTests(unittest.TestCase):
    """Static checks that no Windows-only assumption creeps back in."""

    def _sources(self):
        for root, dirs, files in os.walk(os.path.join(REPO, "cansniff")):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if name.endswith(".py"):
                    path = os.path.join(root, name)
                    with open(path, "r", encoding="utf-8") as fh:
                        yield path, fh.read()

    def test_no_windows_only_apis(self):
        banned = ("winreg", "os.startfile", "CREATE_NO_WINDOW", "%APPDATA%")
        for path, text in self._sources():
            for token in banned:
                self.assertNotIn(token, text,
                                 "{} uses {}".format(os.path.basename(path), token))

    def test_no_drive_letter_or_backslash_paths(self):
        for path, text in self._sources():
            self.assertNotIn("C:\\", text, os.path.basename(path))

    def test_no_shell_true_subprocesses(self):
        for path, text in self._sources():
            self.assertNotIn("shell=True", text, os.path.basename(path))

    def test_font_stack_has_linux_fallbacks(self):
        from cansniff.ui.theme import _MONO_FONTS, _UI_FONTS
        self.assertIn("DejaVu Sans Mono", _MONO_FONTS)
        self.assertIn("Noto Sans", _UI_FONTS)


class PackagingTests(unittest.TestCase):
    def test_pyproject_declares_linux_and_entry_point(self):
        path = os.path.join(REPO, "pyproject.toml")
        self.assertTrue(os.path.exists(path), "pyproject.toml is required")
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("POSIX :: Linux", text)
        self.assertIn("gui-scripts", text)
        self.assertIn("cantools", text)

    def test_desktop_entry_is_present_and_well_formed(self):
        path = os.path.join(REPO, "packaging", "linux", "cansniff.desktop")
        self.assertTrue(os.path.exists(path))
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("[Desktop Entry]"))
        for field in ("Type=Application", "Exec=", "Name=", "Categories="):
            self.assertIn(field, text)

    def test_requirements_and_pyproject_agree_on_dependencies(self):
        with open(os.path.join(REPO, "requirements.txt"), encoding="utf-8") as fh:
            reqs = fh.read()
        with open(os.path.join(REPO, "pyproject.toml"), encoding="utf-8") as fh:
            proj = fh.read()
        for package in ("PySide6", "python-can", "cantools"):
            self.assertIn(package, reqs)
            self.assertIn(package, proj)


if __name__ == "__main__":
    unittest.main()
