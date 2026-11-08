"""Phase 5: Kivy is an optional extra, never a core runtime dependency."""

import os
import re
import subprocess
import sys
import tomllib
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYPROJECT = os.path.join(ROOT, "pyproject.toml")

# Make kivy/kivymd unimportable the way a clean install without the extra is.
_BLOCK_KIVY = """
import importlib.abc
import sys


class _BlockKivy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"kivy", "kivymd"}:
            raise ImportError(fullname + " is not installed (blocked for test)")
        return None


sys.meta_path.insert(0, _BlockKivy())
"""


def _run_python(code: str, *args: str, timeout: int = 90):
    env = dict(os.environ, PYTHONPATH=ROOT)
    env.pop("UC_DATA_DIR", None)
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _dependency_name(requirement: str) -> str:
    return re.split(r"[<>=!~\[; ]", requirement.strip(), maxsplit=1)[0].lower()


class CoreImportsWithoutKivyTests(unittest.TestCase):
    def test_core_modules_import_when_kivy_is_unavailable(self):
        code = _BLOCK_KIVY + """
import main
import headless
import chatroom
import tornado_server
import ui
import api.base
import app.runtime
import app.paths

leaked = sorted(
    {name.split(".")[0] for name in sys.modules} & {"kivy", "kivymd"}
)
if leaked:
    raise SystemExit(f"core import pulled in optional UI packages: {leaked}")
print("CORE_OK")
"""
        result = _run_python(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CORE_OK", result.stdout)

    def test_kivy_flag_fails_fast_with_install_hint(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            code = _BLOCK_KIVY + """
import sys
import main

sys.argv = ["main.py", "--kivy", "--nick", "test", "--data-dir", sys.argv[1]]
main.main()
"""
            result = _run_python(code, tmp)

            self.assertEqual(result.returncode, 1, result.stderr)
            combined = result.stdout + result.stderr
            self.assertIn("optional extra", combined)
            self.assertIn(".[kivy]", combined)
            # It must fail before starting a host or opening the data directory.
            self.assertFalse(os.path.exists(os.path.join(tmp, "history.db")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "identity")))


class PackagingTests(unittest.TestCase):
    def setUp(self):
        with open(PYPROJECT, "rb") as handle:
            self.project = tomllib.load(handle)["project"]

    def test_kivy_is_not_a_required_dependency(self):
        required = {_dependency_name(dep) for dep in self.project["dependencies"]}
        self.assertNotIn("kivy", required)
        self.assertNotIn("kivymd", required)

    def test_kivy_extra_declares_both_packages(self):
        extra = {
            _dependency_name(dep)
            for dep in self.project["optional-dependencies"]["kivy"]
        }
        self.assertEqual(extra, {"kivy", "kivymd"})

    def test_core_modules_do_not_import_kivy_at_module_level(self):
        for name in ("main.py", "headless.py", "chatroom.py", "ui.py", "tornado_server.py"):
            path = os.path.join(ROOT, name)
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            top_level = [
                line
                for line in source.splitlines()
                if re.match(r"\s*(?:import|from)\s+(?:kivy|kivymd)\b", line)
            ]
            self.assertEqual(top_level, [], f"{name} imports Kivy at module level")


if __name__ == "__main__":
    unittest.main()
