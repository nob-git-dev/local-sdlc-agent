"""Harness plugin interfaces and built-in harness implementations."""

from .base import Harness, HarnessEvidence
from .html_browser import HtmlBrowserHarness
from .python_cli import PythonCliHarness

__all__ = [
    "Harness",
    "HarnessEvidence",
    "HtmlBrowserHarness",
    "PythonCliHarness",
]
