#!/usr/bin/env python3
"""Check whether an installed skill has its isolated runtime and Korean fonts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence


MIN_PYTHON = (3, 10)
MIN_NODE_MAJOR = 22
PYTHON_IMPORTS = ("numpy", "PIL", "fitz", "cv2")
PYTHON_DISTRIBUTIONS = ("pip", "numpy", "Pillow", "PyMuPDF", "opencv-python-headless")
BUNDLED_FONT_SHA256 = {
    "NanumGothic-Regular.ttf": "76f45ef4a6bcff344c837c95a7dcc26e017e38b5846d5ae0cdcb5b86be2e2d31",
    "NanumGothic-Bold.ttf": "f96298f9fb18e364d2370f4c3ce948ac67a2b61af992d7234bc15c42b033c674",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def local_python(skill_root: Path) -> Path:
    relative = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    return skill_root / ".venv" / relative


def _run(command: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.setdefault("PYTHONUTF8", "1")
    return subprocess.run(
        list(command),
        cwd=str(cwd) if cwd is not None else None,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _version_major(value: str) -> int | None:
    match = re.match(r"^v?(\d+)(?:\.|$)", value.strip())
    return int(match.group(1)) if match else None


def bundled_font_paths(skill_root: Path) -> tuple[Path, Path]:
    return (
        skill_root / "assets" / "fonts" / "NanumGothic-Regular.ttf",
        skill_root / "assets" / "fonts" / "NanumGothic-Bold.ttf",
    )


def _font_candidates(skill_root: Path) -> list[Path]:
    candidates: list[Path] = []
    candidates.extend(bundled_font_paths(skill_root))
    configured = os.environ.get("MWR_KOREAN_FONT")
    if configured:
        candidates.append(Path(configured).expanduser())

    windows_root = Path(os.environ.get("WINDIR", "C:/Windows"))
    candidates.extend(
        windows_root / "Fonts" / name
        for name in ("malgun.ttf", "malgunbd.ttf", "NanumGothic.ttf")
    )
    candidates.extend(
        Path(value)
        for value in (
            "/System/Library/Fonts/AppleSDGothicNeo.ttc",
            "/Library/Fonts/AppleSDGothicNeo.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJKkr-Regular.otf",
            "/usr/share/fonts/truetype/noto/NotoSansKR-Regular.ttf",
            "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        )
    )
    return candidates


def find_korean_fonts(skill_root: Path) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    for candidate in _font_candidates(skill_root):
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate.absolute()
        key = os.path.normcase(str(resolved))
        if candidate.is_file() and key not in seen:
            found.append(resolved)
            seen.add(key)

    fc_match = shutil.which("fc-match")
    if fc_match:
        for family in ("Noto Sans CJK KR", "Noto Sans KR", "NanumGothic"):
            completed = _run((fc_match, "-f", "%{file}\n", family))
            if completed.returncode != 0:
                continue
            for line in completed.stdout.splitlines():
                candidate = Path(line.strip())
                if not candidate.is_file():
                    continue
                key = os.path.normcase(str(candidate.resolve()))
                if key not in seen:
                    found.append(candidate.resolve())
                    seen.add(key)
    return found


def _marker_applies(marker: str, python_version: tuple[int, int]) -> bool:
    match = re.fullmatch(r'python_version\s*(==|>=|<=|<|>)\s*["\'](\d+)\.(\d+)["\']', marker.strip())
    if not match:
        raise ValueError(f"unsupported environment marker: {marker}")
    operator, major, minor = match.groups()
    expected = (int(major), int(minor))
    return {
        "==": python_version == expected,
        ">=": python_version >= expected,
        "<=": python_version <= expected,
        "<": python_version < expected,
        ">": python_version > expected,
    }[operator]


def _locked_versions(lock_path: Path, python_version: tuple[int, int]) -> dict[str, str]:
    expected: dict[str, str] = {}
    for raw_line in lock_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        requirement, separator, marker = line.partition(";")
        if separator and not _marker_applies(marker, python_version):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s]+)", requirement.strip())
        if not match:
            raise ValueError(f"lock entry is not an exact version: {raw_line}")
        name, version = match.groups()
        expected[name.casefold().replace("_", "-")] = version
    return expected


def _python_runtime_report(python_path: Path, lock_path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(python_path),
        "present": python_path.is_file(),
        "imports_ok": False,
        "pip_check_ok": False,
        "lock_ok": False,
        "versions": {},
    }
    if not python_path.is_file():
        result["error"] = "skill-local .venv is missing"
        return result

    probe = (
        "import importlib, importlib.metadata as m, json; "
        f"imports={PYTHON_IMPORTS!r}; dists={PYTHON_DISTRIBUTIONS!r}; "
        "[importlib.import_module(name) for name in imports]; "
        "print(json.dumps({'python':[__import__('sys').version_info.major,__import__('sys').version_info.minor],"
        "'versions':{name:m.version(name) for name in dists}}, sort_keys=True))"
    )
    completed = _run((str(python_path), "-c", probe))
    result["imports_ok"] = completed.returncode == 0
    if completed.returncode == 0:
        try:
            # Some binary packages emit deprecation notices on import.  The probe's
            # final non-empty line is the machine-readable payload.
            output_lines = [line for line in completed.stdout.splitlines() if line.strip()]
            payload = json.loads(output_lines[-1] if output_lines else "")
            result["python_version"] = payload["python"]
            result["versions"] = payload["versions"]
            expected = _locked_versions(lock_path, tuple(payload["python"]))
            actual = {
                name.casefold().replace("_", "-"): version
                for name, version in result["versions"].items()
            }
            mismatches = {
                name: {"expected": version, "actual": actual.get(name)}
                for name, version in expected.items()
                if actual.get(name) != version
            }
            result["lock_ok"] = not mismatches
            result["lock_mismatches"] = mismatches
        except json.JSONDecodeError:
            result["error"] = "dependency probe returned invalid JSON"
        except (KeyError, OSError, TypeError, ValueError) as exc:
            result["error"] = f"dependency lock validation failed: {exc}"
    else:
        result["error"] = completed.stderr.strip() or completed.stdout.strip()

    pip_check = _run((str(python_path), "-m", "pip", "check"))
    result["pip_check_ok"] = pip_check.returncode == 0
    if pip_check.returncode != 0:
        result["pip_check"] = pip_check.stderr.strip() or pip_check.stdout.strip()
    return result


def _node_report(skill_root: Path) -> dict[str, Any]:
    node = shutil.which("node")
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    result: dict[str, Any] = {
        "node_path": node,
        "npm_path": npm,
        "minimum_node_major": MIN_NODE_MAJOR,
        "version_ok": False,
        "resvg_ok": False,
    }
    if not node:
        result["error"] = "node was not found on PATH"
        return result

    version = _run((node, "--version"))
    version_text = version.stdout.strip() or version.stderr.strip()
    major = _version_major(version_text)
    result["node_version"] = version_text
    result["version_ok"] = version.returncode == 0 and major is not None and major >= MIN_NODE_MAJOR
    if not npm:
        result["error"] = "npm was not found on PATH"
        return result

    package_json = skill_root / "package.json"
    probe = (
        "const {createRequire}=require('node:module');"
        "const req=createRequire(process.argv[1]);"
        "const mod=req('@resvg/resvg-js');"
        "if(typeof mod.Resvg!=='function')process.exit(3);"
    )
    resvg = _run(
        (node, "--input-type=commonjs", "-e", probe, str(package_json.resolve())),
        cwd=skill_root,
    )
    result["resvg_ok"] = resvg.returncode == 0
    if resvg.returncode != 0:
        result["resvg_error"] = resvg.stderr.strip() or resvg.stdout.strip()
    return result


def collect_report(skill_root: Path | None = None) -> dict[str, Any]:
    root = (skill_root or Path(__file__).resolve().parents[1]).resolve()
    required = (
        "SKILL.md",
        "install.ps1",
        "run.py",
        "requirements-lock.txt",
        "package.json",
        "package-lock.json",
        "scripts/bootstrap.py",
        "scripts/build_worksheet.py",
        "scripts/render_svg.mjs",
        "assets/fonts/NanumGothic-Regular.ttf",
        "assets/fonts/NanumGothic-Bold.ttf",
        "assets/fonts/OFL.txt",
    )
    missing = [name for name in required if not (root / name).is_file()]
    python_supported = sys.version_info >= MIN_PYTHON
    python_runtime = _python_runtime_report(local_python(root), root / "requirements-lock.txt")
    node_runtime = _node_report(root)
    fonts = find_korean_fonts(root)
    bundled_fonts = [path.resolve() for path in bundled_font_paths(root) if path.is_file()]
    bundled_font_records: list[dict[str, Any]] = []
    for path in bundled_fonts:
        digest = _sha256(path)
        bundled_font_records.append(
            {
                "path": str(path),
                "sha256": digest,
                "release_hash_match": digest == BUNDLED_FONT_SHA256.get(path.name),
            }
        )
    configured_font = os.environ.get("MWR_KOREAN_FONT")
    bundled_fonts_match = len(bundled_font_records) == 2 and all(
        record["release_hash_match"] for record in bundled_font_records
    )
    deterministic_fonts = bundled_fonts_match or bool(
        configured_font and Path(configured_font).expanduser().is_file()
    )

    errors: list[str] = []
    if missing:
        errors.append("missing skill files: " + ", ".join(missing))
    if not python_supported:
        errors.append(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is required")
    if not python_runtime["present"]:
        errors.append("isolated Python environment is not installed")
    elif (
        not python_runtime["imports_ok"]
        or not python_runtime["pip_check_ok"]
        or not python_runtime["lock_ok"]
    ):
        errors.append("isolated Python dependencies are incomplete or inconsistent")
    if not node_runtime["version_ok"]:
        errors.append(f"Node.js {MIN_NODE_MAJOR} or newer is required")
    if not node_runtime.get("npm_path"):
        errors.append("npm is required")
    if not node_runtime["resvg_ok"]:
        errors.append("the skill-local @resvg/resvg-js runtime is not installed")
    if not fonts:
        errors.append(
            "a Korean font was not found; install Noto Sans KR/CJK KR or set MWR_KOREAN_FONT"
        )
    if bundled_fonts and not bundled_fonts_match:
        errors.append("bundled Korean font hashes do not match this release")

    warnings: list[str] = []
    if fonts and not deterministic_fonts:
        warnings.append(
            "only a system Korean font was found; bundled fonts or MWR_KOREAN_FONT are needed "
            "for stable cross-machine text metrics"
        )

    return {
        "ok": not errors,
        "skill_root": str(root),
        "skill_files": {"ok": not missing, "missing": missing},
        "host_python": {
            "version": ".".join(str(value) for value in sys.version_info[:3]),
            "minimum": f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
            "ok": python_supported,
        },
        "isolated_python": python_runtime,
        "node": node_runtime,
        "korean_fonts": {
            "ok": bool(fonts),
            "deterministic": deterministic_fonts,
            "bundled": [str(path) for path in bundled_fonts],
            "bundled_records": bundled_font_records,
            "files": [str(path) for path in fonts],
        },
        "vision_review": {
            "required": True,
            "machine_detectable": False,
            "instruction": (
                "Use a vision-capable host and actually open the source, named observation overlay, "
                "registered diagnostic, landmark zooms, and final PDF crop."
            ),
        },
        "errors": errors,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the full machine-readable report")
    args = parser.parse_args()
    report = collect_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        print("READY" if report["ok"] else "NOT READY")
        for error in report["errors"]:
            print(f"- {error}")
        for warning in report["warnings"]:
            print(f"- warning: {warning}")
        print(f"- skill root: {report['skill_root']}")
        print(f"- Korean fonts: {len(report['korean_fonts']['files'])}")
        print(f"- vision: {report['vision_review']['instruction']}")
        if not report["ok"]:
            print("Run: python scripts/bootstrap.py --verify")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
