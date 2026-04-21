#!/usr/bin/env python3
"""
list_dirs.py — 从 scan_result.json 中提取被扫描文件的顶层目录。

输出去重后的顶层目录列表（排除默认已跳过的目录），供用户勾选排除范围。

用法：
    python scripts/list_dirs.py --scan .test/scan_result.json
"""

import argparse
import json
import sys

# 与 scan_repo.py 中 SKIP_DIRS + TEST_DIRS 保持一致
_ALREADY_SKIPPED = {
    "__pycache__", ".git", ".venv", "venv", "env",
    "node_modules", ".tox", ".mypy_cache", ".pytest_cache",
    "dist", "build", ".eggs", ".github", ".claude",
    "docs", "scripts", "third_party", "vendor",
    "test", "tests", "testing",
}


def main():
    parser = argparse.ArgumentParser(
        description="从扫描结果中提取顶层目录列表",
    )
    parser.add_argument("--scan", default=".test/scan_result.json",
                        help="scan_repo.py 的输出路径")
    args = parser.parse_args()

    try:
        with open(args.scan, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"错误: {args.scan} 不存在，请先运行 scan_repo.py", file=sys.stderr)
        sys.exit(1)

    dirs = set()
    for fp in data.get("files", {}):
        parts = fp.split("/")
        top = parts[0] if len(parts) > 1 else "."
        if top not in _ALREADY_SKIPPED:
            dirs.add(top)

    for d in sorted(dirs):
        print(d)


if __name__ == "__main__":
    main()
