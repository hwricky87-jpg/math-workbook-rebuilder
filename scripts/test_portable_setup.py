#!/usr/bin/env python3
"""Focused tests for fresh-install bootstrap and readiness checks."""

from __future__ import annotations

import tempfile
import unittest
import subprocess
import sys
import os
from pathlib import Path
from unittest import mock

from scripts import bootstrap, doctor

import run as skill_run


class PortableSetupTests(unittest.TestCase):
    def test_launcher_emits_non_ascii_output_as_utf8(self) -> None:
        source_run = Path(__file__).resolve().parents[1] / "run.py"
        with tempfile.TemporaryDirectory(prefix="mwr-launcher-utf8-") as temporary:
            root = Path(temporary)
            (root / "scripts").mkdir()
            (root / "run.py").write_bytes(source_run.read_bytes())
            (root / "scripts" / "doctor.py").write_text(
                "print('정현우 · 원본 좌표')\n", encoding="utf-8"
            )
            completed = subprocess.run(
                (sys.executable, str(root / "run.py"), "doctor.py"),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), "정현우 · 원본 좌표")

    def test_launcher_child_environment_forces_utf8(self) -> None:
        with mock.patch.dict(os.environ, {"PYTHONUTF8": "0", "PYTHONIOENCODING": "cp949"}):
            environment = skill_run._child_environment()
        self.assertEqual(environment["PYTHONUTF8"], "1")
        self.assertEqual(environment["PYTHONIOENCODING"], "utf-8")

    def test_launcher_requires_the_skill_local_runtime_for_production_scripts(self) -> None:
        source_run = Path(__file__).resolve().parents[1] / "run.py"
        with tempfile.TemporaryDirectory(prefix="mwr-launcher-test-") as temporary:
            root = Path(temporary)
            (root / "scripts").mkdir()
            (root / "run.py").write_bytes(source_run.read_bytes())
            for name in ("doctor.py", "selftest.py"):
                (root / "scripts" / name).write_text("print('ran')\n", encoding="utf-8")

            doctor_run = subprocess.run(
                (sys.executable, str(root / "run.py"), "doctor.py"),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
            )
            production_run = subprocess.run(
                (sys.executable, str(root / "run.py"), "selftest.py"),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
            )
            self.assertEqual(doctor_run.returncode, 0)
            self.assertIn("ran", doctor_run.stdout)
            self.assertEqual(production_run.returncode, 2)
            self.assertIn("bootstrap.py --verify", production_run.stderr)

    def test_local_python_is_skill_relative(self) -> None:
        root = Path("portable-skill")
        expected = root / ".venv" / ("Scripts/python.exe" if doctor.os.name == "nt" else "bin/python")
        self.assertEqual(doctor.local_python(root), expected)

    def test_node_version_parser(self) -> None:
        self.assertEqual(doctor._version_major("v24.7.0"), 24)
        self.assertEqual(doctor._version_major("22"), 22)
        self.assertIsNone(doctor._version_major("unknown"))

    def test_configured_korean_font_is_discovered(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mwr-font-test-") as temporary:
            font = Path(temporary) / "test-font.ttf"
            font.write_bytes(b"synthetic font placeholder")
            with mock.patch.dict(doctor.os.environ, {"MWR_KOREAN_FONT": str(font)}), mock.patch.object(
                doctor.shutil, "which", return_value=None
            ):
                self.assertIn(font.resolve(), doctor.find_korean_fonts(Path(temporary)))

    def test_bundled_fonts_are_first_and_have_release_names(self) -> None:
        root = Path("portable-skill")
        self.assertEqual(
            doctor._font_candidates(root)[:2],
            [
                root / "assets/fonts/NanumGothic-Regular.ttf",
                root / "assets/fonts/NanumGothic-Bold.ttf",
            ],
        )

    def test_lock_markers_select_one_python_specific_numpy(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mwr-lock-test-") as temporary:
            lock = Path(temporary) / "requirements-lock.txt"
            lock.write_text(
                'numpy==1.0; python_version < "3.11"\n'
                'numpy==2.0; python_version >= "3.11"\n'
                'Pillow==12.3.0\n',
                encoding="utf-8",
            )
            self.assertEqual(
                doctor._locked_versions(lock, (3, 12)),
                {"numpy": "2.0", "pillow": "12.3.0"},
            )

    def test_lock_mismatch_is_reported(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mwr-lock-mismatch-") as temporary:
            root = Path(temporary)
            lock = root / "requirements-lock.txt"
            lock.write_text("pip==0.0\n", encoding="utf-8")
            fake_python = root / "python.exe"
            fake_python.write_bytes(b"")
            payload = '{"python":[3,12],"versions":{"pip":"1.0"}}\n'
            completed = doctor.subprocess.CompletedProcess([], 0, payload, "")
            with mock.patch.object(doctor, "_run", return_value=completed):
                report = doctor._python_runtime_report(fake_python, lock)
            self.assertFalse(report["lock_ok"])
            self.assertEqual(
                report["lock_mismatches"],
                {"pip": {"expected": "0.0", "actual": "1.0"}},
            )

    def test_bootstrap_preflight_rejects_incomplete_install_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mwr-preflight-test-") as temporary:
            with self.assertRaisesRegex(bootstrap.BootstrapError, "installed skill is incomplete"):
                bootstrap._preflight(Path(temporary))

    def test_doctor_reports_missing_isolated_runtime(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mwr-doctor-test-") as temporary:
            root = Path(temporary)
            for relative in (
                "SKILL.md",
                "run.py",
                "requirements-lock.txt",
                "package.json",
                "package-lock.json",
                "scripts/render_svg.mjs",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")
            with mock.patch.object(doctor, "_node_report", return_value={
                "node_path": "node",
                "npm_path": "npm",
                "minimum_node_major": 22,
                "version_ok": True,
                "resvg_ok": True,
            }), mock.patch.object(
                doctor, "find_korean_fonts", return_value=[root / "font.ttf"]
            ) as font_probe:
                report = doctor.collect_report(root)
            font_probe.assert_called_once_with(root.resolve())
            self.assertFalse(report["ok"])
            self.assertIn("isolated Python environment is not installed", report["errors"])


if __name__ == "__main__":
    unittest.main()
