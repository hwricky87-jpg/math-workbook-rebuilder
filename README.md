# Math Workbook Rebuilder

A Codex skill for rebuilding Korean math worksheets from photographed workbook pages. It preserves problem text and geometry, removes handwriting, redraws diagrams as verified SVG, and requires source-to-output comparison before PDF delivery.

## Why this exists

This skill began as a project made with care and affection: I needed a dependable way to recreate my child's scanned math problems as clean worksheets without losing the original wording or geometry. Its goal is to make those reconstructions both accurate and easy to read.

아이의 수학 문제 스캔본을 원문과 도형을 잃지 않은 깨끗한 문제지로 다시 만들 필요가 있어, 애정을 담아 시작한 스킬입니다. 정확성과 읽기 편한 결과물을 함께 지키는 것이 목표입니다.

## What it includes

- Exact worksheet transcription and handwriting-removal rules
- SVG geometry reconstruction helpers
- Print-readable SVG defaults with configurable stroke and label sizing
- Quantitative landmark checks for traced diagrams
- Provenance checks that reject self-referential expected values
- Stable ring-degree and midpoint-cut evidence for multigraphs
- Source/redraw/overlay comparison reports with artifact hashes
- Adaptive inspection sizing, crop-local native coordinate grids, and capped overview sheets
- Segment-oriented faint-line validation, shared distance fields, and content-addressed SVG raster caching
- PDF delivery gates and regression tests

## Privacy

The repository contains only skill instructions, scripts, references, and a worksheet stylesheet. Do not commit workbook photographs, student information, generated worksheets, QA images, or PDFs. The included `.gitignore` blocks common source-image and output formats.

The skill does not require an API key and does not contain credentials.

## Feedback and improvements

If you know a better way to inspect source images, reconstruct geometry, verify results, or improve speed, please open a [GitHub Issue](https://github.com/hwricky87-jpg/math-workbook-rebuilder/issues). Reproducible examples and clearly described failure cases are especially helpful. I will review the feedback and use it to guide regression tests and future updates.

원본 판독, 도형 재작도, 검증, 속도 개선에 더 좋은 방법을 아시는 분은 꼭 GitHub Issue로 의견을 남겨 주세요. 재현 가능한 예시나 실패 조건을 함께 알려 주시면 특히 큰 도움이 됩니다. 내용을 검토해 회귀 시험과 다음 업데이트에 반영하겠습니다.

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

`-InstallDependencies` installs binary Python packages and the local SVG rasterizer while disabling npm dependency lifecycle scripts. No API key is requested.

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
