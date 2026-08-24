#!/usr/bin/env python3
"""Focused regression tests for validate_workbook content-addressed resume."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate_workbook


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class WorkbookCacheIdentityTest(unittest.TestCase):
    def test_changed_item_preserves_independent_item_and_invalidates_workbook_downstream(self) -> None:
        with tempfile.TemporaryDirectory(prefix="math-workbook-item-cache-") as temp_name:
            root = Path(temp_name)
            worker = root / "copy.py"
            worker.write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "Path(sys.argv[2]).write_bytes(Path(sys.argv[1]).read_bytes())\n",
                encoding="utf-8",
            )
            workbook = root / "workbook.txt"
            p1_source = root / "p1-source.txt"
            p2_source = root / "p2-source.txt"
            workbook.write_text("stable workbook identity\n", encoding="utf-8")
            p1_source.write_text("p1-A\n", encoding="utf-8")
            p2_source.write_text("p2-A\n", encoding="utf-8")

            def copy_stage(name: str, source: str, output: str, **extra: object) -> dict[str, object]:
                stage: dict[str, object] = {
                    "name": name,
                    "command": [sys.executable, str(worker), str(root / source), str(root / output)],
                    "inputs": [source],
                    "outputs": [output],
                }
                stage.update(extra)
                return stage

            job = {
                "schema_version": "1.0",
                "inputs": [{"path": "workbook.txt", "sha256": _sha256(workbook)}],
                "gates": {
                    "inventory": {
                        "stages": [copy_stage("inventory", "workbook.txt", "inventory.txt")]
                    },
                    "geometry": {
                        "stages": [
                            copy_stage("p1-preprocess", "p1-source.txt", "p1-work.txt"),
                            copy_stage(
                                "p1-render",
                                "p1-work.txt",
                                "p1-final.txt",
                                depends_on=["geometry/p1-preprocess"],
                            ),
                            copy_stage("p2-preprocess", "p2-source.txt", "p2-work.txt"),
                            copy_stage(
                                "p2-render",
                                "p2-work.txt",
                                "p2-final.txt",
                                depends_on=["geometry/p2-preprocess"],
                            ),
                        ]
                    },
                    "pdf": {
                        "stages": [copy_stage("build-pdf", "p1-final.txt", "review.pdf")]
                    },
                    "delivery": {
                        "stages": [copy_stage("delivery", "review.pdf", "delivery.json")]
                    },
                    "revision": {"not_applicable": "synthetic first build"},
                },
            }
            job_path = root / "job.json"
            state_path = root / "state.json"
            _write_json(job_path, job)

            first = validate_workbook.run_job(job_path, state_path=state_path)
            self.assertTrue(first["ok"], first["errors"])
            self.assertTrue(all(not stage["cached"] for stage in first["stages"]))

            unchanged = validate_workbook.run_job(job_path, state_path=state_path)
            self.assertTrue(unchanged["ok"], unchanged["errors"])
            self.assertTrue(all(stage["cached"] for stage in unchanged["stages"]))

            p1_source.write_text("p1-B\n", encoding="utf-8")
            changed = validate_workbook.run_job(job_path, state_path=state_path)
            self.assertTrue(changed["ok"], changed["errors"])
            by_key = {stage["key"]: stage for stage in changed["stages"]}
            self.assertTrue(by_key["inventory/inventory"]["cached"])
            self.assertFalse(by_key["geometry/p1-preprocess"]["cached"])
            self.assertFalse(by_key["geometry/p1-render"]["cached"])
            self.assertTrue(by_key["geometry/p2-preprocess"]["cached"])
            self.assertTrue(by_key["geometry/p2-render"]["cached"])
            self.assertFalse(by_key["pdf/build-pdf"]["cached"])
            self.assertFalse(by_key["delivery/delivery"]["cached"])
            self.assertEqual((root / "p1-final.txt").read_text(encoding="utf-8"), "p1-B\n")
            self.assertEqual((root / "p2-final.txt").read_text(encoding="utf-8"), "p2-A\n")

    def test_imported_helper_change_invalidates_resume(self) -> None:
        with tempfile.TemporaryDirectory(prefix="math-workbook-helper-cache-") as temp_name:
            root = Path(temp_name)
            scripts_root = root / "fake-skill" / "scripts"
            scripts_root.mkdir(parents=True)
            helper = scripts_root / "helper.py"
            worker = scripts_root / "worker.py"
            helper.write_text("VALUE = 'A'\n", encoding="utf-8")
            worker.write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "from helper import VALUE\n"
                "Path(sys.argv[1]).write_text(VALUE, encoding='utf-8')\n",
                encoding="utf-8",
            )

            source = root / "source.txt"
            source.write_text("source\n", encoding="utf-8")
            job = {
                "schema_version": "1.0",
                "inputs": [{"path": "source.txt", "sha256": _sha256(source)}],
                "gates": {
                    "inventory": {"stages": []},
                    "geometry": {"not_applicable": "synthetic helper cache fixture"},
                    "pdf": {"stages": []},
                    "delivery": {"stages": []},
                    "revision": {"not_applicable": "synthetic helper cache fixture"},
                },
            }
            outputs = {
                "inventory": "inventory.txt",
                "pdf": "pdf.txt",
                "delivery": "delivery.txt",
            }
            for gate, output in outputs.items():
                job["gates"][gate]["stages"].append(
                    {
                        "name": gate,
                        "command": [sys.executable, str(worker), str(root / output)],
                        "env": {"PYTHONDONTWRITEBYTECODE": "1"},
                        "outputs": [output],
                    }
                )

            job_path = root / "job.json"
            state_path = root / "state.json"
            _write_json(job_path, job)
            with mock.patch.object(validate_workbook, "SKILL_SCRIPTS_ROOT", scripts_root):
                first = validate_workbook.run_job(job_path, state_path=state_path)
                self.assertTrue(first["ok"])
                self.assertTrue(all(not stage["cached"] for stage in first["stages"]))
                self.assertTrue(
                    all((root / output).read_text(encoding="utf-8") == "A" for output in outputs.values())
                )

                unchanged = validate_workbook.run_job(job_path, state_path=state_path)
                self.assertTrue(unchanged["ok"])
                self.assertTrue(all(stage["cached"] for stage in unchanged["stages"]))

                helper.write_text("VALUE = 'B'\n", encoding="utf-8")
                helper_changed = validate_workbook.run_job(job_path, state_path=state_path)
                self.assertTrue(helper_changed["ok"])
                self.assertTrue(all(not stage["cached"] for stage in helper_changed["stages"]))
                self.assertTrue(
                    all((root / output).read_text(encoding="utf-8") == "B" for output in outputs.values())
                )

    def test_script_package_and_job_evidence_changes_invalidate_resume(self) -> None:
        with tempfile.TemporaryDirectory(prefix="math-workbook-cache-") as temp_name:
            root = Path(temp_name)
            source = root / "source.txt"
            source.write_text("source\n", encoding="utf-8")
            input_descriptor = {"path": "source.txt", "sha256": _sha256(source)}
            build_id = validate_workbook._canonical_hash(
                {"schema_version": "1.0", "inputs": [input_descriptor]}
            )

            evidence = root / "evidence.json"
            _write_json(evidence, {"build_id": build_id, "review": "one"})
            worker = root / "worker.py"
            worker.write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "Path(sys.argv[1]).write_text('A', encoding='utf-8')\n",
                encoding="utf-8",
            )
            _write_json(root / "package.json", {"name": "cache-fixture", "version": "1.0.0"})
            _write_json(root / "package-lock.json", {"name": "cache-fixture", "lockfileVersion": 3})

            outputs = {
                "inventory": "inventory.txt",
                "pdf": "pdf.txt",
                "delivery": "delivery.txt",
            }
            job = {
                "schema_version": "1.0",
                "inputs": [input_descriptor],
                "evidence": [
                    {
                        "path": "evidence.json",
                        "sha256": _sha256(evidence),
                        "build_id": build_id,
                    }
                ],
                "gates": {
                    "inventory": {"stages": []},
                    "geometry": {"not_applicable": "synthetic cache identity fixture"},
                    "pdf": {"stages": []},
                    "delivery": {"stages": []},
                    "revision": {"not_applicable": "synthetic cache identity fixture"},
                },
            }
            for gate, output in outputs.items():
                job["gates"][gate]["stages"].append(
                    {
                        "name": gate,
                        "command": [sys.executable, str(worker), str(root / output)],
                        "outputs": [output],
                    }
                )
            job_path = root / "job.json"
            state_path = root / "state.json"
            _write_json(job_path, job)

            first = validate_workbook.run_job(job_path, state_path=state_path)
            self.assertTrue(first["ok"])
            self.assertTrue(all(not stage["cached"] for stage in first["stages"]))
            self.assertTrue(all((root / output).read_text(encoding="utf-8") == "A" for output in outputs.values()))
            runtime = first["stages"][0]["execution_identity"]["runtime"]
            self.assertIsNotNone(runtime["resolved"])
            self.assertRegex(runtime["sha256"], r"^[0-9a-f]{64}$")
            self.assertIn("version", runtime)
            self.assertTrue(first["stages"][0]["execution_identity"]["package_files"])

            unchanged = validate_workbook.run_job(job_path, state_path=state_path)
            self.assertTrue(unchanged["ok"])
            self.assertTrue(all(stage["cached"] for stage in unchanged["stages"]))

            worker.write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "Path(sys.argv[1]).write_text('B', encoding='utf-8')\n",
                encoding="utf-8",
            )
            script_changed = validate_workbook.run_job(job_path, state_path=state_path)
            self.assertTrue(script_changed["ok"])
            self.assertTrue(all(not stage["cached"] for stage in script_changed["stages"]))
            self.assertTrue(all((root / output).read_text(encoding="utf-8") == "B" for output in outputs.values()))

            _write_json(root / "package-lock.json", {"name": "cache-fixture", "lockfileVersion": 3, "changed": True})
            lock_changed = validate_workbook.run_job(job_path, state_path=state_path)
            self.assertTrue(lock_changed["ok"])
            self.assertTrue(all(not stage["cached"] for stage in lock_changed["stages"]))

            _write_json(evidence, {"build_id": build_id, "review": "two"})
            job["evidence"][0]["sha256"] = _sha256(evidence)
            _write_json(job_path, job)
            evidence_changed = validate_workbook.run_job(job_path, state_path=state_path)
            self.assertTrue(evidence_changed["ok"])
            self.assertTrue(all(not stage["cached"] for stage in evidence_changed["stages"]))


if __name__ == "__main__":
    unittest.main()
