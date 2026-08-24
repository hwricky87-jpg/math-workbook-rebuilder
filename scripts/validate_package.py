#!/usr/bin/env python3
"""Validate the distributable skill package without loading private work artifacts."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote


SKILL_ROOT = Path(__file__).resolve().parents[1]
SKILL_NAME = "math-workbook-rebuilder"
FORBIDDEN_SUFFIXES = {
    ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".heic"
}
EXCLUDED_PARTS = {".git", ".venv", "node_modules", "__pycache__"}
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
PORTABLE_REQUIRED = (
    "install.ps1",
    "scripts/bootstrap.py",
    "scripts/doctor.py",
    "scripts/build_worksheet.py",
    "scripts/test_portable_e2e.py",
    "scripts/test_installer_security.py",
    "references/worksheet-renderer.md",
    "assets/fonts/NanumGothic-Regular.ttf",
    "assets/fonts/NanumGothic-Bold.ttf",
    "assets/fonts/OFL.txt",
)
PINNED_ASSET_SHA256 = {
    "assets/fonts/NanumGothic-Regular.ttf": "76f45ef4a6bcff344c837c95a7dcc26e017e38b5846d5ae0cdcb5b86be2e2d31",
    "assets/fonts/NanumGothic-Bold.ttf": "f96298f9fb18e364d2370f4c3ce948ac67a2b61af992d7234bc15c42b033c674",
    "assets/fonts/OFL.txt": "90f6a909ffb2af7f6422ea20042a26180fe5520cba80bbbf75a5e660320d7924",
}


def package_files() -> list[Path]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(SKILL_ROOT), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            check=True,
            capture_output=True,
        )
        names = completed.stdout.decode("utf-8", errors="replace").split("\0")
        git_files = [SKILL_ROOT / name for name in names if name]
        # An installed skill can live inside an unrelated parent Git worktree
        # (for example a temporary verification directory).  In that case
        # `git ls-files` succeeds but returns no files for this ignored subtree.
        # Fall back to the actual installed tree instead of reporting that zero
        # files were checked.
        if git_files:
            return git_files
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    return [
        path
        for path in SKILL_ROOT.rglob("*")
        if path.is_file() and not EXCLUDED_PARTS.intersection(path.relative_to(SKILL_ROOT).parts)
    ]


def validate_frontmatter(errors: list[str]) -> None:
    path = SKILL_ROOT / "SKILL.md"
    if not path.is_file():
        errors.append("SKILL.md is missing")
        return
    content = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", content, flags=re.DOTALL)
    if not match:
        errors.append("SKILL.md frontmatter is missing or malformed")
        return
    frontmatter = match.group(1)
    name = re.search(r"^name:\s*(.+)$", frontmatter, flags=re.MULTILINE)
    description = re.search(r"^description:\s*(.+)$", frontmatter, flags=re.MULTILINE)
    if not name or name.group(1).strip() != SKILL_NAME:
        errors.append(f"SKILL.md name must be {SKILL_NAME}")
    if not description or len(description.group(1).strip()) < 40:
        errors.append("SKILL.md description is missing or not discriminating")


def validate_versions(errors: list[str]) -> None:
    package = json.loads((SKILL_ROOT / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((SKILL_ROOT / "package-lock.json").read_text(encoding="utf-8"))
    root_lock = lock.get("packages", {}).get("", {})
    versions = {package.get("version"), lock.get("version"), root_lock.get("version")}
    if len(versions) != 1 or None in versions:
        errors.append("package.json and package-lock.json versions do not match")
    if package.get("license") != "Apache-2.0" or root_lock.get("license") != "Apache-2.0":
        errors.append("package metadata must declare Apache-2.0")
    requirements = (SKILL_ROOT / "requirements-lock.txt").read_text(encoding="utf-8").splitlines()
    unpinned = [
        line for line in requirements
        if line.strip() and not line.lstrip().startswith("#") and "==" not in line.split(";", 1)[0]
    ]
    if unpinned:
        errors.append("requirements-lock.txt contains unpinned requirements")


def validate_links(files: list[Path], errors: list[str]) -> None:
    for path in files:
        if path.suffix.lower() != ".md" or not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        for raw_target in MARKDOWN_LINK.findall(content):
            target = raw_target.strip().strip("<>")
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = unquote(target.split("#", 1)[0])
            if target and not (path.parent / target).exists():
                errors.append(f"missing Markdown link target: {path.relative_to(SKILL_ROOT)} -> {target}")


def validate_public_files(files: list[Path], errors: list[str]) -> None:
    for path in files:
        relative = path.relative_to(SKILL_ROOT)
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            errors.append(f"private source/output file must not be distributed: {relative}")
        if path.name == ".env" or path.name.startswith(".env."):
            errors.append(f"environment file must not be distributed: {relative}")

    is_public_checkout = (SKILL_ROOT / ".git").exists() or (SKILL_ROOT / ".github").exists()
    if is_public_checkout:
        required = (
            "SECURITY.md",
            "CONTRIBUTING.md",
            "LICENSE",
            "NOTICE",
            ".github/workflows/ci.yml",
            ".github/dependabot.yml",
            ".github/ISSUE_TEMPLATE/config.yml",
            ".github/ISSUE_TEMPLATE/bug_report.yml",
        )
        for relative in required:
            if not (SKILL_ROOT / relative).is_file():
                errors.append(f"public repository safety file is missing: {relative}")


def validate_portable_package(errors: list[str]) -> None:
    for relative in PORTABLE_REQUIRED:
        if not (SKILL_ROOT / relative).is_file():
            errors.append(f"portable installation file is missing: {relative}")
    for relative, expected in PINNED_ASSET_SHA256.items():
        path = SKILL_ROOT / relative
        if not path.is_file():
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            errors.append(f"bundled portable asset hash changed: {relative}")


def main() -> int:
    errors: list[str] = []
    files = package_files()
    validate_frontmatter(errors)
    validate_versions(errors)
    validate_links(files, errors)
    validate_public_files(files, errors)
    validate_portable_package(errors)
    report = {"ok": not errors, "files_checked": len(files), "errors": errors}
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
