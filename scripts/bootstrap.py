#!/usr/bin/env python3
"""Install the skill runtime in place without modifying global environments."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import venv
from pathlib import Path
from typing import Sequence


MIN_PYTHON = (3, 10)
MIN_NODE_MAJOR = 22


class BootstrapError(RuntimeError):
    """An actionable, user-facing setup error."""


def _configure_utf8_console() -> None:
    """Keep non-ASCII install paths readable in agent terminals on Windows."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def _run(command: Sequence[str], *, cwd: Path | None = None) -> None:
    printable = subprocess.list2cmdline(list(command)) if os.name == "nt" else " ".join(command)
    print(f"> {printable}", flush=True)
    environment = os.environ.copy()
    environment.setdefault("PYTHONUTF8", "1")
    completed = subprocess.run(
        list(command),
        cwd=str(cwd) if cwd is not None else None,
        env=environment,
        check=False,
    )
    if completed.returncode != 0:
        raise BootstrapError(f"command failed with exit code {completed.returncode}: {printable}")


def _node_major(node: str) -> int | None:
    completed = subprocess.run(
        (node, "--version"),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    match = re.match(r"^v?(\d+)(?:\.|$)", completed.stdout.strip())
    return int(match.group(1)) if completed.returncode == 0 and match else None


def _local_python(skill_root: Path) -> Path:
    relative = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    return skill_root / ".venv" / relative


def _is_linklike(path: Path) -> bool:
    """Return true for symlinks, junctions, and other Windows reparse points."""

    try:
        if path.is_symlink():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except (OSError, RuntimeError) as exc:
        raise BootstrapError(f"cannot inspect setup path {path}: {exc}") from exc
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _assert_no_escaping_links(root: Path, purpose: str) -> None:
    """Reject setup trees that could redirect writes outside the skill runtime."""

    if not root.exists() and not root.is_symlink():
        return
    if _is_linklike(root):
        raise BootstrapError(f"refusing a linked {purpose}: {root}")
    try:
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise BootstrapError(f"cannot resolve {purpose} {root}: {exc}") from exc

    pending = [root]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            raise BootstrapError(f"cannot inspect {purpose} {current}: {exc}") from exc
        for entry in entries:
            path = Path(entry.path)
            if _is_linklike(path):
                try:
                    target = path.resolve(strict=True)
                    target.relative_to(resolved_root)
                except (OSError, RuntimeError, ValueError) as exc:
                    raise BootstrapError(
                        f"refusing an escaping link in {purpose}: {path}"
                    ) from exc
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
            except OSError as exc:
                raise BootstrapError(f"cannot inspect {purpose} {path}: {exc}") from exc


def _preflight(skill_root: Path) -> tuple[str, str]:
    if sys.version_info < MIN_PYTHON:
        raise BootstrapError(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is required")
    required = (
        "SKILL.md",
        "install.ps1",
        "requirements-lock.txt",
        "package.json",
        "package-lock.json",
        "scripts/doctor.py",
        "scripts/build_worksheet.py",
        "scripts/selftest.py",
        "scripts/test_portable_e2e.py",
        "assets/fonts/NanumGothic-Regular.ttf",
        "assets/fonts/NanumGothic-Bold.ttf",
        "assets/fonts/OFL.txt",
    )
    missing = [name for name in required if not (skill_root / name).is_file()]
    if missing:
        raise BootstrapError("installed skill is incomplete: " + ", ".join(missing))

    node = shutil.which("node")
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not node:
        raise BootstrapError(f"Node.js {MIN_NODE_MAJOR} or newer is required but was not found on PATH")
    major = _node_major(node)
    if major is None or major < MIN_NODE_MAJOR:
        raise BootstrapError(f"Node.js {MIN_NODE_MAJOR} or newer is required; detected major version {major}")
    if not npm:
        raise BootstrapError("npm was not found on PATH")
    return node, npm


def bootstrap(skill_root: Path, *, verify: bool) -> None:
    root = skill_root.resolve()
    _node, npm = _preflight(root)
    venv_root = root / ".venv"
    node_modules = root / "node_modules"
    _assert_no_escaping_links(venv_root, "virtual environment")
    _assert_no_escaping_links(node_modules, "Node dependency tree")

    python_path = _local_python(root)
    if not python_path.is_file():
        print(f"Creating isolated Python environment: {venv_root}", flush=True)
        venv.EnvBuilder(with_pip=True, clear=False, symlinks=False).create(venv_root)
    if not python_path.is_file():
        raise BootstrapError(f"virtual environment creation did not produce {python_path}")
    _assert_no_escaping_links(venv_root, "virtual environment")

    _run(
        (
            str(python_path),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--only-binary=:all:",
            "--require-virtualenv",
            "-r",
            str(root / "requirements-lock.txt"),
        ),
        cwd=root,
    )
    _run((npm, "--prefix", str(root), "ci", "--omit=dev", "--ignore-scripts"), cwd=root)
    _assert_no_escaping_links(venv_root, "virtual environment")
    _assert_no_escaping_links(node_modules, "Node dependency tree")
    _run((str(python_path), str(root / "scripts" / "doctor.py"), "--json"), cwd=root)

    if verify:
        with tempfile.TemporaryDirectory(prefix="mwr-bootstrap-verify-") as temporary:
            verification_root = Path(temporary)
            _run(
                (
                    str(python_path),
                    str(root / "run.py"),
                    "selftest.py",
                    "--out",
                    str(verification_root / "selftest"),
                ),
                cwd=root,
            )
            _run(
                (
                    str(python_path),
                    str(root / "run.py"),
                    "test_portable_e2e.py",
                    "--out",
                    str(verification_root / "portable-e2e"),
                ),
                cwd=root,
            )

    print("Math Workbook Rebuilder runtime is ready.")
    print(
        "A vision-capable agent must still open the source, observation overlay, registered "
        "diagnostic, landmark zooms, and final PDF crop during each reconstruction."
    )


def main() -> int:
    _configure_utf8_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="run the complete synthetic regression self-test after dependency setup",
    )
    args = parser.parse_args()
    try:
        bootstrap(Path(__file__).resolve().parents[1], verify=args.verify)
    except (BootstrapError, OSError, subprocess.SubprocessError) as exc:
        print(f"bootstrap failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
