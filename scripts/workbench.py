"""Event Analysis Workbench: python scripts/workbench.py url <address> | fixture <file>   (see docs/architecture-modules.md)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "area_events"))

from discovery.workbench import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
