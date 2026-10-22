"""Discovery Service command line: python scripts/discovery.py run | status | inspect   (see area_events/discovery/service/cli.py)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "area_events"))

from discovery.service.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
