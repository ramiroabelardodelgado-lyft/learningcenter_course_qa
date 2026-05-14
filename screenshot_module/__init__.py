"""
Screenshot Module
=================
Self-contained module for LyftLearn tutorial screenshot automation.

Dependencies are installed locally to:
  - .local-packages/  (Python packages via pip --target)
  - .local-browsers/  (Playwright Chromium binary)

Main entry point:
  from screenshot_module import run_screenshots
  result = run_screenshots(params)
"""

from .screenshot_runner import run_screenshots

__all__ = ["run_screenshots"]
