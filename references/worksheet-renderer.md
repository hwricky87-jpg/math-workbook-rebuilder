# Portable worksheet renderer

Use `build_worksheet.py` after a diagram has passed the source-observation,
registered-comparison, and visual-review stages. The renderer does not infer or
repair geometry. It places an already verified SVG or raster figure into a clean
A4 worksheet and emits PNG pages plus an optional pixel-faithful PDF.

## Command

```powershell
python <skill>/run.py build_worksheet.py worksheet.json --png worksheet.png --pdf worksheet.pdf --report worksheet-render.json
```

`--font` and `--bold-font` override all font discovery. Without overrides, the
renderer prefers redistributable Korean fonts bundled under `assets/fonts`, then
uses the operating system's Korean font configuration. The report records the
resolved paths and SHA-256 values, so a later rebuild can pin the exact same
files. `--dpi` overrides `page.dpi`; the supported range is 72–600.

One page is written to the exact `--png` path. Multiple pages use numbered names
such as `worksheet-01.png` and `worksheet-02.png`. The default report name is
`worksheet-report.json`. The optional PDF embeds the reviewed PNG-equivalent
pages; it cannot silently reflow Korean text or substitute fonts.

## JSON schema version 1

Only `schema_version` and `problems` are required. Paths are resolved relative to
the JSON file.

```json
{
  "schema_version": 1,
  "document": {
    "title": "도형 복습 문제지",
    "subtitle": "그림을 보고 차근차근 풀어 보세요.",
    "student_fields": ["이름", "날짜"],
    "footer": "복습용",
    "repeat_title": false
  },
  "page": {
    "dpi": 200,
    "margin_mm": 14,
    "background": "#FFFFFF"
  },
  "style": {
    "body_font_pt": 12.5,
    "title_font_pt": 19,
    "ink": "#26364A",
    "muted": "#6F7787",
    "line": "#DCE3EE",
    "accent": "#177F78",
    "accent_soft": "#EAF6F4"
  },
  "problems": [
    {
      "number": "1",
      "heading": "도형 복습",
      "text": "정사각형의 변과 꼭짓점을 세어 보세요.",
      "figure": {
        "path": "verified-figure.svg",
        "placement": "below",
        "max_width_mm": 125,
        "max_height_mm": 75
      },
      "answer": {
        "label": "풀이",
        "lines": 4,
        "line_gap_mm": 9
      }
    }
  ]
}
```

### Document fields

- `title`, `subtitle`, and `footer` are optional strings.
- `student_fields` is an optional list of labels rendered with blank lines.
- `repeat_title` repeats the full header after the first page.

### Problem fields

- `text` is a required string or list of paragraph strings. It is reproduced
  verbatim apart from line wrapping.
- `number` accepts a string or number. `badge` is an alternative free-form badge
  such as `개념 탐구`; `heading` appears beside it.
- `figure.path` accepts SVG, PNG, JPEG, WebP, TIFF, or BMP. `placement` is
  `below` or `right`. Dimensions are maximums; aspect ratio is preserved.
- `answer.lines` is 0–20. Set it to zero to omit the solution area.
- `start_on_new_page` begins the problem on a new page.
- `card` defaults to `true`; set it to `false` for an unbordered layout.

`style.font` and `style.bold_font` may point to alternate local font files when
an exact house style is required. Omit them to use the skill's bundled Korean
fonts; command-line font overrides still take precedence.

The renderer keeps each problem together. If a problem is taller than the
printable area, it stops with a specific error instead of clipping or silently
shrinking the verified figure.

## Delivery use

Open every emitted PNG at original detail and compare the placed figure and text
with the current source. When PDF is requested, render it with `qa_pdf.py` and
inspect the page render and the final problem crop as usual. Keep the renderer
report beside the delivery evidence; it binds the JSON, figures, fonts, pages,
and PDF by hash.
