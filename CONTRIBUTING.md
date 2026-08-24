# Contributing

Contributions should improve deterministic worksheet reconstruction, validation, privacy, or installation without weakening the source-comparison gates.

## Protect source material

- Do not upload real workbook pages, student work, names, school information, generated worksheets, QA images, PDFs, or photograph metadata.
- Use a minimal synthetic fixture that you created and have the right to share.
- Redact local usernames, absolute paths, tokens, and machine-specific identifiers from logs.
- Report security problems through [private vulnerability reporting](https://github.com/hwricky87-jpg/math-workbook-rebuilder/security/advisories/new), not a public issue.

## Before submitting

1. Keep the change narrowly scoped and explain the failure it prevents.
2. Add or update a deterministic self-test when behavior changes.
3. Run `python scripts/validate_package.py`.
4. Run `python run.py selftest.py --out qa-selftest` with the locked dependencies installed.
5. Confirm that `git status --short` contains no source photographs or generated artifacts.

By submitting a contribution, you confirm that it contains no private student data or third-party material you lack permission to distribute. Unless explicitly stated otherwise, contributions submitted for inclusion are licensed under Apache-2.0, consistent with the project license.
