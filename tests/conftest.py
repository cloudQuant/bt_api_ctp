from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PACKAGE_ROOT / "src"
# A standalone clone can live directly below a drive root. Only add the parent
# checkout for the actual bt_api/<plugin> monorepo layout, never an arbitrary
# ancestor such as D:/ or /tmp. The plugin's own source stays first.
if PACKAGE_ROOT.parent.name == "bt_api":
    parent_root = PACKAGE_ROOT.parent.parent
    if (parent_root / "bt_api_py" / "__init__.py").is_file():
        parent_text = str(parent_root)
        if parent_text not in sys.path:
            sys.path.append(parent_text)

source_text = str(SRC_ROOT)
if source_text in sys.path:
    sys.path.remove(source_text)
sys.path.insert(0, source_text)
