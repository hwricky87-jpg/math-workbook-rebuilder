#!/usr/bin/env python3
"""Focused regression tests for hash-bound visual inspection evidence."""

from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from validate_visual_review import GEOMETRY_ARTIFACTS, GEOMETRY_CHECKS, validate_visual_review


class VisualReviewTests(unittest.TestCase):
    def test_current_approved_review_passes_and_pending_or_stale_fails(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mwr-visual-review-") as directory:
            root = Path(directory)
            artifacts = {}
            for name in GEOMETRY_ARTIFACTS:
                path = root / f"{name}.bin"
                path.write_bytes(f"current-{name}".encode("ascii"))
                artifacts[name] = {
                    "file": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            review = {
                "status": "approved",
                "reviewer": "vision-agent",
                "review_method": "Opened both PNGs in the image viewer at original detail.",
                "artifacts": artifacts,
                "checks": {name: True for name in GEOMETRY_CHECKS},
                "issues": [],
            }
            self.assertTrue(validate_visual_review(review, root)["ok"])

            pending = copy.deepcopy(review)
            pending["status"] = "pending-vision-review"
            self.assertFalse(validate_visual_review(pending, root)["ok"])

            stale = copy.deepcopy(review)
            stale["artifacts"]["source"]["sha256"] = "0" * 64
            self.assertFalse(validate_visual_review(stale, root)["ok"])


if __name__ == "__main__":
    unittest.main()
