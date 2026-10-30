import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for path in ("shared", "services/edge", "services/shield", "services/origin", "services/router"):
    sys.path.insert(0, str(ROOT / path))
