# Math Workbook Rebuilder

A Codex skill for rebuilding Korean math worksheets from photographed workbook pages. It preserves problem text and geometry, removes handwriting, redraws diagrams as verified SVG, and requires source-to-output comparison before PDF delivery.

## What it includes

- Exact worksheet transcription and handwriting-removal rules
- SVG geometry reconstruction helpers
- Quantitative landmark checks for traced diagrams
- Source/redraw/overlay comparison reports with artifact hashes
- PDF delivery gates and regression tests

## Privacy

The repository contains only skill instructions, scripts, references, and a worksheet stylesheet. Do not commit workbook photographs, student information, generated worksheets, QA images, or PDFs. The included `.gitignore` blocks common source-image and output formats.

The skill does not require an API key and does not contain credentials.

## Requirements

- Codex desktop app, Codex CLI, or IDE extension with local skill support
- Python 3.10 or newer
- Python packages in `requirements.txt`
- Node.js 18 or newer for `scripts/render_svg.mjs`
- The `geometry-dsl` skill is optional but recommended for advanced plane geometry

## Install on Windows

```powershell
git clone https://github.com/hwricky87-jpg/math-workbook-rebuilder.git
cd math-workbook-rebuilder
.\install.ps1 -InstallDependencies
```

The default destination is `%USERPROFILE%\.codex\skills\math-workbook-rebuilder`. Restart Codex if the skill does not appear immediately.

`-InstallDependencies` installs the Python packages and the local SVG rasterizer. No API key is requested.

## Update

Run this from the cloned repository:

```powershell
.\update.ps1
```

## Verify

```powershell
python .\scripts\selftest.py --out .\qa-selftest
```

All negative gates must report `true` before using a modified release.

## License

No open-source license has been selected yet. Until a license is added, all rights are reserved even if the repository is made public.
