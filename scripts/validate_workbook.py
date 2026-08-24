#!/usr/bin/env python3
"""Run a resumable, content-addressed workbook production and QA pipeline.

The orchestrator executes commands without a shell in this fixed gate order:
``inventory -> geometry -> pdf -> delivery -> revision``.  A gate may contain
fine-grained stages named ``preprocess``, ``observe-s1p2``, ``render-s1p2``,
``overlay-s1p2``, ``compare-s1p2``, ``review-s1p2``, and so on.  Successful
stages are cached using their command, current input hashes, actual declared
stage dependencies, invoked executable/script bytes, runtime/package identity,
job-level evidence, and the current workbook build ID.  Dependencies are
inferred when a stage input/evidence path consumes a preceding stage output;
``depends_on`` may declare additional ``gate/name`` edges.  PDF, delivery, and
revision stages are workbook-wide downstream stages and therefore depend on
all preceding stages.  A stage with no dependency declarations falls back to
all preceding stages (fail closed).  A resumed run can consequently preserve
an independent unchanged item while invalidating its changed item chain and
workbook-wide downstream artifacts.

Minimal job configuration (paths are relative to the job file):

.. code-block:: json

  {
    "schema_version": "1.0",
    "inputs": [{"path": "source.pdf", "sha256": "64 hex characters"}],
    "gates": {
      "inventory": {"stages": [{
        "name": "inventory",
        "command": ["{python}", "{skill_root}/scripts/validate_inventory.py",
                    "inventory.json", "--report", "qa/inventory.json"],
        "inputs": ["inventory.json"],
        "outputs": ["qa/inventory.json"]
      }]},
      "geometry": {"stages": [
        {"name": "preprocess-s1p2", "command": ["..."],
         "inputs": ["source/s1p2.png"], "outputs": ["work/s1p2.png"]},
        {"name": "compare-s1p2", "command": ["..."],
         "inputs": ["work/s1p2.png"],
         "depends_on": ["geometry/preprocess-s1p2"],
         "outputs": ["qa/s1p2.json"]}
      ]},
      "pdf": {"stages": [{"name": "build-pdf", "command": ["..."],
                            "outputs": ["out/review.pdf"]}]},
      "delivery": {"stages": [{"name": "delivery", "command": ["..."],
                                 "outputs": ["qa/delivery.json"]}]},
      "revision": {"not_applicable": "first complete build; no baseline"}
    }
  }

Placeholders supported in command, cwd, and environment strings are
``{python}``, ``{job_dir}``, ``{skill_root}``, and ``{build_id}``.  Use
``--fresh`` to ignore the cache; safe resume is the default.  Cached output
files are rehashed before a stage is skipped.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0"
GATE_ORDER = ("inventory", "geometry", "pdf", "delivery", "revision")
REQUIRED_GATES = frozenset({"inventory", "pdf", "delivery"})
WORKBOOK_WIDE_DOWNSTREAM_GATES = frozenset({"pdf", "delivery", "revision"})
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
STAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
MAX_CAPTURE_CHARS = 20_000
CODE_FILE_SUFFIXES = frozenset(
    {
        ".py",
        ".pyw",
        ".js",
        ".mjs",
        ".cjs",
        ".jsx",
        ".ts",
        ".tsx",
        ".mts",
        ".cts",
        ".ps1",
        ".sh",
        ".bash",
        ".bat",
        ".cmd",
        ".rb",
        ".pl",
        ".php",
        ".lua",
        ".r",
    }
)
SKILL_SCRIPTS_ROOT = Path(__file__).resolve().parent
PACKAGE_IDENTITY_FILES = (
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
)
VERSION_RUNTIME_NAMES = frozenset(
    {
        "node",
        "nodejs",
        "npm",
        "npx",
        "perl",
        "php",
        "python",
        "python2",
        "python3",
        "pythonw",
        "ruby",
    }
)
INTERPRETER_NAMES = frozenset(
    {
        "bash",
        "node",
        "nodejs",
        "perl",
        "php",
        "python",
        "python2",
        "python3",
        "pythonw",
        "ruby",
        "sh",
    }
)
_FILE_IDENTITY_CACHE: dict[tuple[str, int, int], dict[str, Any]] = {}
_RUNTIME_VERSION_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


class JobError(Exception):
    """A deterministic configuration, identity, or evidence failure."""

    def __init__(self, code: str, path: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.issue = _issue(code, path, message, **details)


def _issue(code: str, path: str, message: str, **details: Any) -> dict[str, Any]:
    """Create a stable machine-readable issue."""
    result: dict[str, Any] = {"code": code, "path": path, "message": message}
    if details:
        result["details"] = details
    return result


def _sha256(path: Path) -> str:
    """Hash a file in bounded chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_identity(path: Path) -> dict[str, Any]:
    """Return a content identity for executable code or dependency metadata."""
    resolved = path.resolve()
    stat = resolved.stat()
    cache_key = (str(resolved), stat.st_size, stat.st_mtime_ns)
    cached = _FILE_IDENTITY_CACHE.get(cache_key)
    if cached is not None:
        return copy.deepcopy(cached)
    identity = {
        "path": str(resolved),
        "sha256": _sha256(resolved),
        "size": stat.st_size,
    }
    _FILE_IDENTITY_CACHE[cache_key] = identity
    return copy.deepcopy(identity)


def _resolve_command_path(value: str, cwd: Path, env: dict[str, str]) -> Path | None:
    """Resolve an executable or explicit command file without executing it."""
    raw = Path(value)
    has_parent = raw.parent != Path(".")
    if raw.is_absolute() or has_parent:
        candidate = raw if raw.is_absolute() else cwd / raw
        return candidate.resolve() if candidate.is_file() else None
    found = shutil.which(value, path=env.get("PATH"))
    return Path(found).resolve() if found else None


def _command_argument_path(value: str, cwd: Path) -> Path | None:
    """Resolve a command argument only when it names an existing regular file."""
    if not value or "\x00" in value:
        return None
    raw = Path(value)
    candidate = raw if raw.is_absolute() else cwd / raw
    try:
        return candidate.resolve() if candidate.is_file() else None
    except OSError:
        return None


def _runtime_version(executable: Path, env: dict[str, str]) -> dict[str, Any]:
    """Capture a stable, bounded version string for known language runtimes."""
    stem = executable.stem.casefold()
    if stem not in VERSION_RUNTIME_NAMES:
        return {"probed": False}
    executable_sha = _file_identity(executable)["sha256"]
    cache_key = (str(executable), executable_sha)
    cached = _RUNTIME_VERSION_CACHE.get(cache_key)
    if cached is not None:
        return copy.deepcopy(cached)
    try:
        completed = subprocess.run(
            [str(executable), "--version"],
            env=env,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        combined = "\n".join(
            value.strip() for value in (completed.stdout, completed.stderr) if value.strip()
        )
        result: dict[str, Any] = {
            "probed": True,
            "exit_code": completed.returncode,
            "output": combined[:4096],
            "output_sha256": hashlib.sha256(combined.encode("utf-8")).hexdigest(),
            "truncated": len(combined) > 4096,
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = {
            "probed": True,
            "error": type(exc).__name__,
        }
    _RUNTIME_VERSION_CACHE[cache_key] = result
    return copy.deepcopy(result)


def _nearest_package_files(anchor: Path) -> list[Path]:
    """Find the nearest Node package manifest and its lock file(s)."""
    current = anchor if anchor.is_dir() else anchor.parent
    while True:
        manifest = current / "package.json"
        if manifest.is_file():
            return [
                current / name
                for name in PACKAGE_IDENTITY_FILES
                if (current / name).is_file()
            ]
        if current.parent == current:
            return []
        current = current.parent


def _skill_scripts_identity(root: Path | None = None) -> dict[str, Any]:
    """Hash every executable source file under the skill's scripts tree once."""
    scripts_root = (root or SKILL_SCRIPTS_ROOT).resolve()
    if not scripts_root.is_dir():
        raise JobError(
            "SKILL_SCRIPTS_NOT_FOUND",
            "skill_scripts",
            f"skill scripts directory does not exist: {scripts_root}",
        )

    try:
        paths = sorted(
            (
                path
                for path in scripts_root.rglob("*")
                if path.is_file() and path.suffix.casefold() in CODE_FILE_SUFFIXES
            ),
            key=lambda path: path.relative_to(scripts_root).as_posix().casefold(),
        )
        files = []
        for path in paths:
            identity = _file_identity(path)
            files.append(
                {
                    "path": path.relative_to(scripts_root).as_posix(),
                    "sha256": identity["sha256"],
                    "size": identity["size"],
                }
            )
    except OSError as exc:
        raise JobError(
            "SKILL_SCRIPTS_READ_ERROR",
            "skill_scripts",
            f"cannot inventory skill scripts: {exc}",
        ) from exc

    return {
        "root": str(scripts_root),
        "sha256": _canonical_hash({"files": files}),
        "file_count": len(files),
        "total_size": sum(item["size"] for item in files),
    }


def _execution_identity(
    spec: dict[str, Any],
    skill_scripts_identity: dict[str, Any],
) -> dict[str, Any]:
    """Bind a stage fingerprint to the code and runtime that will execute it."""
    command = spec["command"]
    cwd = spec["cwd"]
    env = os.environ.copy()
    env.update(spec["env"])

    executable = _resolve_command_path(command[0], cwd, env)
    runtime: dict[str, Any] = {"requested": command[0]}
    if executable is None:
        runtime["resolved"] = None
    else:
        executable_identity = _file_identity(executable)
        runtime["resolved"] = executable_identity.pop("path")
        runtime.update(executable_identity)
        runtime["version"] = _runtime_version(executable, env)

    code_paths: dict[str, Path] = {}
    for token in command[1:]:
        path = _command_argument_path(token, cwd)
        if path is not None and path.suffix.casefold() in CODE_FILE_SUFFIXES:
            code_paths[str(path).casefold()] = path

    runtime_name = executable.stem.casefold() if executable is not None else Path(command[0]).stem.casefold()
    if runtime_name in INTERPRETER_NAMES:
        # Also cover extensionless scripts invoked as the interpreter's first
        # existing positional file. Inline ``-c`` code is already in command.
        for token in command[1:]:
            if token.startswith("-"):
                continue
            path = _command_argument_path(token, cwd)
            if path is not None:
                code_paths[str(path).casefold()] = path
                break

    code_files = [
        _file_identity(path)
        for _, path in sorted(code_paths.items(), key=lambda item: item[0])
    ]

    package_paths: dict[str, Path] = {}
    for anchor in [cwd, *code_paths.values()]:
        for path in _nearest_package_files(anchor):
            package_paths[str(path.resolve()).casefold()] = path.resolve()
    package_files = [
        _file_identity(path)
        for _, path in sorted(package_paths.items(), key=lambda item: item[0])
    ]

    return {
        "runtime": runtime,
        "code_files": code_files,
        "package_files": package_files,
        "skill_scripts": copy.deepcopy(skill_scripts_identity),
    }


def _orchestrator_identity() -> dict[str, Any]:
    """Identify the cache engine itself so semantic upgrades invalidate state."""
    return {
        "script": _file_identity(Path(__file__)),
        "python": {
            "executable": _file_identity(Path(sys.executable)),
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "cache_tag": getattr(sys.implementation, "cache_tag", None),
        },
    }


def _canonical_hash(value: Any) -> str:
    """Hash JSON data using a stable UTF-8 canonical representation."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_write_json(path: Path, value: Any) -> None:
    """Atomically replace a JSON state/report file on Windows and POSIX."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            Path(temp_name).unlink()
        except FileNotFoundError:
            pass


def _read_json(path: Path, label: str) -> Any:
    """Read JSON and translate filesystem/parser exceptions into JobError."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise JobError("FILE_NOT_FOUND", label, f"file does not exist: {path}") from exc
    except OSError as exc:
        raise JobError("FILE_READ_ERROR", label, f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise JobError(
            "JSON_INVALID",
            label,
            f"invalid JSON at line {exc.lineno}, column {exc.colno}",
        ) from exc


def _resolve(base: Path, value: Any, label: str) -> Path:
    """Resolve a required string path against the job directory."""
    if not isinstance(value, str) or not value.strip():
        raise JobError("PATH_INVALID", label, "path must be a non-empty string")
    path = Path(value)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _logical_path(value: str) -> str:
    """Normalize a configured path for stable build material."""
    return Path(value).as_posix()


def _substitute(value: str, replacements: dict[str, str], label: str) -> str:
    """Expand only documented placeholders and reject spelling mistakes."""
    unknown = sorted(set(PLACEHOLDER_RE.findall(value)) - set(replacements))
    if unknown:
        raise JobError(
            "PLACEHOLDER_UNKNOWN",
            label,
            "unsupported placeholder in job configuration",
            placeholders=unknown,
        )
    for name, replacement in replacements.items():
        value = value.replace("{" + name + "}", replacement)
    return value


def _artifact_descriptor(raw: Any, label: str) -> dict[str, Any]:
    """Normalize a string or object artifact descriptor."""
    if isinstance(raw, str):
        return {"path": raw}
    if not isinstance(raw, dict):
        raise JobError("ARTIFACT_INVALID", label, "artifact must be a path string or object")
    if not isinstance(raw.get("path"), str) or not raw["path"].strip():
        raise JobError("ARTIFACT_PATH_MISSING", f"{label}.path", "artifact path is required")
    return dict(raw)


def _hash_artifacts(
    values: Any,
    base: Path,
    label: str,
    *,
    require_declared_sha: bool = False,
    require_build_id: str | None = None,
) -> list[dict[str, str]]:
    """Resolve, hash, and optionally authenticate configured artifacts."""
    if values is None:
        values = []
    if not isinstance(values, list):
        raise JobError("ARTIFACT_LIST_INVALID", label, "artifact collection must be a list")

    results: list[dict[str, str]] = []
    logical_seen: set[str] = set()
    for index, raw in enumerate(values):
        item_label = f"{label}[{index}]"
        descriptor = _artifact_descriptor(raw, item_label)
        configured_path = descriptor["path"]
        logical = _logical_path(configured_path)
        if logical in logical_seen:
            raise JobError("ARTIFACT_DUPLICATE", item_label, f"duplicate artifact path: {logical}")
        logical_seen.add(logical)
        path = _resolve(base, configured_path, f"{item_label}.path")
        if not path.is_file():
            raise JobError("ARTIFACT_NOT_FOUND", item_label, f"artifact does not exist: {path}")
        actual_sha = _sha256(path)

        declared_sha = descriptor.get("sha256")
        if require_declared_sha and declared_sha is None:
            raise JobError("ARTIFACT_SHA256_MISSING", f"{item_label}.sha256", "declared SHA-256 is required")
        if declared_sha is not None:
            if not isinstance(declared_sha, str) or not SHA256_RE.fullmatch(declared_sha):
                raise JobError(
                    "ARTIFACT_SHA256_INVALID",
                    f"{item_label}.sha256",
                    "SHA-256 must be 64 hexadecimal characters",
                )
            if declared_sha.lower() != actual_sha:
                raise JobError(
                    "ARTIFACT_SHA256_STALE",
                    f"{item_label}.sha256",
                    "declared artifact hash does not match the current file",
                    declared=declared_sha.lower(),
                    actual=actual_sha,
                )

        declared_build = descriptor.get("build_id")
        if require_build_id is not None:
            if not isinstance(declared_build, str) or declared_build != require_build_id:
                raise JobError(
                    "EVIDENCE_BUILD_ID_STALE",
                    f"{item_label}.build_id",
                    "evidence build_id does not match the current input-derived build_id",
                    declared=declared_build,
                    actual=require_build_id,
                )

        json_field = descriptor.get("json_build_id_field")
        if json_field is not None:
            if not isinstance(json_field, str) or not json_field.strip():
                raise JobError(
                    "EVIDENCE_BUILD_FIELD_INVALID",
                    f"{item_label}.json_build_id_field",
                    "json_build_id_field must be a non-empty top-level field name",
                )
            evidence_json = _read_json(path, item_label)
            if not isinstance(evidence_json, dict) or evidence_json.get(json_field) != require_build_id:
                raise JobError(
                    "EVIDENCE_JSON_BUILD_ID_STALE",
                    item_label,
                    "evidence JSON does not identify the current build",
                    field=json_field,
                    actual=evidence_json.get(json_field) if isinstance(evidence_json, dict) else None,
                    expected=require_build_id,
                )

        results.append({"path": logical, "sha256": actual_sha})
    return sorted(results, key=lambda item: item["path"].casefold())


def _build_identity(job: dict[str, Any], base: Path) -> tuple[str, list[dict[str, str]]]:
    """Verify global input declarations and derive the immutable build ID."""
    inputs = _hash_artifacts(job.get("inputs"), base, "$.inputs", require_declared_sha=True)
    if not inputs:
        raise JobError("INPUTS_EMPTY", "$.inputs", "at least one content-addressed input is required")
    build_id = _canonical_hash({"schema_version": SCHEMA_VERSION, "inputs": inputs})
    declared = job.get("declared_build_id")
    if declared is not None:
        if not isinstance(declared, str) or not SHA256_RE.fullmatch(declared):
            raise JobError(
                "DECLARED_BUILD_ID_INVALID",
                "$.declared_build_id",
                "declared_build_id must be 64 hexadecimal characters",
            )
        if declared.lower() != build_id:
            raise JobError(
                "DECLARED_BUILD_ID_STALE",
                "$.declared_build_id",
                "declared_build_id does not match current input hashes",
                declared=declared.lower(),
                actual=build_id,
            )
    return build_id, inputs


def _gate_stages(job: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Validate gate presence/order declarations and flatten their stages."""
    gates = job.get("gates")
    if not isinstance(gates, dict):
        raise JobError("GATES_INVALID", "$.gates", "gates must be an object")
    unknown = sorted(set(gates) - set(GATE_ORDER))
    if unknown:
        raise JobError("GATES_UNKNOWN", "$.gates", "unknown gate names", names=unknown)

    flattened: list[tuple[str, dict[str, Any]]] = []
    names: set[str] = set()
    for gate in GATE_ORDER:
        gate_path = f"$.gates.{gate}"
        if gate not in gates:
            raise JobError("GATE_MISSING", gate_path, "every gate must be present or explicitly not_applicable")
        raw_gate = gates[gate]
        if not isinstance(raw_gate, dict):
            raise JobError("GATE_INVALID", gate_path, "gate must be an object")
        if "not_applicable" in raw_gate:
            reason = raw_gate.get("not_applicable")
            if gate in REQUIRED_GATES:
                raise JobError("GATE_REQUIRED", gate_path, f"{gate} cannot be marked not_applicable")
            if not isinstance(reason, str) or not reason.strip():
                raise JobError("GATE_NA_REASON_MISSING", f"{gate_path}.not_applicable", "a non-empty reason is required")
            if "stages" in raw_gate or "command" in raw_gate:
                raise JobError("GATE_NA_CONFLICT", gate_path, "not_applicable cannot be combined with commands")
            continue

        raw_stages = raw_gate.get("stages")
        if raw_stages is None and "command" in raw_gate:
            raw_stages = [raw_gate]
        if not isinstance(raw_stages, list) or not raw_stages:
            raise JobError("GATE_STAGES_EMPTY", f"{gate_path}.stages", "gate requires at least one command stage")
        for index, raw_stage in enumerate(raw_stages):
            stage_path = f"{gate_path}.stages[{index}]"
            if not isinstance(raw_stage, dict):
                raise JobError("STAGE_INVALID", stage_path, "stage must be an object")
            stage = dict(raw_stage)
            name = stage.get("name")
            if not isinstance(name, str) or not STAGE_NAME_RE.fullmatch(name):
                raise JobError(
                    "STAGE_NAME_INVALID",
                    f"{stage_path}.name",
                    "stage name must use letters, digits, dot, dash, or underscore",
                )
            key = f"{gate}/{name}"
            if key in names:
                raise JobError("STAGE_NAME_DUPLICATE", f"{stage_path}.name", f"duplicate stage: {key}")
            names.add(key)
            flattened.append((gate, stage))
    return flattened


def _command_spec(
    gate: str,
    stage: dict[str, Any],
    index: int,
    base: Path,
    build_id: str,
) -> dict[str, Any]:
    """Expand and validate one executable stage configuration."""
    label = f"$.gates.{gate}.stages[{index}]"
    replacements = {
        "python": sys.executable,
        "job_dir": str(base),
        "skill_root": str(Path(__file__).resolve().parents[1]),
        "build_id": build_id,
    }
    command = stage.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(value, str) and value for value in command):
        raise JobError("STAGE_COMMAND_INVALID", f"{label}.command", "command must be a non-empty list of strings")
    expanded_command = [
        _substitute(value, replacements, f"{label}.command[{position}]")
        for position, value in enumerate(command)
    ]

    cwd_raw = stage.get("cwd", ".")
    if not isinstance(cwd_raw, str):
        raise JobError("STAGE_CWD_INVALID", f"{label}.cwd", "cwd must be a string")
    cwd = _resolve(base, _substitute(cwd_raw, replacements, f"{label}.cwd"), f"{label}.cwd")
    if not cwd.is_dir():
        raise JobError("STAGE_CWD_NOT_FOUND", f"{label}.cwd", f"working directory does not exist: {cwd}")

    raw_env = stage.get("env", {})
    if not isinstance(raw_env, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in raw_env.items()):
        raise JobError("STAGE_ENV_INVALID", f"{label}.env", "env must map string names to string values")
    env = {
        key: _substitute(value, replacements, f"{label}.env.{key}")
        for key, value in sorted(raw_env.items())
    }

    timeout = stage.get("timeout_seconds")
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0
    ):
        raise JobError("STAGE_TIMEOUT_INVALID", f"{label}.timeout_seconds", "timeout must be a positive number")

    cache = stage.get("cache", True)
    if not isinstance(cache, bool):
        raise JobError("STAGE_CACHE_INVALID", f"{label}.cache", "cache must be boolean")
    outputs = stage.get("outputs", [])
    if not isinstance(outputs, list):
        raise JobError("ARTIFACT_LIST_INVALID", f"{label}.outputs", "outputs must be a list")
    if cache and not outputs:
        raise JobError(
            "CACHED_STAGE_OUTPUTS_EMPTY",
            f"{label}.outputs",
            "a cached stage requires at least one output; set cache=false for side-effect-only commands",
        )

    raw_depends_on = stage.get("depends_on")
    if raw_depends_on is not None:
        if not isinstance(raw_depends_on, list) or not all(
            isinstance(value, str) and value for value in raw_depends_on
        ):
            raise JobError(
                "STAGE_DEPENDENCIES_INVALID",
                f"{label}.depends_on",
                "depends_on must be a list of preceding gate/name stage keys",
            )
        if len(set(raw_depends_on)) != len(raw_depends_on):
            raise JobError(
                "STAGE_DEPENDENCY_DUPLICATE",
                f"{label}.depends_on",
                "depends_on must not contain duplicate stage keys",
            )

    return {
        "gate": gate,
        "name": stage["name"],
        "key": f"{gate}/{stage['name']}",
        "label": label,
        "command": expanded_command,
        "cwd": cwd,
        "env": env,
        "timeout": float(timeout) if timeout is not None else None,
        "cache": cache,
        "raw_inputs": stage.get("inputs", []),
        "raw_outputs": outputs,
        "raw_evidence": stage.get("evidence", []),
        "raw_depends_on": raw_depends_on,
    }


def _stage_dependencies(
    spec: dict[str, Any],
    stage_inputs: list[dict[str, str]],
    stage_evidence: list[dict[str, str]],
    prior_stages: list[dict[str, Any]],
    base: Path,
) -> list[dict[str, Any]]:
    """Return only actual upstream stages, with a conservative fallback.

    File edges are derived from the job's declared input/evidence and output
    paths. Additional non-file edges must be named by ``depends_on``. An
    omitted dependency declaration on a stage with no input/evidence signal is
    ambiguous, so it retains the historical fail-closed behavior and depends
    on every preceding stage. Workbook-wide downstream gates always consume
    the complete preceding workbook chain.
    """
    by_key = {stage["key"]: stage for stage in prior_stages}
    declared = spec["raw_depends_on"]
    selected: set[str] = set()
    if declared is not None:
        for index, dependency_key in enumerate(declared):
            if dependency_key not in by_key:
                raise JobError(
                    "STAGE_DEPENDENCY_NOT_PRECEDING",
                    f"{spec['label']}.depends_on[{index}]",
                    "dependency must identify a preceding stage by gate/name",
                    dependency=dependency_key,
                )
            selected.add(dependency_key)

    consumed_paths = {
        os.path.normcase(str(_resolve(base, artifact["path"], "stage dependency")))
        for artifact in [*stage_inputs, *stage_evidence]
    }
    for prior in prior_stages:
        produced_paths = set(prior["resolved_output_paths"])
        if consumed_paths & produced_paths:
            selected.add(prior["key"])

    if spec["gate"] in WORKBOOK_WIDE_DOWNSTREAM_GATES:
        selected.update(stage["key"] for stage in prior_stages)
    elif declared is None and not consumed_paths:
        # No declared file or explicit dependency boundary exists. Retain
        # conservative all-predecessor invalidation instead of guessing that
        # the command is independent.
        selected.update(stage["key"] for stage in prior_stages)

    return [
        {
            "key": stage["key"],
            "fingerprint": stage["fingerprint"],
            "output_hashes": stage["output_hashes"],
        }
        for stage in prior_stages
        if stage["key"] in selected
    ]


def _output_paths(values: Any, base: Path, label: str) -> list[tuple[str, Path, dict[str, Any]]]:
    """Normalize declared outputs without requiring them to exist yet."""
    results: list[tuple[str, Path, dict[str, Any]]] = []
    seen: set[str] = set()
    for index, raw in enumerate(values):
        descriptor = _artifact_descriptor(raw, f"{label}[{index}]")
        logical = _logical_path(descriptor["path"])
        if logical in seen:
            raise JobError("ARTIFACT_DUPLICATE", f"{label}[{index}]", f"duplicate output path: {logical}")
        seen.add(logical)
        results.append((logical, _resolve(base, descriptor["path"], f"{label}[{index}].path"), descriptor))
    return sorted(results, key=lambda item: item[0].casefold())


def _verify_outputs(
    outputs: list[tuple[str, Path, dict[str, Any]]],
    label: str,
) -> list[dict[str, str]]:
    """Require every stage output and reject a configured stale output hash."""
    hashed: list[dict[str, str]] = []
    for index, (logical, path, descriptor) in enumerate(outputs):
        if not path.is_file():
            raise JobError("STAGE_OUTPUT_MISSING", f"{label}[{index}]", f"stage output does not exist: {path}")
        actual = _sha256(path)
        declared = descriptor.get("sha256")
        if declared is not None:
            if not isinstance(declared, str) or not SHA256_RE.fullmatch(declared):
                raise JobError("ARTIFACT_SHA256_INVALID", f"{label}[{index}].sha256", "SHA-256 must be 64 hexadecimal characters")
            if declared.lower() != actual:
                raise JobError(
                    "ARTIFACT_SHA256_STALE",
                    f"{label}[{index}].sha256",
                    "declared output hash does not match the produced file",
                    declared=declared.lower(),
                    actual=actual,
                )
        hashed.append({"path": logical, "sha256": actual})
    return hashed


def _cache_hit(
    saved: Any,
    fingerprint: str,
    outputs: list[tuple[str, Path, dict[str, Any]]],
    label: str,
) -> tuple[bool, list[dict[str, str]]]:
    """Accept a cache entry only if all current output hashes still match."""
    if not isinstance(saved, dict) or saved.get("status") != "success" or saved.get("fingerprint") != fingerprint:
        return False, []
    try:
        current = _verify_outputs(outputs, label)
    except JobError:
        return False, []
    if current != saved.get("output_hashes"):
        return False, []
    return True, current


def _load_state(path: Path, build_id: str, resume: bool) -> dict[str, Any]:
    """Load only state belonging to the current build; otherwise start clean."""
    empty = {
        "schema_version": SCHEMA_VERSION,
        "build_id": build_id,
        "stages": {},
    }
    if not resume or not path.is_file():
        return empty
    data = _read_json(path, "state")
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION or data.get("build_id") != build_id:
        return empty
    if not isinstance(data.get("stages"), dict):
        return empty
    return data


def _capture(value: str) -> tuple[str, str, bool]:
    """Return a bounded tail, full SHA-256, and truncation flag for process text."""
    encoded = value.encode("utf-8", errors="replace")
    digest = hashlib.sha256(encoded).hexdigest()
    truncated = len(value) > MAX_CAPTURE_CHARS
    return value[-MAX_CAPTURE_CHARS:] if truncated else value, digest, truncated


def run_job(
    job_path: Path,
    *,
    state_path: Path,
    report_path: Path | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    """Execute a workbook job and return its JSON-serializable report."""
    # These caches are process-local speedups only. Clearing them for each job
    # preserves content-addressed behavior for callers that invoke run_job()
    # repeatedly after changing a script, runtime, or package lock file.
    _FILE_IDENTITY_CACHE.clear()
    _RUNTIME_VERSION_CACHE.clear()
    job_path = job_path.resolve()
    base = job_path.parent
    report: dict[str, Any] = {
        "ok": False,
        "schema_version": SCHEMA_VERSION,
        "job": str(job_path),
        "build_id": None,
        "resumed": resume,
        "stages": [],
        "errors": [],
    }

    try:
        job = _read_json(job_path, "job")
        if not isinstance(job, dict):
            raise JobError("JOB_ROOT_INVALID", "$", "job root must be an object")
        if job.get("schema_version") != SCHEMA_VERSION:
            raise JobError(
                "SCHEMA_VERSION_UNSUPPORTED",
                "$.schema_version",
                f"schema_version must be {SCHEMA_VERSION}",
            )

        build_id, global_inputs = _build_identity(job, base)
        report["build_id"] = build_id
        report["inputs"] = global_inputs

        # Job-level evidence authenticates pre-existing review/baseline material
        # and participates in every stage fingerprint.
        raw_job_evidence = job.get("evidence", [])
        job_evidence = _hash_artifacts(
            raw_job_evidence,
            base,
            "$.evidence",
            require_declared_sha=True,
            require_build_id=build_id,
        )
        report["evidence"] = job_evidence
        orchestrator_identity = _orchestrator_identity()
        report["orchestrator_identity"] = orchestrator_identity
        # Imported helpers are not visible in a command line. Hash the whole
        # skill scripts tree once per job, then reuse its compact digest for
        # every stage identity instead of rescanning it for every stage.
        skill_scripts_identity = _skill_scripts_identity()
        report["skill_scripts_identity"] = skill_scripts_identity

        flattened = _gate_stages(job)
        report["gate_order"] = list(GATE_ORDER)
        report["not_applicable_gates"] = {
            gate: job["gates"][gate]["not_applicable"]
            for gate in GATE_ORDER
            if "not_applicable" in job["gates"][gate]
        }
        state = _load_state(state_path, build_id, resume)
        state["job"] = str(job_path)
        state["inputs"] = global_inputs
        state["evidence"] = job_evidence
        state["orchestrator_identity"] = orchestrator_identity
        state["skill_scripts_identity"] = skill_scripts_identity
        prior_stages: list[dict[str, Any]] = []
        gate_counts = {gate: 0 for gate in GATE_ORDER}

        for gate, raw_stage in flattened:
            gate_index = gate_counts[gate]
            gate_counts[gate] += 1
            spec = _command_spec(gate, raw_stage, gate_index, base, build_id)
            key = spec["key"]
            label = spec["label"]
            stage_inputs = _hash_artifacts(spec["raw_inputs"], base, f"{label}.inputs")
            stage_evidence = _hash_artifacts(
                spec["raw_evidence"],
                base,
                f"{label}.evidence",
                require_declared_sha=True,
                require_build_id=build_id,
            )
            execution_identity = _execution_identity(spec, skill_scripts_identity)
            outputs = _output_paths(spec["raw_outputs"], base, f"{label}.outputs")
            dependencies = _stage_dependencies(
                spec,
                stage_inputs,
                stage_evidence,
                prior_stages,
                base,
            )
            fingerprint_material = {
                "schema_version": SCHEMA_VERSION,
                "build_id": build_id,
                "orchestrator_identity": orchestrator_identity,
                "job_evidence": job_evidence,
                "gate": gate,
                "name": spec["name"],
                "command": spec["command"],
                "execution_identity": execution_identity,
                "cwd": str(spec["cwd"]),
                "env": spec["env"],
                "inputs": stage_inputs,
                "evidence": stage_evidence,
                "declared_dependencies": spec["raw_depends_on"],
                "dependency_stages": dependencies,
                "cache": spec["cache"],
            }
            fingerprint = _canonical_hash(fingerprint_material)
            cache_hit, output_hashes = (False, [])
            if resume and spec["cache"]:
                cache_hit, output_hashes = _cache_hit(
                    state["stages"].get(key),
                    fingerprint,
                    outputs,
                    f"{label}.outputs",
                )

            stage_report: dict[str, Any] = {
                "gate": gate,
                "name": spec["name"],
                "key": key,
                "fingerprint": fingerprint,
                "command": spec["command"],
                "cwd": str(spec["cwd"]),
                "input_hashes": stage_inputs,
                "evidence_hashes": stage_evidence,
                "dependency_keys": [dependency["key"] for dependency in dependencies],
                "execution_identity": execution_identity,
                "cached": cache_hit,
            }
            if cache_hit:
                stage_report.update(
                    status="success",
                    exit_code=0,
                    elapsed_seconds=0.0,
                    output_hashes=output_hashes,
                )
                report["stages"].append(stage_report)
                prior_stages.append(
                    {
                        "key": key,
                        "gate": gate,
                        "fingerprint": fingerprint,
                        "output_hashes": output_hashes,
                        "resolved_output_paths": [
                            os.path.normcase(str(path.resolve())) for _, path, _ in outputs
                        ],
                    }
                )
                continue

            env = os.environ.copy()
            env.setdefault("PYTHONUTF8", "1")
            env.setdefault("PYTHONIOENCODING", "utf-8")
            env.update(spec["env"])
            env["MATH_WORKBOOK_BUILD_ID"] = build_id
            started = time.perf_counter()
            timed_out = False
            try:
                completed = subprocess.run(
                    spec["command"],
                    cwd=spec["cwd"],
                    env=env,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=spec["timeout"],
                )
                exit_code = completed.returncode
                stdout = completed.stdout
                stderr = completed.stderr
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                exit_code = 124
                stdout_value = exc.stdout or ""
                stderr_value = exc.stderr or ""
                stdout = stdout_value.decode("utf-8", errors="replace") if isinstance(stdout_value, bytes) else stdout_value
                stderr = stderr_value.decode("utf-8", errors="replace") if isinstance(stderr_value, bytes) else stderr_value
            except OSError as exc:
                exit_code = 127
                stdout = ""
                stderr = str(exc)
            elapsed = round(time.perf_counter() - started, 6)
            stdout_tail, stdout_sha, stdout_truncated = _capture(stdout)
            stderr_tail, stderr_sha, stderr_truncated = _capture(stderr)
            stage_report.update(
                exit_code=exit_code,
                elapsed_seconds=elapsed,
                stdout=stdout_tail,
                stdout_sha256=stdout_sha,
                stdout_truncated=stdout_truncated,
                stderr=stderr_tail,
                stderr_sha256=stderr_sha,
                stderr_truncated=stderr_truncated,
                timed_out=timed_out,
            )

            stage_error: dict[str, Any] | None = None
            if exit_code == 0:
                try:
                    output_hashes = _verify_outputs(outputs, f"{label}.outputs")
                    evidence_hashes_after = _hash_artifacts(
                        spec["raw_evidence"],
                        base,
                        f"{label}.evidence",
                        require_declared_sha=True,
                        require_build_id=build_id,
                    )
                    if evidence_hashes_after != stage_evidence:
                        raise JobError(
                            "EVIDENCE_CHANGED_DURING_STAGE",
                            f"{label}.evidence",
                            "stage mutated evidence that was authenticated before execution",
                        )
                    job_evidence_after = _hash_artifacts(
                        raw_job_evidence,
                        base,
                        "$.evidence",
                        require_declared_sha=True,
                        require_build_id=build_id,
                    )
                    if job_evidence_after != job_evidence:
                        raise JobError(
                            "JOB_EVIDENCE_CHANGED_DURING_STAGE",
                            "$.evidence",
                            "stage mutated job-level evidence that was authenticated before execution",
                        )
                    stage_report["status"] = "success"
                    stage_report["output_hashes"] = output_hashes
                except JobError as exc:
                    stage_error = exc.issue
            else:
                stage_error = _issue(
                    "STAGE_TIMEOUT" if timed_out else "STAGE_COMMAND_FAILED",
                    label,
                    "stage timed out" if timed_out else f"stage command exited with code {exit_code}",
                    key=key,
                    exit_code=exit_code,
                )

            if stage_error is not None:
                stage_report["status"] = "failed"
                stage_report["error"] = stage_error
                state["stages"][key] = dict(stage_report)
                _atomic_write_json(state_path, state)
                report["stages"].append(stage_report)
                report["errors"].append(stage_error)
                if report_path is not None:
                    _atomic_write_json(report_path, report)
                return report

            state["stages"][key] = dict(stage_report)
            _atomic_write_json(state_path, state)
            report["stages"].append(stage_report)
            prior_stages.append(
                {
                    "key": key,
                    "gate": gate,
                    "fingerprint": fingerprint,
                    "output_hashes": output_hashes,
                    "resolved_output_paths": [
                        os.path.normcase(str(path.resolve())) for _, path, _ in outputs
                    ],
                }
            )

        report["ok"] = True
        if report_path is not None:
            _atomic_write_json(report_path, report)
        return report
    except JobError as exc:
        report["errors"].append(exc.issue)
        if report_path is not None:
            _atomic_write_json(report_path, report)
        return report


def main() -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("job", type=Path, help="workbook pipeline job JSON")
    parser.add_argument(
        "--state",
        type=Path,
        help="resume state JSON (default: JOB.validate-workbook-state.json)",
    )
    parser.add_argument("--report", type=Path, help="write the complete run report to this JSON file")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume", dest="resume", action="store_true", help="reuse verified successful stages (default)")
    mode.add_argument("--fresh", dest="resume", action="store_false", help="rerun every command stage")
    parser.set_defaults(resume=True)
    args = parser.parse_args()

    job_path = args.job.resolve()
    state_path = (
        args.state.resolve()
        if args.state
        else job_path.with_name(f"{job_path.stem}.validate-workbook-state.json")
    )
    report_path = args.report.resolve() if args.report else None
    report = run_job(
        job_path,
        state_path=state_path,
        report_path=report_path,
        resume=args.resume,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
