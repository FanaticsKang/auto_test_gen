#!/usr/bin/env python3
from __future__ import annotations
"""
collect_results.py — 扫描 .test/<slug>/run_result.json,聚合为单一 JSON。

用法:
    python3 scripts/collect_results.py \\
        --batches .test/batches.json \\
        --output .test/all_results.json

退出码:
    0 — 成功(即使部分 run_result 缺失)
    2 — batches.json 不存在
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


def _slug_of(source_path: str) -> str:
    return source_path.replace("/", "_").replace(".", "_")


def main() -> int:
    parser = argparse.ArgumentParser(description="聚合所有 run_result.json")
    parser.add_argument("--batches", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    batches_path = Path(args.batches)
    if not batches_path.is_file():
        print(f"错误: batches 不存在: {batches_path}", file=sys.stderr)
        return 2

    try:
        batches_data = json.loads(batches_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"错误: 无法解析 batches: {e}", file=sys.stderr)
        return 2

    # 收集所有预期的 (source_path, slug)
    expected: list[tuple[str, str]] = []
    for batch in batches_data.get("batches", []):
        for agent in batch.get("agents", []):
            for src in agent.get("files", []):
                expected.append((src, _slug_of(src)))

    # batches_path 一般在 .test/batches.json,工作目录就是其父目录
    workroot = batches_path.parent

    results = []
    missing = []
    for src, slug in expected:
        rr_path = workroot / slug / "run_result.json"
        if not rr_path.is_file():
            missing.append(src)
            continue
        try:
            rr = json.loads(rr_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"警告: 无法解析 {rr_path}: {e}", file=sys.stderr)
            missing.append(src)
            continue
        results.append({
            "source_path": src,
            "slug": slug,
            "status": rr.get("status", "unknown"),
            "run_result": rr,
        })

    total_expected = len(expected)
    total_found = len(results)

    output = {
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "total_files_expected": total_expected,
        "total_files_found": total_found,
        "missing_run_results": missing,
        "results": results,
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(output, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(out_path)

    print(f"collected {total_found}/{total_expected} run_results "
          f"(missing: {len(missing)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
