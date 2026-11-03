"""Run both projects' regression suites, keeping pytest IDs safe on Windows."""
import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "tests"), str(ROOT / "funpayparsers")]


class ShortIds:
    def pytest_collection_modifyitems(self, items):
        # pytest writes node IDs into PYTEST_CURRENT_TEST. Oversized binary
        # parameters otherwise exceed Windows' 32767-character env limit.
        for item in items:
            if len(item.nodeid) > 500:
                prefix = item.nodeid.split("[", 1)[0]
                item._nodeid = prefix + "[sha256-" + hashlib.sha256(item.nodeid.encode()).hexdigest()[:16] + "]"


if __name__ == "__main__":
    raise SystemExit(pytest.main(sys.argv[1:] or ["tests", "funpayparsers/tests", "-q", "-m", "not network", "--tb=short"], plugins=[ShortIds()]))
