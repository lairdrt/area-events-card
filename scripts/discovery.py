"""Compatibility entry point for the installed Area Events package."""
"""Compatibility command for operators; the installed package owns the CLI."""

from area_events.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
