#!/usr/bin/env python3
from __future__ import annotations
"""
writeback_baseline.py — 把 all_results.json 中的 cases 回写到 test_cases.json。

严格规则:
  - 只更新 baseline["files"][path]["functions"][func_key]["cases"]
  - 更新 baseline["tool_status"]
  - 其他字段一律不动(func_md5、dimensions、mocks_needed、coverage_config、summary 等)
  - atomic write

用法:
    python3 scripts/writeback_baseline.py \\
        --baseline test/generated_unit/test_cases.json \\
        --results .test/all_results.json \\
        [--tool-status .test/tool_status.json]

退出码:
    0 — 成功
    1 — 参数错
    2 — baseline / results 文件不存在或不可解析
    3 — baseline 结构异常无法回写
"""

import argparse
import json
import sys
from pathlib import Path


def _load_json(path: Path, name: str) -> dict | None:
    if not path.is_file():
        print(f"错误: {name} 不存在: {path}", file=sys.stderr)
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"错误: 无法解析 {name}: {e}", file=sys.stderr)
        return None


def _normalize_tool_status(tool_status_data: dict) -> dict[str, bool]:
    """
    把 check_env.py 的输出(含 tools / all_ok / missing 等)压扁为 {name: bool}。
    也兼容已经压扁的形式。
    """
    if "tools" in tool_status_data and isinstance(tool_status_data["tools"], dict):
        tools = tool_status_data["tools"]
        return {name: bool(info.get("ok", False)) for name, info in tools.items()}
    # 已是压扁形式,直接布尔化
    return {k: bool(v) for k, v in tool_status_data.items()
            if not isinstance(v, dict)}


def main() -> int:
    parser = argparse.ArgumentParser(description="把 cases 回写到 test_cases.json")
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--results", required=True)
    parser.add_argument("--tool-status", default=None)
    args = parser.parse_args()

    baseline_path = Path(args.baseline)
    results_path = Path(args.results)

    baseline = _load_json(baseline_path, "baseline")
    if baseline is None:
        return 2
    results = _load_json(results_path, "results")
    if results is None:
        return 2

    if not isinstance(baseline.get("files"), dict):
        print("错误: baseline.files 不是 object", file=sys.stderr)
        return 3

    tool_status: dict[str, bool] = {}
    if args.tool_status:
        ts_data = _load_json(Path(args.tool_status), "tool_status")
        if ts_data is None:
            return 2
        tool_status = _normalize_tool_status(ts_data)

    # 统计计数
    updated_files = 0
    updated_functions = 0
    total_cases_written = 0
    not_found_files = []   # run_result 的源文件在 baseline 里找不到
    not_found_funcs = []   # (source_path, func_key) 在 baseline 里找不到

    # 逐个应用 run_result 的 cases 回写
    for item in results.get("results", []):
        src = item.get("source_path")
        rr = item.get("run_result", {}) or {}
        cases_by_func = rr.get("cases", {}) or {}

        if src not in baseline["files"]:
            not_found_files.append(src)
            continue

        file_entry = baseline["files"][src]
        file_funcs = file_entry.get("functions", {}) or {}

        file_touched = False
        for func_key, case_list in cases_by_func.items():
            if func_key not in file_funcs:
                not_found_funcs.append((src, func_key))
                continue
            file_funcs[func_key]["cases"] = case_list
            updated_functions += 1
            total_cases_written += len(case_list) if isinstance(case_list, list) else 0
            file_touched = True

        if file_touched:
            updated_files += 1

    # 更新 tool_status
    if tool_status:
        existing_ts = baseline.get("tool_status", {})
        if not isinstance(existing_ts, dict):
            existing_ts = {}
        existing_ts.update(tool_status)
        baseline["tool_status"] = existing_ts

    # 原子写回
    tmp = baseline_path.with_suffix(baseline_path.suffix + ".tmp")
    tmp.write_text(json.dumps(baseline, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(baseline_path)

    # 报告
    print(f"updated {updated_files} files, {updated_functions} functions, "
          f"cases written {total_cases_written}")

    if not_found_files:
        print(f"警告: {len(not_found_files)} 个 source_path 在 baseline 里找不到",
              file=sys.stderr)
        for p in not_found_files[:5]:
            print(f"  - {p}", file=sys.stderr)
    if not_found_funcs:
        print(f"警告: {len(not_found_funcs)} 个函数在 baseline 里找不到",
              file=sys.stderr)
        for src, fk in not_found_funcs[:5]:
            print(f"  - {src}::{fk}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
