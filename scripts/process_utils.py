#!/usr/bin/env python3
"""Subprocess helpers with deterministic UTF-8 decoding on every platform."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Any


def run_utf8(command: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run a text subprocess without inheriting the Windows ANSI code page."""
    if "encoding" in kwargs or "errors" in kwargs or "text" in kwargs:
        raise TypeError("run_utf8 controls encoding, errors, and text mode")
    return subprocess.run(
        command,
        encoding="utf-8",
        errors="replace",
        text=True,
        **kwargs,
    )
