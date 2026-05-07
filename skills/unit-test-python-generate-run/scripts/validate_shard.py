#!/usr/bin/env python3
"""
validate_shard.py — 校验 state_shard / bug_shard JSON 格式。

子命令：
  state  校验 state_shard 格式（func_data 必须为 dict 包裹，非裸列表）
  bug    校验 bug_shard 格式（bugs 字段必须为 list）

默认模式：两个都校验（--state-shard / --bug-shard 均为可选，提供则校验）。

用法：
  python validate_shard.py --state-shard .test/state_shards/core_parser_py.json
  python validate_shard.py --state-shard .test/state_shards/core_parser_py.json --bug-shard .test/bug_shards/core_parser_py.json
  python validate_shard.py state --file .test/state_shards/core_parser_py.json
  python validate_shard.py bug --file .test/bug_shards/core_parser_py.json

返回码：
  0 = 格式正确
  1 = 格式错误（会输出具体错误信息）
"""

import argparse
import json
import sys


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[validate-shard] 文件不存在: {path}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"[validate-shard] JSON 解析失败: {path}: {e}", file=sys.stderr)
        sys.exit(1)


def validate_state_shard(file_path):
    data = _load_json(file_path)
    errors = []

    if "files" not in data:
        errors.append("缺少顶层 'files' 键")
    else:
        files = data["files"]
        if not isinstance(files, dict):
            errors.append(f"'files' 应为 dict，实际为 {type(files).__name__}")
        else:
            for src_path, file_data in files.items():
                if not isinstance(file_data, dict):
                    errors.append(f"files['{src_path}'] 应为 dict，实际为 {type(file_data).__name__}")
                    continue
                funcs = file_data.get("functions", {})
                if not isinstance(funcs, dict):
                    errors.append(f"files['{src_path}'].functions 应为 dict，实际为 {type(funcs).__name__}")
                    continue
                for func_key, func_data in funcs.items():
                    if isinstance(func_data, list):
                        errors.append(
                            f"files['{src_path}'].functions['{func_key}'] 是裸列表 (len={len(func_data)})，"
                            f"应为 dict 格式: {{'func_md5_at_gen': '', 'cases': [...]}}"
                        )
                    elif not isinstance(func_data, dict):
                        errors.append(
                            f"files['{src_path}'].functions['{func_key}'] 应为 dict，实际为 {type(func_data).__name__}"
                        )
                    elif "cases" not in func_data:
                        errors.append(
                            f"files['{src_path}'].functions['{func_key}'] 缺少 'cases' 字段"
                        )
                    elif not isinstance(func_data["cases"], list):
                        errors.append(
                            f"files['{src_path}'].functions['{func_key}'].cases 应为 list，"
                            f"实际为 {type(func_data['cases']).__name__}"
                        )

    if "last_round" not in data:
        errors.append("缺少顶层 'last_round' 字段")

    if errors:
        for e in errors:
            print(f"[validate-shard] state ERROR: {e}", file=sys.stderr)
        return False

    total_funcs = 0
    total_cases = 0
    for file_data in data.get("files", {}).values():
        for func_data in file_data.get("functions", {}).values():
            total_funcs += 1
            total_cases += len(func_data.get("cases", []))

    print(f"[validate-shard] state: ok ({len(data['files'])} files, {total_funcs} functions, {total_cases} cases)")
    return True


def validate_bug_shard(file_path):
    data = _load_json(file_path)
    errors = []

    if "bugs" not in data:
        errors.append("缺少顶层 'bugs' 键")
    elif not isinstance(data["bugs"], list):
        errors.append(f"'bugs' 应为 list，实际为 {type(data['bugs']).__name__}")

    if errors:
        for e in errors:
            print(f"[validate-shard] bug ERROR: {e}", file=sys.stderr)
        return False

    print(f"[validate-shard] bug: ok ({len(data['bugs'])} bugs)")
    return True


def main():
    parser = argparse.ArgumentParser(description="校验 state_shard / bug_shard JSON 格式")
    sub = parser.add_subparsers(dest="command")

    p_state = sub.add_parser("state", help="校验 state_shard 格式")
    p_state.add_argument("--file", required=True, help="state_shard JSON 文件路径")

    p_bug = sub.add_parser("bug", help="校验 bug_shard 格式")
    p_bug.add_argument("--file", required=True, help="bug_shard JSON 文件路径")

    # 默认模式（无子命令）：同时支持 --state-shard 和 --bug-shard
    parser.add_argument("--state-shard", default=None, help="state_shard JSON 文件路径")
    parser.add_argument("--bug-shard", default=None, help="bug_shard JSON 文件路径")

    args = parser.parse_args()

    if args.command == "state":
        ok = validate_state_shard(args.file)
        sys.exit(0 if ok else 1)

    if args.command == "bug":
        ok = validate_bug_shard(args.file)
        sys.exit(0 if ok else 1)

    # 默认模式：至少提供一个
    if not args.state_shard and not args.bug_shard:
        parser.print_help()
        sys.exit(1)

    ok = True
    if args.state_shard:
        ok = validate_state_shard(args.state_shard) and ok
    if args.bug_shard:
        ok = validate_bug_shard(args.bug_shard) and ok

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
