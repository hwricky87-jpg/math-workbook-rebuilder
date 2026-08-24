# Math Workbook Rebuilder

An Agent Skill for Codex and Claude Code that rebuilds Korean math worksheets from photographed workbook pages. It preserves problem text and geometry, removes handwriting, traces native source coordinates, and makes the agent visually inspect named source overlays, registered redraw diagnostics, landmark zooms, and final PDF crops.

## Why this exists

This skill began as a project made with care and affection: I needed a dependable way to recreate my child's scanned math problems as clean worksheets without losing the original wording or geometry. Its goal is to make those reconstructions both accurate and easy to read.

아이의 수학 문제 스캔본을 원문과 도형을 잃지 않은 깨끗한 문제지로 다시 만들 필요가 있어, 애정을 담아 시작한 스킬입니다. 정확성과 읽기 편한 결과물을 함께 지키는 것이 목표입니다.

## What it includes

- Exact worksheet transcription and handwriting-removal rules
- Whole-workbook source inventory and resumable content-addressed production stages
- Vision-first source/observation overlays and native landmark zoom sheets
- SVG geometry reconstruction helpers
- Shared-junction native-ink coordinate refinement that preserves topology
- One-to-one curve, label, dimension, right-angle, and tick-marker inventories
- Print-readable SVG defaults with configurable stroke and label sizing
- Quantitative landmark checks for traced diagrams
- Provenance checks that reject self-referential expected values
- Stable ring-degree and midpoint-cut evidence for multigraphs
- Source/redraw/overlay comparison reports with artifact hashes
- Registered red/blue/green difference diagnostics and full-source missing-line leads
- Adaptive inspection sizing, crop-local native coordinate grids, and capped overview sheets
- Segment-oriented faint-line validation, shared distance fields, and content-addressed SVG raster caching
- Current-PDF hash-bound final visual review, four-margin QA, revision-scope pixel comparison, and regression tests
- Fresh-install bootstrap and doctor checks with a skill-local pinned runtime
- Bundled OFL Korean fonts and a JSON-driven A4 PNG/PDF worksheet renderer

## Privacy

The repository contains only skill instructions, scripts, references, a worksheet stylesheet, and redistributable Korean fonts. Do not commit workbook photographs, student information, generated worksheets, QA images, or PDFs. The included `.gitignore` blocks common source-image and output formats.

The skill does not require an API key and does not contain credentials.

Public issues and pull requests must use synthetic examples. Never upload real workbook pages, student data, generated worksheets, QA images, PDFs, photograph metadata, credentials, or unredacted local paths. See [CONTRIBUTING.md](CONTRIBUTING.md) and report vulnerabilities through [private vulnerability reporting](https://github.com/hwricky87-jpg/math-workbook-rebuilder/security/advisories/new).

## Feedback and improvements

If you know a better way to inspect source images, reconstruct geometry, verify results, or improve speed, please open a [GitHub Issue](https://github.com/hwricky87-jpg/math-workbook-rebuilder/issues). Use only minimal synthetic examples that you have the right to distribute. I will review the feedback and use it to guide regression tests and future updates.

원본 판독, 도형 재작도, 검증, 속도 개선에 더 좋은 방법을 아시는 분은 GitHub Issue로 의견을 남겨 주세요. 학생 정보나 실제 교재 사진 대신 배포 권한이 있는 최소 합성 예시만 사용해 주세요. 내용을 검토해 회귀 시험과 다음 업데이트에 반영하겠습니다.

## Requirements

- Codex desktop app, Codex CLI, an IDE extension with local skill support, or Claude Code
- Python 3.10 or newer
- Python packages pinned in `requirements-lock.txt`
- Node.js 22 or newer for `scripts/render_svg.mjs`; current Node.js 24 LTS is recommended
- A vision-capable host that lets the agent actually open local source, overlay, diagnostic, landmark, and final-crop images
- The `geometry-dsl` skill is optional but recommended for advanced plane geometry

## Install directly from GitHub as a skill

Ask Codex to install `hwricky87-jpg/math-workbook-rebuilder` with its skill installer. To reproduce a particular release directly from a Codex installation, the equivalent command is:

```powershell
python "$env:USERPROFILE\.codex\skills\.system\skill-installer\scripts\install-skill-from-github.py" `
  --repo hwricky87-jpg/math-workbook-rebuilder --path . --ref v0.6.2 `
  --dest "$env:USERPROFILE\.codex\skills" --name math-workbook-rebuilder --method download
```

For Claude Code, use the same downloaded release or the Windows installer below with `-Agent Claude`; when invoking the helper explicitly, set `--dest "$env:USERPROFILE\.claude\skills"`.

The skill installer copies the public package but does not install Python or Node dependencies, so run the in-place bootstrap once after installation:

```powershell
# Codex
python "$env:USERPROFILE\.codex\skills\math-workbook-rebuilder\run.py" bootstrap.py --verify

# Claude Code
python "$env:USERPROFILE\.claude\skills\math-workbook-rebuilder\run.py" bootstrap.py --verify
```

Bootstrap creates `.venv` and `node_modules` only inside the installed skill, installs the locked runtime, checks the bundled Korean fonts, and runs the synthetic regression suite. It does not install into another project or global Python environment.

## Install on Windows

```powershell
git clone https://github.com/hwricky87-jpg/math-workbook-rebuilder.git
cd math-workbook-rebuilder

# Codex (default)
.\install.ps1 -Agent Codex -InstallDependencies

# Claude Code
.\install.ps1 -Agent Claude -InstallDependencies
```

Codex installs to `%USERPROFILE%\.codex\skills\math-workbook-rebuilder`; Claude Code installs to `%USERPROFILE%\.claude\skills\math-workbook-rebuilder`. The `SKILL.md` workflow and supporting files are shared Agent Skill content. Claude Code ignores the optional Codex UI metadata in `agents/openai.yaml`.

`-InstallDependencies` delegates to the same in-place bootstrap and verification used by a direct skill installation. It does not modify another project's Python environment and requests no API key.

## Update

Run this from the cloned repository:

```powershell
# Update the Codex installation
.\update.ps1 -Agent Codex -InstallDependencies

# Update the Claude Code installation
.\update.ps1 -Agent Claude -InstallDependencies
```

The update refuses a dirty source clone and uses a fast-forward-only Git pull. Dependencies remain isolated inside the installed skill.

## Verify

```powershell
# Codex installation
python "$env:USERPROFILE\.codex\skills\math-workbook-rebuilder\run.py" selftest.py --out .\qa-selftest

# Claude Code installation
python "$env:USERPROFILE\.claude\skills\math-workbook-rebuilder\run.py" selftest.py --out .\qa-selftest-claude

python "$env:USERPROFILE\.codex\skills\math-workbook-rebuilder\run.py" doctor.py --json
python .\scripts\validate_package.py
```

The doctor must report the skill-local locked runtime, local resvg, and deterministic bundled Korean fonts as ready. All negative gates must report `true` before using a modified release. Vision readiness is a host capability, not a package claim: the agent still has to open the current source, named observation overlay, registered diagnostic, landmark zooms, and final worksheet crop during each real job.

## Security

CI runs the package validator, Python and npm vulnerability audits, and regression self-test on Windows and Linux with read-only repository permissions. GitHub Actions are pinned to immutable commit hashes, and Dependabot monitors Python, npm, and workflow dependencies. See [SECURITY.md](SECURITY.md) for private reporting rules.

## License

Copyright 2026 hwricky87-jpg. Licensed under the [Apache License 2.0](LICENSE). Modified distributions must preserve the license and the attribution in [NOTICE](NOTICE), and modified files must identify that they were changed.
