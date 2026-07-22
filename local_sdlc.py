#!/usr/bin/env python3
"""Compatibility entrypoint for the local SDLC runner."""

from __future__ import annotations

from local_sdlc.cli import *  # noqa: F403
from local_sdlc.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
