#!/usr/bin/env python3
from __future__ import annotations
"""
build_build_context.py — 从 CMakeLists + compile_commands 生成 .test/build_context.json。

合并了 cxx_standard 抽取逻辑:优先从 --cxx-standard 参数读,其次从 CMakeLists 解析。

用法:
    python3 scripts/build_build_context.py \\
        --repo-root <repo_root> \\
        --compile-commands build/compile_commands.json \\
        --output .test/build_context.json \\
        [--cxx-standard 17] \\
        [--top-n-includes 10]

退出码:
    0 — 成功
    1 — 参数错
    2 — 前置条件不满足
    3 — 抽不到 cxx_standard 且未传 --cxx-standard
"""

import argparse
import json
import re
import shlex
import shutil
import sys
from collections import Counter
from pathlib import Path


_CMAKE_STD_PATTERNS = [
    # set(CMAKE_CXX_STANDARD 17)
    re.compile(r"set\s*\(\s*CMAKE_CXX_STANDARD\s+(\d+)\s*\)", re.IGNORECASE),
    # CMAKE_CXX_STANDARD 17 CACHE STRING ...
    re.compile(r"CMAKE_CXX_STANDARD\s+(\d+)", re.IGNORECASE),
]


def extract_cxx_standard(cmake_path: Path) -> int | None:
    """从 CMakeLists.txt 抽 CMAKE_CXX_STANDARD。只抽不处理 target_compile_features。"""
    try:
        text = cmake_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for pat in _CMAKE_STD_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                v = int(m.group(1))
                if v in (98, 3, 11, 14, 17, 20, 23, 26):
                    return 17 if v in (98, 3) else v  # 过老值回退 17
                return v
            except ValueError:
                continue
    return None


def extract_top_includes(cc_path: Path, top_n: int) -> list[str]:
    """
    从 compile_commands.json 收集所有 -I/-isystem 路径,按出现频率降序取 top_n。
    路径保留原样(绝对或相对)。
    """
    try:
        entries = json.loads(cc_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    counter: Counter = Counter()
    for entry in entries:
        # compile_commands.json 每项要么有 arguments (list) 要么有 command (str)
        tokens: list[str] = []
        if "arguments" in entry and isinstance(entry["arguments"], list):
            tokens = list(entry["arguments"])
        elif "command" in entry and isinstance(entry["command"], str):
            try:
                tokens = shlex.split(entry["command"])
            except ValueError:
                continue
        # 收 -I<path>、-I <path>、-isystem<path>、-isystem <path>
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok.startswith("-I") and len(tok) > 2:
                counter[tok[2:]] += 1
            elif tok == "-I" and i + 1 < len(tokens):
                counter[tokens[i + 1]] += 1
                i += 1
            elif tok.startswith("-isystem") and len(tok) > len("-isystem"):
                counter[tok[len("-isystem"):]] += 1
            elif tok == "-isystem" and i + 1 < len(tokens):
                counter[tokens[i + 1]] += 1
                i += 1
            i += 1

    return [p for p, _ in counter.most_common(top_n)]


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 build_context.json")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--compile-commands", required=True,
                        help="compile_commands.json 相对 repo_root 的路径")
    parser.add_argument("--output", required=True)
    parser.add_argument("--cxx-standard", type=int, default=None,
                        help="强制指定 C++ 标准;未传则从 CMakeLists 自动抽取")
    parser.add_argument("--top-n-includes", type=int, default=10)
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    cmake_path = repo_root / "CMakeLists.txt"
    cc_path = repo_root / args.compile_commands

    # 前置条件
    if not cmake_path.is_file():
        print(f"错误: CMakeLists.txt 不存在: {cmake_path}", file=sys.stderr)
        return 2
    if not cc_path.is_file():
        print(f"错误: compile_commands.json 不存在: {cc_path}", file=sys.stderr)
        return 2
    if not shutil.which("cmake"):
        print("错误: cmake 不在 PATH", file=sys.stderr)
        return 2

    # cxx_standard
    cxx_std = args.cxx_standard
    if cxx_std is None:
        cxx_std = extract_cxx_standard(cmake_path)
    if cxx_std is None:
        print("错误: 无法从 CMakeLists 抽取 CMAKE_CXX_STANDARD,"
              "请通过 --cxx-standard 指定", file=sys.stderr)
        return 3

    # top-N includes
    common_includes = extract_top_includes(cc_path, args.top_n_includes)

    result = {
        "build_system": "cmake",
        "compile_commands_path": args.compile_commands,
        "common_include_dirs": common_includes,
        "common_link_libraries": [],
        "cxx_standard": cxx_std,
        "extra_cxxflags": [],
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(result, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(out_path)

    print(f"build_context written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
