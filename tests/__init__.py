import os
import sys

# The engine package lives inside the Home Assistant app folder (area_events/discovery).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "area_events"))
