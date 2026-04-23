#!/usr/bin/env python3
from __future__ import annotations
"""
list_top_dirs.py — 从 test_cases.json 的 files 提取顶层目录。

供 Claude 调用 AskUserQuestion 让用户勾选排除的目录。

用法:
    python3 scripts/list_top_dirs.py \\
        --baseline test/generated_unit/test_cases.json \\
        [--language cpp]

stdout: 每行一个顶层目录。
退出码:
    0 — 成功
    1 — 参数错
    2 — baseline 文件不存在或不合法
"""

import argparse
import json
import sys
from pathlib import Path


# 与 init skill 的 list_dirs.py 保持一致
_ALREADY_SKIPPED = {
    "__pycache__", ".git", ".venv", "venv", "env",
    "node_modules", ".tox", ".mypy_cache", ".pytest_cache",
    "dist", "build", ".eggs", ".github", ".claude",
    "docs", "scripts", "third_party", "vendor",
    "test", "tests", "testing",
}

_CPP_SUFFIXES = (".cpp", ".cc", ".cxx", ".c++", ".hpp", ".h", ".hh", ".hxx")
_PY_SUFFIXES = (".py",)


def _matches_language(path: str, language: str) -> bool:
    p = path.lower()
    if language == "cpp":
        return p.endswith(_CPP_SUFFIXES)
    if language == "python":
        return p.endswith(_PY_SUFFIXES)
    if language == "all":
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="列出基线中的顶层目录")
    parser.add_argument("--baseline", required=True,
                        help="test_cases.json 路径")
    parser.add_argument("--language", default="cpp",
                        choices=["cpp", "python", "all"],
                        help="按语言过滤(默认 cpp)")
    args = parser.parse_args()

    baseline_path = Path(args.baseline)
    if not baseline_path.is_file():
        print(f"错误: baseline 文件不存在: {baseline_path}", file=sys.stderr)
        return 2

    try:
        with open(baseline_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"错误: 无法解析 baseline: {e}", file=sys.stderr)
        return 2

    dirs: set[str] = set()
    for fp in data.get("files", {}):
        if not _matches_language(fp, args.language):
            continue
        parts = fp.split("/")
        top = parts[0] if len(parts) > 1 else "."
        if top not in _ALREADY_SKIPPED:
            dirs.add(top)

    for d in sorted(dirs):
        print(d)

    return 0


if __name__ == "__main__":
    sys.exit(main())
