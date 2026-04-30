from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from chatgpt_register import paths


class RuntimePathTests(unittest.TestCase):
    def test_config_explicit_override(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            explicit = root / "custom.json"
            self.assertEqual(paths.config_path(explicit, project_root=root), explicit)

    def test_config_env_override(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env_config = root / "env.json"
            with mock.patch.dict(os.environ, {paths.CONFIG_ENV: str(env_config)}, clear=False):
                self.assertEqual(paths.config_path(project_root=root), env_config)

    def test_legacy_config_is_used_without_override(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy = root / "config.json"
            legacy.write_text("{}", encoding="utf-8")
            self.assertEqual(paths.config_path(project_root=root), legacy)

    def test_data_dir_override_ignores_legacy_runtime_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy = root / "data.db"
            legacy.write_text("", encoding="utf-8")
            data_dir = root / "state"
            with mock.patch.dict(os.environ, {paths.DATA_DIR_ENV: str(data_dir)}, clear=False):
                self.assertEqual(paths.database_path(project_root=root), data_dir / "data.db")

    def test_source_checkout_default_uses_var(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(paths.config_path(project_root=root), root / "var" / "config.json")

    def test_installed_default_uses_cwd_var(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            with mock.patch("chatgpt_register.paths.find_project_root", return_value=None):
                with mock.patch("pathlib.Path.cwd", return_value=cwd):
                    self.assertEqual(paths.data_dir(), cwd / "var")

    def test_relative_output_uses_legacy_when_present(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy = root / "registered_accounts.txt"
            legacy.write_text("", encoding="utf-8")
            self.assertEqual(paths.output_file_path("registered_accounts.txt", project_root=root), legacy)

    def test_relative_output_uses_data_dir_without_legacy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(
                paths.output_file_path("nested/out.txt", project_root=root),
                root / "var" / "nested" / "out.txt",
            )

    def test_absolute_output_is_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "out.txt"
            self.assertEqual(paths.output_file_path(target), target)


if __name__ == "__main__":
    unittest.main()
