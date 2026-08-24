#!/usr/bin/env python3
"""Render and validate every geometry manifest in a growing golden corpus."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from geometry_svg import render_manifest
from process_utils import run_utf8
from validate_geometry import svg_fit, validate_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path, help="Directory containing case JSON manifests")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    renderer = Path(__file__).with_name("render_svg.mjs")

    cases = []
    failures = []
    for manifest_path in sorted(args.corpus.rglob("*.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if manifest.get("type") not in {"trace", "prism", "net"}:
            continue
        relative = manifest_path.relative_to(args.corpus).with_suffix("")
        stem = "__".join(relative.parts)
        svg_path = args.out / f"{stem}.svg"
        png_path = args.out / f"{stem}.png"
        try:
            svg, render_report = render_manifest(manifest)
            svg_path.write_text(svg, encoding="utf-8")
            manifest_report = validate_manifest(manifest)
            fit_report = svg_fit(svg)
            run_utf8(
                ["node", str(renderer), str(svg_path), str(png_path), "2"],
                check=True,
                capture_output=True,
            )
            ok = bool(manifest_report["ok"] and fit_report["ok"])
            case = {
                "case": str(manifest_path),
                "ok": ok,
                "manifest": manifest_report,
                "svg": fit_report,
                "render": render_report,
                "preview": str(png_path),
            }
        except Exception as exc:  # Keep the full corpus report instead of stopping at case one.
            case = {"case": str(manifest_path), "ok": False, "error": str(exc)}
            ok = False
        cases.append(case)
        if not ok:
            failures.append(str(manifest_path))

    if not cases:
        failures.append("no geometry manifests found")
    report = {"ok": not failures, "cases": cases, "failures": failures}
    report_path = args.out / "regression-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
