"""Root conftest.py — adds src/ to sys.path so that modules in src/ can
resolve their internal imports (e.g. `from utils.tag_parser import ...`)
when tests are run from the project root via pytest."""

import sys
from pathlib import Path

_src_dir = str(Path(__file__).parent / "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)
