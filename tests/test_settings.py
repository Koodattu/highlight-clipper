from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from highlight_clipper.settings import Settings


class SettingsDiscoveryTests(unittest.TestCase):
    def test_default_and_nested_private_workdirs_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(Settings.discover(root).work_dir, root / "workdir")
            with patch.dict(os.environ, {"HIGHLIGHT_CLIPPER_WORKDIR": "workdir/machine-a"}, clear=True):
                self.assertEqual(Settings.discover(root).work_dir, root / "workdir" / "machine-a")

    def test_override_cannot_bypass_the_ignored_privacy_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for override in (str(root), "data", str(root.parent / "external")):
                with self.subTest(override=override), patch.dict(
                    os.environ,
                    {"HIGHLIGHT_CLIPPER_WORKDIR": override},
                    clear=True,
                ), self.assertRaisesRegex(RuntimeError, "Git-ignored workdir"):
                    Settings.discover(root)


if __name__ == "__main__":
    unittest.main()
