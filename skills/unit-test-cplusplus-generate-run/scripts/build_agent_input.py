#!/usr/bin/env python3
from __future__ import annotations
"""
build_agent_input.py — 为指定的 (batch_id, agent_id) 生成 sub-agent 的 input JSON。

一次调用生成一个 agent 的输入,Claude 按批循环调用。

用法:
    python3 scripts/build_agent_input.py \\
        --baseline test/generated_unit/test_cases.json \\
        --batches .test/batches.json \\
        --batch-id 0 \\
        --agent-id 1 \\
        --repo-root /abs/path/to/repo \\
        --build-context-path .test/build_context.json \\
        --scripts-dir /abs/path/to/scripts \\
        --output-dir .test/

stdout: 生成的 agent_input.json 的绝对路径。
退出码:
    0 — 成功
    1 — 参数错
    2 — baseline / batches 文件不存在,或 batch_id/agent_id 越界
"""

import argparse
import json
import sys
from pathlib import Path


# 基线函数级字段中,要保留给 sub-agent 的白名单
_FUNC_FIELDS = (
    "func_md5", "line_range", "signature",
    "class_name", "namespace",
    "is_template", "is_static", "is_virtual",
    "dimensions", "mocks_needed",
    # Python 特有的 is_async 也带上(虽然本 skill 只处理 cpp)
    "is_async",
)


def _slug_of(source_path: str) -> str:
    """core/parser.cpp -> core_parser_cpp."""
    return source_path.replace("/", "_").replace(".", "_")


def _build_paths(slug: str, output_dir: Path) -> dict:
    """根据 slug 和 output_dir(通常是 .test/) 拼出文件级路径。"""
    work_dir = f"{output_dir.as_posix().rstrip('/')}/{slug}/"
    build_dir = f"{work_dir}build/"
    return {
        "slug": slug,
        "work_dir": work_dir,
        "run_result": f"{work_dir}run_result.json",
        "log": f"{work_dir}process.log",
        "input_backup": f"{work_dir}input.json",
        "build_dir": build_dir,
    }


def _trim_func(raw: dict) -> dict:
    """只保留 _FUNC_FIELDS 中的字段。"""
    out = {k: raw[k] for k in _FUNC_FIELDS if k in raw}
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="生成单个 sub-agent 的 input JSON")
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--batches", required=True)
    parser.add_argument("--batch-id", type=int, required=True)
    parser.add_argument("--agent-id", type=int, required=True)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--build-context-path", required=True)
    parser.add_argument("--scripts-dir", required=True)
    parser.add_argument("--output-dir", required=True,
                        help="sub-agent 工作目录的父目录,如 .test/")
    args = parser.parse_args()

    # 前置条件
    baseline_path = Path(args.baseline)
    batches_path = Path(args.batches)
    if not baseline_path.is_file():
        print(f"错误: baseline 不存在: {baseline_path}", file=sys.stderr)
        return 2
    if not batches_path.is_file():
        print(f"错误: batches 不存在: {batches_path}", file=sys.stderr)
        return 2

    try:
        with open(baseline_path, "r", encoding="utf-8") as f:
            baseline = json.load(f)
        with open(batches_path, "r", encoding="utf-8") as f:
            batches_data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"错误: JSON 解析失败: {e}", file=sys.stderr)
        return 2

    # 定位 batch 和 agent
    batches = batches_data.get("batches", [])
    target_batch = next((b for b in batches if b["batch_id"] == args.batch_id), None)
    if target_batch is None:
        print(f"错误: batch_id={args.batch_id} 不存在", file=sys.stderr)
        return 2
    agents = target_batch.get("agents", [])
    if args.agent_id < 0 or args.agent_id >= len(agents):
        print(f"错误: agent_id={args.agent_id} 越界 "
              f"(本 batch 共 {len(agents)} 个 agent)", file=sys.stderr)
        return 2

    agent_info = agents[args.agent_id]
    file_paths = agent_info["files"]

    # 构建每个文件的 schema
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    baseline_files = baseline.get("files", {})

    files_payload = []
    for src_path in file_paths:
        finfo = baseline_files.get(src_path)
        if not finfo:
            print(f"警告: baseline 中找不到 {src_path},跳过", file=sys.stderr)
            continue
        slug = _slug_of(src_path)
        funcs_out = {
            k: _trim_func(v) for k, v in finfo.get("functions", {}).items()
        }
        files_payload.append({
            "source_path": src_path,
            "test_path": finfo.get("test_path", ""),
            "file_md5": finfo.get("file_md5", ""),
            "functions": funcs_out,
            "paths": _build_paths(slug, output_dir),
        })

    # coverage_config 从 baseline 拷出三项阈值
    cov = baseline.get("coverage_config", {})
    coverage_config = {
        "statement_threshold": cov.get("statement_threshold", 90),
        "branch_threshold":    cov.get("branch_threshold", 90),
        "function_threshold":  cov.get("function_threshold", 100),
    }

    result = {
        "files": files_payload,
        "coverage_config": coverage_config,
        "repo_root": str(Path(args.repo_root).resolve()),
        "build_context_path": args.build_context_path,
        "scripts_dir": str(Path(args.scripts_dir).resolve()),
    }

    # agent 的 input JSON 放在各自的目录下
    agent_dir = output_dir / agent_info["slug_prefix"]
    agent_dir.mkdir(parents=True, exist_ok=True)
    out_path = agent_dir / "agent_input.json"
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(result, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(out_path)

    # 输出绝对路径给 Claude
    print(out_path.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
