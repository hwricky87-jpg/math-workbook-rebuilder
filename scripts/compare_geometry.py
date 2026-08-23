#!/usr/bin/env python3
"""Create SOURCE / REDRAW / OVERLAY evidence for a geometry reconstruction."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from validate_geometry import trace_alignment


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def rasterize_svg(path: Path, scale: float = 2.0) -> Image.Image:
    renderer = Path(__file__).with_name("render_svg.mjs")
    with tempfile.TemporaryDirectory(prefix="geometry-render-") as directory:
        output = Path(directory) / "render.png"
        subprocess.run(
            ["node", str(renderer), str(path), str(output), str(scale)],
            check=True,
            capture_output=True,
            text=True,
        )
        return Image.open(output).convert("RGB").copy()


def fit_panel(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    result = Image.new("RGB", size, "white")
    fitted = ImageOps.contain(image.convert("RGB"), (size[0] - 24, size[1] - 24))
    x = (size[0] - fitted.width) // 2
    y = (size[1] - fitted.height) // 2
    result.paste(fitted, (x, y))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("redraw", type=Path)
    parser.add_argument("--box", nargs=4, type=int)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--width", type=int, default=560)
    parser.add_argument("--height", type=int, default=420)
    args = parser.parse_args()

    source = Image.open(args.source).convert("RGB")
    if args.box:
        source = source.crop(tuple(args.box))
    redraw = rasterize_svg(args.redraw) if args.redraw.suffix.lower() == ".svg" else Image.open(args.redraw).convert("RGB")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8")) if args.manifest else None
    trace = trace_alignment(manifest) if manifest else None
    size = (args.width, args.height)
    source_panel, redraw_panel = fit_panel(source, size), fit_panel(redraw, size)
    overlay = Image.blend(source_panel, redraw_panel, 0.5)

    label_height = 54 if trace else 34
    sheet = Image.new("RGB", (args.width * 3, args.height + label_height), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for index, (label, panel) in enumerate(
        (("SOURCE", source_panel), ("REDRAW", redraw_panel), ("OVERLAY", overlay))
    ):
        x = index * args.width
        sheet.paste(panel, (x, label_height))
        draw.text((x + 10, 10), label, fill="#28334a", font=font)
        draw.rectangle((x, label_height, x + args.width - 1, args.height + label_height - 1), outline="#d6dce7")

    if trace:
        maximum = trace.get("max_normalized_error", float("inf"))
        tolerance = trace.get("tolerance", 0.0)
        status = "PASS" if trace.get("ok") else "FAIL"
        draw.text(
            (args.width * 2 + 10, 30),
            f"TRACE MAX={maximum:.4f} LIMIT={tolerance:.4f} {status}",
            fill="#176b3a" if trace.get("ok") else "#b42318",
            font=font,
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(args.out)
    result = {
        "ok": bool(trace is None or trace.get("ok")),
        "source": str(args.source),
        "redraw": str(args.redraw),
        "artifact": str(args.out),
        "source_sha256": sha256(args.source),
        "redraw_sha256": sha256(args.redraw),
        "artifact_sha256": sha256(args.out),
        "source_size": list(source.size),
        "redraw_size": list(redraw.size),
        "panel_size": list(size),
        "trace": trace,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
