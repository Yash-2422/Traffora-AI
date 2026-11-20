"""Test configuration: make the repository root importable."""

import sys
from pathlib import Path

# tests -> traffic_mind -> agents -> backend -> <repo root>
REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))