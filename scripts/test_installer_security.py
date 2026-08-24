#!/usr/bin/env python3
"""Windows installer regression tests for destination reparse-point safety."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import bootstrap


SKILL_ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Windows PowerShell installer test")
class InstallerSecurityTests(unittest.TestCase):
    def _make_junction(self, link: Path, target: Path) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        assert powershell is not None
        environment = os.environ.copy()
        environment["MWR_TEST_LINK"] = str(link)
        environment["MWR_TEST_TARGET"] = str(target)
        completed = subprocess.run(
            (
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "New-Item -ItemType Junction -Path $env:MWR_TEST_LINK "
                "-Target $env:MWR_TEST_TARGET | Out-Null",
            ),
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_bootstrap_rejects_nested_runtime_junction_to_outside(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mwr-bootstrap-security-") as temporary:
            root = Path(temporary)
            runtime = root / ".venv"
            outside = root / "outside"
            runtime.mkdir()
            outside.mkdir()
            self._make_junction(runtime / "Scripts", outside)
            with self.assertRaisesRegex(bootstrap.BootstrapError, "escaping link"):
                bootstrap._assert_no_escaping_links(runtime, "virtual environment")

    def test_existing_content_junction_cannot_redirect_install_writes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mwr-installer-security-") as temporary:
            root = Path(temporary)
            destination_root = root / "skills"
            destination = destination_root / "math-workbook-rebuilder"
            outside = root / "outside"
            destination.mkdir(parents=True)
            outside.mkdir()

            self._make_junction(destination / "scripts", outside)

            installed = subprocess.run(
                (
                    shutil.which("powershell.exe") or "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(SKILL_ROOT / "install.ps1"),
                    "-DestinationRoot",
                    str(destination_root),
                ),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertNotEqual(installed.returncode, 0)
            self.assertIn("reparse point", (installed.stderr + installed.stdout).lower())
            self.assertEqual(list(outside.iterdir()), [])

    def test_in_place_install_does_not_delete_its_own_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mwr-installer-in-place-") as temporary:
            destination_root = Path(temporary) / "skills"
            destination = destination_root / "math-workbook-rebuilder"
            destination.mkdir(parents=True)
            installer = destination / "install.ps1"
            installer.write_bytes((SKILL_ROOT / "install.ps1").read_bytes())
            sentinel = destination / "README.md"
            sentinel.write_text("keep this in-place file\n", encoding="utf-8")

            installed = subprocess.run(
                (
                    shutil.which("powershell.exe") or "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(installer),
                    "-DestinationRoot",
                    str(destination_root),
                ),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(installed.returncode, 0, installed.stderr or installed.stdout)
            self.assertTrue(installer.is_file())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep this in-place file\n")


if __name__ == "__main__":
    unittest.main()
