#!/usr/bin/env python
"""Compatibility entry point; the maintained demo lives in demos/custom."""

from pathlib import Path
import runpy
import sys

_target = Path(__file__).resolve().parents[1] / "custom" / "pulse-radar-analyzer" / "pulse-radar-analyzer.py"
sys.path.insert(0, str(_target.parent))
globals().update(runpy.run_path(str(_target), run_name=__name__))
