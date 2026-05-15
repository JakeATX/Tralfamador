#!/usr/bin/env python3
"""Backward-compatible entry point for older local FiveThirtyEight runs."""

from tralfamador import main


if __name__ == "__main__":
    raise SystemExit(main())
