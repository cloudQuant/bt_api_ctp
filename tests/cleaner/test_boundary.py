"""架构边界测试：cleaner 框架模块不得依赖 CTP 具体实现（对齐迭代04 约定）。"""

from __future__ import annotations

import ast
from pathlib import Path

import bt_api_ctp.cleaner

#: cli.py 是唯一装配点（允许函数内延迟导入具体实现），其余模块必须与交易所无关。
_ASSEMBLY_POINT = "cli.py"
_FORBIDDEN_PREFIXES = (
    "bt_api_ctp.ctp",
    "bt_api_ctp.collector_ctp",
    "bt_api_ctp.cleaner_ctp",
    "_ctp",
)


def _imported_modules(path: Path) -> list[tuple[str, bool]]:
    """``(module, is_function_local)`` for every import in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    found: list[tuple[str, bool]] = []
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        if not modules:
            continue
        local = any(any(inner is node for inner in ast.walk(fn)) for fn in functions)
        found.extend((module, local) for module in modules)
    return found


class TestCleanerPackageIsVenueAgnostic:
    def test_framework_modules_do_not_import_ctp(self):
        base = Path(bt_api_ctp.cleaner.__file__).parent
        offenders: list[str] = []
        for path in sorted(base.rglob("*.py")):
            if path.name == _ASSEMBLY_POINT:
                continue
            for module, _ in _imported_modules(path):
                if module.startswith(_FORBIDDEN_PREFIXES):
                    offenders.append(f"{path.relative_to(base)}: {module}")
        assert offenders == []

    def test_cli_only_imports_ctp_lazily(self):
        base = Path(bt_api_ctp.cleaner.__file__).parent
        for module, local in _imported_modules(base / _ASSEMBLY_POINT):
            if module.startswith(_FORBIDDEN_PREFIXES):
                assert local, f"cli.py must import {module} inside a function"
