#!/usr/bin/env python3
"""Render a clean Korean worksheet from a content-agnostic JSON specification.

The renderer deliberately owns presentation only.  Diagram geometry must already
have passed the source-observation and comparison workflow before its SVG/PNG is
placed here.  Output pages are deterministic raster canvases; the optional PDF
embeds those reviewed pages without substituting fonts or reflowing text.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import pymupdf
from PIL import Image, ImageColor, ImageDraw, ImageFont

from process_utils import run_utf8


SCHEMA_VERSION = 1
RENDERER_VERSION = 1
A4_MM = (210.0, 297.0)
HANGUL_RE = re.compile(r"[\uac00-\ud7a3]")
SUPPORTED_RASTER = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}


class WorksheetError(ValueError):
    """Raised when a worksheet cannot be rendered without guessing."""


@dataclass(frozen=True)
class FontChoice:
    regular: Path
    bold: Path
    regular_source: str
    bold_source: str


@dataclass
class ProblemLayout:
    source: dict[str, Any]
    badge: str
    heading: str
    text_lines: list[str]
    text_width: int
    text_height: int
    figure: Image.Image | None
    figure_path: Path | None
    figure_box: tuple[int, int]
    figure_placement: str
    main_height: int
    answer_label: str
    answer_lines: int
    answer_gap: int
    answer_height: int
    height: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pixel_sha256(image: Image.Image) -> str:
    rgb = image.convert("RGB")
    header = f"{rgb.width}x{rgb.height}:RGB\n".encode("ascii")
    return hashlib.sha256(header + rgb.tobytes()).hexdigest()


def _mm(value: float, dpi: int) -> int:
    return max(0, int(round(float(value) * dpi / 25.4)))


def _pt(value: float, dpi: int) -> int:
    return max(1, int(round(float(value) * dpi / 72.0)))


def _as_mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise WorksheetError(f"{name} must be an object")
    return value


def _as_text(value: Any, name: str, *, allow_empty: bool = True) -> str:
    if value is None and allow_empty:
        return ""
    if not isinstance(value, str):
        raise WorksheetError(f"{name} must be a string")
    if not allow_empty and not value.strip():
        raise WorksheetError(f"{name} cannot be empty")
    return value


def _as_number(value: Any, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorksheetError(f"{name} must be a number")
    number = float(value)
    if minimum is not None and number < minimum:
        raise WorksheetError(f"{name} must be at least {minimum}")
    return number


def _color(value: Any, name: str) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise WorksheetError(f"{name} must be a CSS color string")
    try:
        return ImageColor.getrgb(value)
    except ValueError as exc:
        raise WorksheetError(f"invalid {name}: {value!r}") from exc


def _resolve_declared_path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _glyph_signature(font: ImageFont.FreeTypeFont, text: str) -> tuple[tuple[int, int], str]:
    mask = font.getmask(text, mode="L")
    return mask.size, hashlib.sha256(bytes(mask)).hexdigest()


def _supports_hangul(path: Path) -> bool:
    try:
        font = ImageFont.truetype(str(path), 36)
        samples = [_glyph_signature(font, char) for char in "가각한글힣"]
        missing = _glyph_signature(font, "\ufffd")
    except (OSError, ValueError):
        return False
    return len(set(samples)) >= 3 and any(sample != missing for sample in samples)


def _known_font_candidates(*, bold: bool) -> Iterable[Path]:
    windows = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    if bold:
        names = (
            "malgunbd.ttf",
            "NotoSansKR-Bold.ttf",
            "NotoSansCJKkr-Bold.otf",
            "NotoSansCJK-Bold.ttc",
            "NanumGothicBold.ttf",
            "AppleSDGothicNeo.ttc",
            "arialbd.ttf",
            "DejaVuSans-Bold.ttf",
        )
    else:
        names = (
            "malgun.ttf",
            "NotoSansKR-Regular.ttf",
            "NotoSansCJKkr-Regular.otf",
            "NotoSansCJK-Regular.ttc",
            "NanumGothic.ttf",
            "AppleSDGothicNeo.ttc",
            "arial.ttf",
            "DejaVuSans.ttf",
        )
    roots = (
        windows,
        Path("/System/Library/Fonts"),
        Path("/System/Library/Fonts/Supplemental"),
        Path("/Library/Fonts"),
        Path.home() / "Library/Fonts",
        Path("/usr/share/fonts/truetype/noto"),
        Path("/usr/share/fonts/opentype/noto"),
        Path("/usr/share/fonts/truetype/nanum"),
        Path("/usr/share/fonts/truetype/dejavu"),
        Path.home() / ".local/share/fonts",
    )
    seen: set[Path] = set()
    for root in roots:
        for name in names:
            candidate = root / name
            if candidate not in seen:
                seen.add(candidate)
                yield candidate


def _bundled_font_candidates(*, bold: bool) -> Iterable[Path]:
    """Yield redistributable fonts shipped with the installed skill first."""
    font_root = Path(__file__).resolve().parent.parent / "assets" / "fonts"
    names = (
        (
            "NanumGothic-Bold.ttf",
            "NanumGothicBold.ttf",
            "NotoSansKR-Bold.ttf",
            "NotoSansCJKkr-Bold.otf",
        )
        if bold
        else (
            "NanumGothic-Regular.ttf",
            "NanumGothic.ttf",
            "NotoSansKR-Regular.ttf",
            "NotoSansCJKkr-Regular.otf",
        )
    )
    for name in names:
        yield font_root / name


def _fontconfig_match(*, bold: bool) -> Path | None:
    pattern = "sans-serif:lang=ko:style=Bold" if bold else "sans-serif:lang=ko"
    try:
        completed = subprocess.run(
            ["fc-match", "-f", "%{file}", pattern],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    candidate = Path(completed.stdout.strip()).expanduser().resolve()
    return candidate if candidate.is_file() else None


def _choose_font(
    explicit: Path | None,
    *,
    bold: bool,
    need_hangul: bool,
) -> tuple[Path, str]:
    if explicit is not None:
        candidate = explicit.expanduser().resolve()
        if not candidate.is_file():
            raise WorksheetError(f"font file does not exist: {candidate}")
        if need_hangul and not _supports_hangul(candidate):
            raise WorksheetError(f"font does not provide usable Korean glyphs: {candidate}")
        try:
            ImageFont.truetype(str(candidate), 20)
        except OSError as exc:
            raise WorksheetError(f"font cannot be opened by Pillow: {candidate}") from exc
        return candidate, "explicit"

    candidates: list[tuple[Path, str]] = []
    candidates.extend((path, "bundled") for path in _bundled_font_candidates(bold=bold))
    fc_match = _fontconfig_match(bold=bold)
    if fc_match is not None:
        candidates.append((fc_match, "fontconfig"))
    candidates.extend((path, "system-candidate") for path in _known_font_candidates(bold=bold))
    seen: set[Path] = set()
    for candidate, source in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        if need_hangul and not _supports_hangul(resolved):
            continue
        try:
            ImageFont.truetype(str(resolved), 20)
        except OSError:
            continue
        return resolved, source
    language = "Korean " if need_hangul else ""
    raise WorksheetError(
        f"no usable {language}font was found; install Noto Sans CJK KR/Nanum Gothic "
        "or pass --font with an absolute font path"
    )


def resolve_fonts(
    spec: dict[str, Any],
    spec_dir: Path,
    *,
    font_override: Path | None,
    bold_override: Path | None,
    need_hangul: bool,
) -> FontChoice:
    style = _as_mapping(spec.get("style"), "style")
    declared_regular = style.get("font")
    declared_bold = style.get("bold_font")
    regular_explicit = font_override
    if regular_explicit is None and declared_regular is not None:
        regular_explicit = _resolve_declared_path(
            _as_text(declared_regular, "style.font", allow_empty=False), spec_dir
        )
    bold_explicit = bold_override
    if bold_explicit is None and declared_bold is not None:
        bold_explicit = _resolve_declared_path(
            _as_text(declared_bold, "style.bold_font", allow_empty=False), spec_dir
        )

    regular, regular_source = _choose_font(
        regular_explicit, bold=False, need_hangul=need_hangul
    )
    if bold_explicit is not None:
        bold, bold_source = _choose_font(
            bold_explicit, bold=True, need_hangul=need_hangul
        )
    else:
        try:
            bold, bold_source = _choose_font(None, bold=True, need_hangul=need_hangul)
        except WorksheetError:
            bold, bold_source = regular, "regular-fallback"
    return FontChoice(regular, bold, regular_source, bold_source)


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> float:
    return float(draw.textlength(text, font=font))


def _split_long_token(
    draw: ImageDraw.ImageDraw,
    token: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    pieces: list[str] = []
    current = ""
    for char in token:
        candidate = current + char
        if current and _text_width(draw, candidate, font) > max_width:
            pieces.append(current)
            current = char
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces or [""]


def _wrap_paragraph(
    draw: ImageDraw.ImageDraw,
    paragraph: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    if not paragraph:
        return [""]
    tokens = re.findall(r"\S+", paragraph)
    if not tokens:
        return [""]
    lines: list[str] = []
    current = ""
    for token in tokens:
        parts = (
            [token]
            if _text_width(draw, token, font) <= max_width
            else _split_long_token(draw, token, font, max_width)
        )
        for index, part in enumerate(parts):
            separator = " " if current and index == 0 else ""
            candidate = current + separator + part
            if current and _text_width(draw, candidate, font) > max_width:
                lines.append(current)
                current = part
            else:
                current = candidate
            if index < len(parts) - 1:
                lines.append(current)
                current = ""
    if current:
        lines.append(current)
    return lines or [""]


def _problem_text(problem: dict[str, Any], index: int) -> str:
    raw = problem.get("text")
    if isinstance(raw, str):
        text = raw
    elif isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        text = "\n".join(raw)
    else:
        raise WorksheetError(f"problems[{index}].text must be a string or string array")
    if not text.strip():
        raise WorksheetError(f"problems[{index}].text cannot be empty")
    return text


def _fit_size(width: int, height: int, max_width: int, max_height: int) -> tuple[int, int]:
    if width <= 0 or height <= 0:
        raise WorksheetError("figure has invalid dimensions")
    scale = min(max_width / width, max_height / height)
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def _svg_length(value: str | None) -> float | None:
    if value is None:
        return None
    match = re.fullmatch(
        r"\s*([0-9]+(?:\.[0-9]+)?|\.[0-9]+)\s*(px|pt|pc|mm|cm|in)?\s*",
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "px").lower()
    factors = {
        "px": 1.0,
        "pt": 96.0 / 72.0,
        "pc": 16.0,
        "mm": 96.0 / 25.4,
        "cm": 96.0 / 2.54,
        "in": 96.0,
    }
    return number * factors[unit]


def _svg_intrinsic_size(data: bytes, path: Path) -> tuple[float, float]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise WorksheetError(f"cannot parse SVG figure {path}: {exc}") from exc
    width = _svg_length(root.attrib.get("width"))
    height = _svg_length(root.attrib.get("height"))
    view_box = root.attrib.get("viewBox") or root.attrib.get("viewbox")
    view_size: tuple[float, float] | None = None
    if view_box:
        try:
            values = [float(value) for value in re.split(r"[\s,]+", view_box.strip()) if value]
        except ValueError:
            values = []
        if len(values) == 4 and values[2] > 0 and values[3] > 0:
            view_size = (values[2], values[3])
    if width is None and view_size is not None:
        width = view_size[0]
    if height is None and view_size is not None:
        height = view_size[1]
    if width is None or height is None or width <= 0 or height <= 0:
        raise WorksheetError(
            f"SVG figure needs positive width/height or a viewBox for deterministic placement: {path}"
        )
    return width, height


def _render_svg(path: Path, target: tuple[int, int]) -> Image.Image:
    data = path.read_bytes()
    width, height = _svg_intrinsic_size(data, path)
    scale = min(target[0] / width, target[1] / height)
    node = shutil.which("node")
    rasterizer = Path(__file__).resolve().parent / "render_svg.mjs"
    if node is None or not rasterizer.is_file():
        raise WorksheetError(
            "SVG placement requires the bundled resvg renderer; run "
            "python run.py bootstrap.py --verify"
        )
    with tempfile.TemporaryDirectory(prefix="worksheet-svg-") as temp:
        rendered = Path(temp) / "figure.png"
        completed = run_utf8(
            [node, str(rasterizer), str(path), str(rendered), f"{scale:.10g}"],
            check=False,
            capture_output=True,
        )
        if completed.returncode != 0 or not rendered.is_file():
            detail = (completed.stderr or completed.stdout).strip()
            raise WorksheetError(
                f"cannot render SVG figure {path} with resvg"
                + (f": {detail}" if detail else "")
            )
        try:
            with Image.open(rendered) as source:
                source.load()
                rgba = source.convert("RGBA")
        except OSError as exc:
            raise WorksheetError(f"resvg produced an unreadable figure for {path}") from exc
    if rgba.width > target[0] or rgba.height > target[1]:
        fitted = _fit_size(rgba.width, rgba.height, *target)
        rgba = rgba.resize(fitted, Image.Resampling.LANCZOS)
    return rgba


def _load_figure(path: Path, target: tuple[int, int]) -> Image.Image:
    if not path.is_file():
        raise WorksheetError(f"figure file does not exist: {path}")
    suffix = path.suffix.lower()
    if suffix == ".svg":
        return _render_svg(path, target)
    if suffix not in SUPPORTED_RASTER:
        raise WorksheetError(f"unsupported figure format {suffix!r}: {path}")
    try:
        with Image.open(path) as source:
            source.load()
            rgba = source.convert("RGBA")
    except (OSError, ValueError) as exc:
        raise WorksheetError(f"cannot open figure {path}: {exc}") from exc
    fitted = _fit_size(rgba.width, rgba.height, *target)
    if fitted != rgba.size:
        rgba = rgba.resize(fitted, Image.Resampling.LANCZOS)
    return rgba


def _paste_rgba(page: Image.Image, figure: Image.Image, x: int, y: int) -> None:
    if figure.mode != "RGBA":
        figure = figure.convert("RGBA")
    page.paste(figure, (x, y), figure)


class WorksheetRenderer:
    def __init__(
        self,
        spec: dict[str, Any],
        spec_path: Path,
        fonts: FontChoice,
        *,
        dpi_override: int | None = None,
    ) -> None:
        self.spec = spec
        self.spec_path = spec_path
        self.spec_dir = spec_path.parent
        page = _as_mapping(spec.get("page"), "page")
        declared_dpi = dpi_override if dpi_override is not None else page.get("dpi", 200)
        dpi = int(_as_number(declared_dpi, "page.dpi", minimum=72))
        if dpi > 600:
            raise WorksheetError("page.dpi cannot exceed 600")
        self.dpi = dpi
        self.width = _mm(A4_MM[0], dpi)
        self.height = _mm(A4_MM[1], dpi)
        margin = _as_number(page.get("margin_mm", 14), "page.margin_mm", minimum=5)
        if margin > 35:
            raise WorksheetError("page.margin_mm cannot exceed 35")
        self.margin = _mm(margin, dpi)
        self.background = _color(page.get("background", "#FFFFFF"), "page.background")
        self.document = _as_mapping(spec.get("document"), "document")
        self.style = _as_mapping(spec.get("style"), "style")
        self.fonts = fonts

        self.ink = _color(self.style.get("ink", "#26364A"), "style.ink")
        self.muted = _color(self.style.get("muted", "#6F7787"), "style.muted")
        self.line = _color(self.style.get("line", "#DCE3EE"), "style.line")
        self.accent = _color(self.style.get("accent", "#177F78"), "style.accent")
        self.accent_soft = _color(
            self.style.get("accent_soft", "#EAF6F4"), "style.accent_soft"
        )
        self.card_background = _color(
            self.style.get("card_background", "#FFFFFF"), "style.card_background"
        )

        body_pt = _as_number(self.style.get("body_font_pt", 12.5), "style.body_font_pt", minimum=7)
        badge_pt = _as_number(self.style.get("badge_font_pt", 10), "style.badge_font_pt", minimum=7)
        title_pt = _as_number(self.style.get("title_font_pt", 19), "style.title_font_pt", minimum=10)
        subtitle_pt = _as_number(
            self.style.get("subtitle_font_pt", 10.5), "style.subtitle_font_pt", minimum=7
        )
        self.body_font = ImageFont.truetype(str(fonts.regular), _pt(body_pt, dpi))
        self.body_bold = ImageFont.truetype(str(fonts.bold), _pt(body_pt, dpi))
        self.badge_font = ImageFont.truetype(str(fonts.bold), _pt(badge_pt, dpi))
        self.title_font = ImageFont.truetype(str(fonts.bold), _pt(title_pt, dpi))
        self.subtitle_font = ImageFont.truetype(str(fonts.regular), _pt(subtitle_pt, dpi))
        self.body_line_height = _pt(body_pt * 1.62, dpi)
        self.heading_line_height = _pt(body_pt * 1.35, dpi)
        self.subtitle_line_height = _pt(subtitle_pt * 1.45, dpi)
        self.card_radius = _mm(
            _as_number(self.style.get("card_radius_mm", 4), "style.card_radius_mm", minimum=0),
            dpi,
        )
        self.card_padding = _mm(
            _as_number(self.style.get("card_padding_mm", 6), "style.card_padding_mm", minimum=2),
            dpi,
        )
        self.problem_gap = _mm(
            _as_number(self.style.get("problem_gap_mm", 6), "style.problem_gap_mm", minimum=0),
            dpi,
        )
        self.footer_height = _mm(8, dpi)
        self._measure_canvas = Image.new("RGB", (16, 16), "white")
        self._measure = ImageDraw.Draw(self._measure_canvas)

    def _header_height(self, page_index: int) -> int:
        title = _as_text(self.document.get("title"), "document.title")
        subtitle = _as_text(self.document.get("subtitle"), "document.subtitle")
        fields = self.document.get("student_fields", [])
        if not isinstance(fields, list) or not all(isinstance(value, str) for value in fields):
            raise WorksheetError("document.student_fields must be a string array")
        repeat = bool(self.document.get("repeat_title", False))
        if page_index > 0 and not repeat:
            return 0
        if not title and not subtitle and not fields:
            return 0
        height = 0
        if title:
            height += _pt(25, self.dpi)
        if subtitle:
            height += self.subtitle_line_height
        if fields:
            height += _mm(10, self.dpi)
        return height + _mm(5, self.dpi)

    def _wrap_text(self, text: str, max_width: int) -> list[str]:
        lines: list[str] = []
        for paragraph in text.splitlines():
            lines.extend(_wrap_paragraph(self._measure, paragraph, self.body_font, max_width))
        return lines or [""]

    def _layout_problem(self, problem: dict[str, Any], index: int, card_width: int) -> ProblemLayout:
        text = _problem_text(problem, index)
        badge = _as_text(problem.get("badge"), f"problems[{index}].badge")
        if not badge:
            number = problem.get("number")
            if number is not None:
                if isinstance(number, (int, float)) and not isinstance(number, bool):
                    badge = str(number)
                elif isinstance(number, str):
                    badge = number
                else:
                    raise WorksheetError(f"problems[{index}].number must be text or number")
        heading = _as_text(problem.get("heading"), f"problems[{index}].heading")
        content_width = card_width - 2 * self.card_padding
        if content_width <= 0:
            raise WorksheetError("card content width is not positive")

        figure_spec = _as_mapping(problem.get("figure"), f"problems[{index}].figure")
        figure: Image.Image | None = None
        figure_path: Path | None = None
        placement = _as_text(
            figure_spec.get("placement", "below"),
            f"problems[{index}].figure.placement",
            allow_empty=False,
        )
        if placement not in {"below", "right"}:
            raise WorksheetError(
                f"problems[{index}].figure.placement must be 'below' or 'right'"
            )
        figure_box = (0, 0)
        if figure_spec:
            declared_path = _as_text(
                figure_spec.get("path"), f"problems[{index}].figure.path", allow_empty=False
            )
            figure_path = _resolve_declared_path(declared_path, self.spec_dir)
            default_width = 64 if placement == "right" else 138
            default_height = 62 if placement == "right" else 82
            max_width = min(
                content_width,
                _mm(
                    _as_number(
                        figure_spec.get("max_width_mm", default_width),
                        f"problems[{index}].figure.max_width_mm",
                        minimum=10,
                    ),
                    self.dpi,
                ),
            )
            max_height = _mm(
                _as_number(
                    figure_spec.get("max_height_mm", default_height),
                    f"problems[{index}].figure.max_height_mm",
                    minimum=10,
                ),
                self.dpi,
            )
            figure = _load_figure(figure_path, (max_width, max_height))
            figure_box = figure.size

        side_gap = _mm(7, self.dpi)
        if figure is not None and placement == "right":
            text_width = content_width - figure.width - side_gap
            if text_width < _mm(55, self.dpi):
                raise WorksheetError(
                    f"problems[{index}] right-side figure leaves too little text width; "
                    "reduce figure.max_width_mm or use placement='below'"
                )
        else:
            text_width = content_width
        text_lines = self._wrap_text(text, text_width)
        text_height = len(text_lines) * self.body_line_height
        if figure is None:
            main_height = text_height
        elif placement == "right":
            main_height = max(text_height, figure.height)
        else:
            main_height = text_height + _mm(4, self.dpi) + figure.height

        answer_spec = _as_mapping(problem.get("answer"), f"problems[{index}].answer")
        answer_lines_raw = answer_spec.get("lines", 0)
        if isinstance(answer_lines_raw, bool) or not isinstance(answer_lines_raw, int):
            raise WorksheetError(f"problems[{index}].answer.lines must be an integer")
        if not 0 <= answer_lines_raw <= 20:
            raise WorksheetError(f"problems[{index}].answer.lines must be in 0..20")
        answer_label = _as_text(
            answer_spec.get("label", "풀이"), f"problems[{index}].answer.label"
        )
        answer_gap = _mm(
            _as_number(
                answer_spec.get("line_gap_mm", 9),
                f"problems[{index}].answer.line_gap_mm",
                minimum=4,
            ),
            self.dpi,
        )
        answer_height = 0
        if answer_lines_raw:
            answer_height = _mm(5, self.dpi) + self.heading_line_height + answer_lines_raw * answer_gap

        badge_height = _mm(8, self.dpi) if badge or heading else 0
        after_badge = _mm(3, self.dpi) if badge_height else 0
        height = (
            self.card_padding
            + badge_height
            + after_badge
            + main_height
            + answer_height
            + self.card_padding
        )
        return ProblemLayout(
            source=problem,
            badge=badge,
            heading=heading,
            text_lines=text_lines,
            text_width=text_width,
            text_height=text_height,
            figure=figure,
            figure_path=figure_path,
            figure_box=figure_box,
            figure_placement=placement,
            main_height=main_height,
            answer_label=answer_label,
            answer_lines=answer_lines_raw,
            answer_gap=answer_gap,
            answer_height=answer_height,
            height=height,
        )

    def _draw_header(self, page: Image.Image, page_index: int) -> int:
        draw = ImageDraw.Draw(page)
        title = _as_text(self.document.get("title"), "document.title")
        subtitle = _as_text(self.document.get("subtitle"), "document.subtitle")
        repeat = bool(self.document.get("repeat_title", False))
        if page_index > 0 and not repeat:
            return self.margin
        y = self.margin
        if title:
            draw.text((self.margin, y), title, font=self.title_font, fill=self.ink)
            y += _pt(25, self.dpi)
        if subtitle:
            draw.text((self.margin, y), subtitle, font=self.subtitle_font, fill=self.muted)
            y += self.subtitle_line_height
        fields = self.document.get("student_fields", [])
        if fields:
            x = self.margin
            baseline = y + _mm(6, self.dpi)
            available = self.width - 2 * self.margin
            field_width = available // len(fields)
            for field in fields:
                label_width = int(round(_text_width(draw, field, self.subtitle_font)))
                draw.text((x, y), field, font=self.subtitle_font, fill=self.muted)
                line_x0 = x + label_width + _mm(2, self.dpi)
                line_x1 = x + field_width - _mm(5, self.dpi)
                if line_x1 > line_x0:
                    draw.line((line_x0, baseline, line_x1, baseline), fill=self.line, width=2)
                x += field_width
            y += _mm(10, self.dpi)
        if title or subtitle or fields:
            y += _mm(5, self.dpi)
        return y

    def _draw_badge(self, draw: ImageDraw.ImageDraw, layout: ProblemLayout, x: int, y: int) -> None:
        if not layout.badge and not layout.heading:
            return
        badge_height = _mm(8, self.dpi)
        cursor = x
        if layout.badge:
            label_width = int(round(_text_width(draw, layout.badge, self.badge_font)))
            badge_width = max(_mm(10, self.dpi), label_width + _mm(6, self.dpi))
            draw.rounded_rectangle(
                (cursor, y, cursor + badge_width, y + badge_height),
                radius=badge_height // 2,
                fill=self.accent,
            )
            bbox = draw.textbbox((0, 0), layout.badge, font=self.badge_font)
            text_y = y + (badge_height - (bbox[3] - bbox[1])) // 2 - bbox[1]
            draw.text(
                (cursor + (badge_width - label_width) // 2, text_y),
                layout.badge,
                font=self.badge_font,
                fill=(255, 255, 255),
            )
            cursor += badge_width + _mm(3, self.dpi)
        if layout.heading:
            bbox = draw.textbbox((0, 0), layout.heading, font=self.body_bold)
            text_y = y + (badge_height - (bbox[3] - bbox[1])) // 2 - bbox[1]
            draw.text((cursor, text_y), layout.heading, font=self.body_bold, fill=self.ink)

    def _draw_problem(self, page: Image.Image, layout: ProblemLayout, x: int, y: int, width: int) -> None:
        draw = ImageDraw.Draw(page)
        card_enabled = bool(layout.source.get("card", True))
        if card_enabled:
            draw.rounded_rectangle(
                (x, y, x + width, y + layout.height),
                radius=self.card_radius,
                fill=self.card_background,
                outline=self.line,
                width=max(1, _pt(0.55, self.dpi)),
            )
        inner_x = x + self.card_padding
        cursor_y = y + self.card_padding
        if layout.badge or layout.heading:
            self._draw_badge(draw, layout, inner_x, cursor_y)
            cursor_y += _mm(11, self.dpi)

        for line in layout.text_lines:
            if line:
                draw.text((inner_x, cursor_y), line, font=self.body_font, fill=self.ink)
            cursor_y += self.body_line_height

        if layout.figure is not None:
            content_width = width - 2 * self.card_padding
            if layout.figure_placement == "right":
                figure_x = inner_x + content_width - layout.figure.width
                figure_y = cursor_y - layout.text_height
            else:
                cursor_y += _mm(4, self.dpi)
                figure_x = inner_x + (content_width - layout.figure.width) // 2
                figure_y = cursor_y
                cursor_y += layout.figure.height
            _paste_rgba(page, layout.figure, figure_x, figure_y)

        if layout.answer_lines:
            main_start = y + self.card_padding
            if layout.badge or layout.heading:
                main_start += _mm(11, self.dpi)
            answer_y = main_start + layout.main_height + _mm(5, self.dpi)
            draw.text((inner_x, answer_y), layout.answer_label, font=self.body_bold, fill=self.muted)
            line_y = answer_y + self.heading_line_height
            right = x + width - self.card_padding
            for _ in range(layout.answer_lines):
                line_y += layout.answer_gap
                draw.line((inner_x, line_y, right, line_y), fill=self.line, width=max(1, _pt(0.45, self.dpi)))

    def render(self) -> tuple[list[Image.Image], list[list[ProblemLayout]]]:
        raw_problems = self.spec.get("problems")
        if not isinstance(raw_problems, list) or not raw_problems:
            raise WorksheetError("problems must be a non-empty array")
        if not all(isinstance(problem, dict) for problem in raw_problems):
            raise WorksheetError("every problems entry must be an object")
        card_width = self.width - 2 * self.margin
        layouts = [
            self._layout_problem(problem, index, card_width)
            for index, problem in enumerate(raw_problems)
        ]

        pages_layout: list[list[ProblemLayout]] = [[]]
        page_index = 0
        y = self.margin + self._header_height(page_index)
        bottom = self.height - self.margin - self.footer_height
        for index, layout in enumerate(layouts):
            force_break = bool(layout.source.get("start_on_new_page", False))
            needed = layout.height + (self.problem_gap if pages_layout[page_index] else 0)
            if force_break and pages_layout[page_index]:
                pages_layout.append([])
                page_index += 1
                y = self.margin + self._header_height(page_index)
            elif y + needed > bottom and pages_layout[page_index]:
                pages_layout.append([])
                page_index += 1
                y = self.margin + self._header_height(page_index)
                needed = layout.height
            if y + needed > bottom:
                raise WorksheetError(
                    f"problems[{index}] is taller than one printable page; reduce its figure or answer lines"
                )
            if pages_layout[page_index]:
                y += self.problem_gap
            pages_layout[page_index].append(layout)
            y += layout.height

        pages: list[Image.Image] = []
        total = len(pages_layout)
        footer = _as_text(self.document.get("footer"), "document.footer")
        for current, page_layout in enumerate(pages_layout):
            page = Image.new("RGB", (self.width, self.height), self.background)
            draw = ImageDraw.Draw(page)
            y = self._draw_header(page, current)
            for offset, layout in enumerate(page_layout):
                if offset:
                    y += self.problem_gap
                self._draw_problem(page, layout, self.margin, y, card_width)
                y += layout.height
            page_label = f"{current + 1} / {total}"
            if footer:
                draw.text(
                    (self.margin, self.height - self.margin - self.subtitle_line_height),
                    footer,
                    font=self.subtitle_font,
                    fill=self.muted,
                )
            label_width = int(round(_text_width(draw, page_label, self.subtitle_font)))
            draw.text(
                (
                    self.width - self.margin - label_width,
                    self.height - self.margin - self.subtitle_line_height,
                ),
                page_label,
                font=self.subtitle_font,
                fill=self.muted,
            )
            pages.append(page)
        return pages, pages_layout


def _output_paths(base: Path, count: int) -> list[Path]:
    if base.suffix.lower() != ".png":
        raise WorksheetError("--png must end in .png")
    if count == 1:
        return [base]
    return [base.with_name(f"{base.stem}-{index:02d}.png") for index in range(1, count + 1)]


def _write_pdf(pages: Sequence[Image.Image], output: Path, dpi: int) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    width_pt = A4_MM[0] * 72.0 / 25.4
    height_pt = A4_MM[1] * 72.0 / 25.4
    document = pymupdf.open()
    try:
        for image in pages:
            page = document.new_page(width=width_pt, height=height_pt)
            payload = io.BytesIO()
            image.convert("RGB").save(payload, format="PNG", optimize=True)
            page.insert_image(page.rect, stream=payload.getvalue(), keep_proportion=False)
        document.set_metadata(
            {
                "title": "Worksheet",
                "creator": f"math-workbook-rebuilder worksheet renderer v{RENDERER_VERSION}",
                "producer": "PyMuPDF",
            }
        )
        document.save(output, deflate=True, garbage=3, no_new_id=True)
    finally:
        document.close()


def _collect_text(spec: dict[str, Any]) -> str:
    parts: list[str] = []
    document = _as_mapping(spec.get("document"), "document")
    for key in ("title", "subtitle", "footer"):
        value = document.get(key)
        if isinstance(value, str):
            parts.append(value)
    fields = document.get("student_fields", [])
    if isinstance(fields, list):
        parts.extend(value for value in fields if isinstance(value, str))
    problems = spec.get("problems", [])
    if isinstance(problems, list):
        for problem in problems:
            if not isinstance(problem, dict):
                continue
            for key in ("badge", "heading"):
                value = problem.get(key)
                if isinstance(value, str):
                    parts.append(value)
            raw_text = problem.get("text")
            if isinstance(raw_text, str):
                parts.append(raw_text)
            elif isinstance(raw_text, list):
                parts.extend(value for value in raw_text if isinstance(value, str))
            answer = problem.get("answer")
            if isinstance(answer, dict) and isinstance(answer.get("label"), str):
                parts.append(answer["label"])
    return "\n".join(parts)


def build_worksheet(
    spec_path: Path,
    png_path: Path,
    *,
    pdf_path: Path | None = None,
    report_path: Path | None = None,
    font_override: Path | None = None,
    bold_override: Path | None = None,
    dpi_override: int | None = None,
) -> dict[str, Any]:
    spec_path = spec_path.expanduser().resolve()
    png_path = png_path.expanduser().resolve()
    if not spec_path.is_file():
        raise WorksheetError(f"spec file does not exist: {spec_path}")
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorksheetError(f"cannot read JSON spec {spec_path}: {exc}") from exc
    if not isinstance(spec, dict):
        raise WorksheetError("worksheet spec root must be an object")
    if spec.get("schema_version") != SCHEMA_VERSION:
        raise WorksheetError(f"schema_version must be {SCHEMA_VERSION}")

    need_hangul = bool(HANGUL_RE.search(_collect_text(spec)))
    fonts = resolve_fonts(
        spec,
        spec_path.parent,
        font_override=font_override,
        bold_override=bold_override,
        need_hangul=need_hangul,
    )
    renderer = WorksheetRenderer(spec, spec_path, fonts, dpi_override=dpi_override)
    pages, page_layouts = renderer.render()
    outputs = _output_paths(png_path, len(pages))

    resolved_pdf: Path | None = None
    if pdf_path is not None:
        resolved_pdf = pdf_path.expanduser().resolve()
        if resolved_pdf.suffix.lower() != ".pdf":
            raise WorksheetError("--pdf must end in .pdf")
    target_report = (
        report_path.expanduser().resolve()
        if report_path is not None
        else png_path.with_name(f"{png_path.stem}-report.json")
    )

    input_paths: list[tuple[str, Path]] = [
        ("worksheet spec", spec_path),
        ("regular font", fonts.regular.resolve()),
        ("bold font", fonts.bold.resolve()),
    ]
    for layouts in page_layouts:
        for layout in layouts:
            if layout.figure_path is not None:
                input_paths.append(("figure", layout.figure_path.resolve()))
    output_paths: list[tuple[str, Path]] = [
        *(('worksheet page', output.resolve()) for output in outputs),
        ("worksheet report", target_report),
    ]
    if resolved_pdf is not None:
        output_paths.append(("worksheet PDF", resolved_pdf))

    input_by_key = {
        os.path.normcase(str(path)): (role, path)
        for role, path in input_paths
    }
    seen_outputs: dict[str, tuple[str, Path]] = {}
    for role, path in output_paths:
        key = os.path.normcase(str(path))
        if key in input_by_key:
            input_role, input_path = input_by_key[key]
            raise WorksheetError(
                f"output path for {role} would overwrite {input_role}: {input_path}"
            )
        if key in seen_outputs:
            previous_role, previous_path = seen_outputs[key]
            raise WorksheetError(
                f"output paths collide: {previous_role} and {role}: {previous_path}"
            )
        seen_outputs[key] = (role, path)

    output_records: list[dict[str, Any]] = []
    for index, (page, output, layouts) in enumerate(zip(pages, outputs, page_layouts), start=1):
        output.parent.mkdir(parents=True, exist_ok=True)
        page.save(output, format="PNG", optimize=True, dpi=(renderer.dpi, renderer.dpi))
        output_records.append(
            {
                "page": index,
                "path": str(output),
                "sha256": _sha256(output),
                "pixel_sha256": _pixel_sha256(page),
                "size_px": [page.width, page.height],
                "problems": [
                    str(layout.source.get("number", layout.source.get("badge", offset + 1)))
                    for offset, layout in enumerate(layouts)
                ],
            }
        )

    pdf_record: dict[str, Any] | None = None
    if resolved_pdf is not None:
        _write_pdf(pages, resolved_pdf, renderer.dpi)
        pdf_record = {
            "path": str(resolved_pdf),
            "sha256": _sha256(resolved_pdf),
            "page_count": len(pages),
            "mode": "reviewed-page-raster-embed",
        }

    figures: list[dict[str, Any]] = []
    seen_figures: set[Path] = set()
    for layouts in page_layouts:
        for layout in layouts:
            if layout.figure_path is None or layout.figure_path in seen_figures:
                continue
            seen_figures.add(layout.figure_path)
            figures.append(
                {
                    "path": str(layout.figure_path),
                    "sha256": _sha256(layout.figure_path),
                }
            )
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "renderer_version": RENDERER_VERSION,
        "ok": True,
        "spec": str(spec_path),
        "spec_sha256": _sha256(spec_path),
        "dpi": renderer.dpi,
        "page_size_mm": list(A4_MM),
        "page_size_px": [renderer.width, renderer.height],
        "page_count": len(pages),
        "font": {
            "regular": str(fonts.regular),
            "regular_sha256": _sha256(fonts.regular),
            "regular_source": fonts.regular_source,
            "bold": str(fonts.bold),
            "bold_sha256": _sha256(fonts.bold),
            "bold_source": fonts.bold_source,
            "hangul_required": need_hangul,
        },
        "figures": figures,
        "pages": output_records,
        "pdf": pdf_record,
    }
    target_report.parent.mkdir(parents=True, exist_ok=True)
    report["report"] = str(target_report)
    target_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path, help="UTF-8 worksheet JSON specification")
    parser.add_argument("--png", type=Path, required=True, help="output PNG path or page stem")
    parser.add_argument("--pdf", type=Path, help="optional raster-faithful PDF output")
    parser.add_argument("--report", type=Path, help="output content-addressed JSON report")
    parser.add_argument("--font", type=Path, help="regular Korean font path (overrides JSON)")
    parser.add_argument("--bold-font", type=Path, help="bold Korean font path (overrides JSON)")
    parser.add_argument("--dpi", type=int, help="override page DPI (72..600)")
    args = parser.parse_args()

    try:
        report = build_worksheet(
            args.spec,
            args.png,
            pdf_path=args.pdf,
            report_path=args.report,
            font_override=args.font,
            bold_override=args.bold_font,
            dpi_override=args.dpi,
        )
    except (WorksheetError, OSError) as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)]}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
