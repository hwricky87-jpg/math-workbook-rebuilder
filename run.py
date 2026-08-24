#!/usr/bin/env python3
"""Run a bundled skill script, preferring the skill-local virtual environment."""

from __future__ import annotations

import os
import runpy
import subprocess
import sys
from pathlib import Path


def _local_python(skill_root: Path) -> Path | None:
    candidates = (
        skill_root / ".venv" / "Scripts" / "python.exe",
        skill_root / ".venv" / "bin" / "python",
    )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python run.py <script.py> [arguments ...]", file=sys.stderr)
        return 2

    skill_root = Path(__file__).resolve().parent
    scripts_root = (skill_root / "scripts").resolve()
    script_name = sys.argv[1]
    if Path(script_name).name != script_name or not script_name.endswith(".py"):
        print("script must be a Python filename from the bundled scripts directory", file=sys.stderr)
        return 2

    target = (scripts_root / script_name).resolve()
    if target.parent != scripts_root or not target.is_file():
        print(f"unknown bundled script: {script_name}", file=sys.stderr)
        return 2

    local_python = _local_python(skill_root)
    if local_python is not None and local_python.resolve() != Path(sys.executable).resolve():
        command = [str(local_python), str(Path(__file__).resolve()), *sys.argv[1:]]
        if os.name == "nt":
            return subprocess.run(command, check=False).returncode
        os.execv(
            str(local_python),
            command,
        )

    sys.path.insert(0, str(scripts_root))
    sys.argv = [str(target), *sys.argv[2:]]
    runpy.run_path(str(target), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
