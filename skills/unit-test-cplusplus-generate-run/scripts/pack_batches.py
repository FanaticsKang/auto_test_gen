#!/usr/bin/env python3
from __future__ import annotations
"""
pack_batches.py — 从 test_cases.json 筛选 C++ 文件 + LPT 贪心打包成 sub-agent 批次。

用法:
    python3 scripts/pack_batches.py \\
        --baseline test/generated_unit/test_cases.json \\
        [--skip-dirs dir1 dir2] \\
        --output .test/batches.json \\
        [--k-max 10] \\
        [--batch-size 3]

退出码:
    0 — 成功
    1 — 参数错
    2 — baseline 不存在
    3 — 筛选后没有可处理的 C++ 文件
"""

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

_CPP_SUFFIXES = (".cpp", ".cc", ".cxx", ".c++")


def _is_cpp_source(path: str) -> bool:
    """只对 .cpp/.cc/.cxx 实现文件打包,不对头文件打包。"""
    return path.lower().endswith(_CPP_SUFFIXES)


def _is_in_skip_dirs(path: str, skip_dirs: list[str]) -> bool:
    for d in skip_dirs:
        dd = d.rstrip("/")
        if path == dd or path.startswith(dd + "/"):
            return True
    return False


def pack_lpt(files_with_counts: list[tuple[str, int]],
             agent_count: int, k_max: int) -> list[list[tuple[str, int]]]:
    """
    LPT 贪心装箱:按函数数降序逐个放入当前最轻的桶(未满 k_max 的桶里最轻的)。

    若所有现有桶都满 k_max,则新开一个桶。
    """
    sorted_files = sorted(files_with_counts, key=lambda x: -x[1])
    buckets: list[list[tuple[str, int]]] = [[] for _ in range(agent_count)]
    bucket_loads = [0] * agent_count

    for fp, cnt in sorted_files:
        # 找未满 k_max 的桶中 load 最小的
        best = -1
        best_load = math.inf
        for i, bucket in enumerate(buckets):
            if len(bucket) >= k_max:
                continue
            if bucket_loads[i] < best_load:
                best_load = bucket_loads[i]
                best = i
        if best < 0:
            # 所有桶都满 k_max,新开一个
            buckets.append([])
            bucket_loads.append(0)
            best = len(buckets) - 1
        buckets[best].append((fp, cnt))
        bucket_loads[best] += cnt

    # 剔除空桶
    return [b for b in buckets if b]


def main() -> int:
    parser = argparse.ArgumentParser(description="LPT 贪心打包 sub-agent 批次")
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--skip-dirs", nargs="*", default=[],
                        help="要跳过的顶层目录列表(与 coverage_config.exclude_dirs 取并集)")
    parser.add_argument("--output", required=True)
    parser.add_argument("--k-max", type=int, default=10,
                        help="单 sub-agent 最大文件数(默认 10)")
    parser.add_argument("--batch-size", type=int, default=3,
                        help="每批并发 sub-agent 数(默认 3)")
    args = parser.parse_args()

    if args.k_max < 1:
        print("错误: --k-max 必须 >= 1", file=sys.stderr)
        return 1
    if args.batch_size < 1:
        print("错误: --batch-size 必须 >= 1", file=sys.stderr)
        return 1

    baseline_path = Path(args.baseline)
    if not baseline_path.is_file():
        print(f"错误: baseline 不存在: {baseline_path}", file=sys.stderr)
        return 2

    try:
        with open(baseline_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"错误: 无法解析 baseline: {e}", file=sys.stderr)
        return 2

    if "cpp" not in data.get("languages", []):
        print("警告: baseline languages 不含 'cpp',仍尝试处理", file=sys.stderr)

    # 合并 skip_dirs 与 coverage_config.exclude_dirs
    baseline_excludes = data.get("coverage_config", {}).get("exclude_dirs", []) or []
    skip_dirs = sorted(set(args.skip_dirs) | set(baseline_excludes))

    # 筛选 C++ 源文件
    selected: list[tuple[str, int]] = []
    for src_path, finfo in data.get("files", {}).items():
        if not _is_cpp_source(src_path):
            continue
        if _is_in_skip_dirs(src_path, skip_dirs):
            continue
        func_count = len(finfo.get("functions", {}))
        if func_count == 0:
            # 没有函数的文件没必要派发 sub-agent
            continue
        selected.append((src_path, func_count))

    if not selected:
        print("错误: 过滤后没有可处理的 C++ 文件", file=sys.stderr)
        return 3

    total_files = len(selected)
    total_funcs = sum(c for _, c in selected)

    # 计算 agent_count
    agent_count = max(1, math.ceil(total_files / args.batch_size))

    # k_max 兜底:若 total_files / agent_count > k_max,需要提高 agent_count
    while math.ceil(total_files / agent_count) > args.k_max:
        agent_count += 1

    # LPT 打包
    buckets = pack_lpt(selected, agent_count, args.k_max)
    actual_agent_count = len(buckets)
    batch_count = math.ceil(actual_agent_count / args.batch_size)

    # 构建 batches 结构
    batches = []
    for batch_id in range(batch_count):
        start = batch_id * args.batch_size
        end = min(start + args.batch_size, actual_agent_count)
        agents = []
        for agent_id_in_batch, bucket_idx in enumerate(range(start, end)):
            bucket = buckets[bucket_idx]
            agents.append({
                "slug_prefix": f"cpp_gen_batch_{batch_id}_agent_{agent_id_in_batch}",
                "files": [fp for fp, _ in bucket],
                "total_functions": sum(c for _, c in bucket),
            })
        batches.append({"batch_id": batch_id, "agents": agents})

    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total_files": total_files,
        "total_functions": total_funcs,
        "skip_dirs": skip_dirs,
        "k_max": args.k_max,
        "batch_size": args.batch_size,
        "agent_count": actual_agent_count,
        "batch_count": batch_count,
        "batches": batches,
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(result, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(out_path)

    print(f"batches written to {out_path}, "
          f"agent_count={actual_agent_count}, batch_count={batch_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
