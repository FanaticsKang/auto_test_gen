#!/usr/bin/env python3
"""
dispatch.py — 单测生成流水线的调度编排脚本。

子命令：
  init    从 test_cases.json 生成 generate_process.json
  batch   只读筛选：返回 n 个待处理文件的信息，不改状态
  claim   原子选 N 个文件并标 "running"（含 claimed_at），返回与 batch 同构的信息
  report  按源文件输出测试分析报告（Markdown / JSON）

claim 对比 batch：batch 只查不写、适合 dry-run；claim 原子写回状态、适合并行派发。

用法：
  python dispatch.py init   --baseline test/generated_unit/test_cases.json --output test/generated_unit/generate_process.json
  python dispatch.py batch  --process  test/generated_unit/generate_process.json --baseline test/generated_unit/test_cases.json --number 3
  python dispatch.py claim  --process  test/generated_unit/generate_process.json --baseline test/generated_unit/test_cases.json --number 3 [--stale-seconds 1800]
  python dispatch.py report --baseline ... --run-state ... [--run-result ... | --run-results-dir ...] --output .test/report.md
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------

def _write_json_atomic(data, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(output_path)


def _load_json(path) -> dict:
    p = Path(path)
    if not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Per-file shard 路径规划
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


def _slug(src_path: str) -> str:
    """把源文件路径映射成文件名安全的 slug。"""
    s = _SLUG_RE.sub("_", src_path).strip("_")
    return s or "file"


def _shard_paths(shards_root: str, src_path: str) -> dict:
    """返回 sub-agent 为该源文件使用的 per-file 路径集。"""
    slug = _slug(src_path)
    root = shards_root.rstrip("/")
    return {
        "slug": slug,
        "run_result": f"{root}/run_results/{slug}.json",
        "state_shard": f"{root}/state_shards/{slug}.json",
        "bug_shard": f"{root}/bug_shards/{slug}.json",
    }


def _build_file_info(src_path, proc_files, bl_files, cov_config, max_iter, shards_root):
    """构造 batch / claim 共用的单文件返回结构。"""
    bl_file = bl_files.get(src_path, {})
    functions = {}
    for func_key, fmeta in bl_file.get("functions", {}).items():
        # test_optional 函数不发给子 agent
        if fmeta.get("test_optional"):
            continue
        functions[func_key] = {
            "dimensions": fmeta.get("dimensions", []),
            "line_range": fmeta.get("line_range", []),
            "signature": fmeta.get("signature", ""),
            "mocks_needed": fmeta.get("mocks_needed", []),
        }
    return {
        "source_path": src_path,
        "test_path": proc_files[src_path].get("test_path", bl_file.get("test_path", "")),
        "file_md5": proc_files[src_path].get("file_md5", ""),
        "functions": functions,
        "coverage_config": cov_config,
        "max_iterations": max_iter,
        "paths": _shard_paths(shards_root, src_path),
    }


# ---------------------------------------------------------------------------
# init: 从基线生成 generate_process.json
# ---------------------------------------------------------------------------

def cmd_init(args):
    baseline = _load_json(args.baseline)
    if not baseline:
        print(f"错误: 基线 {args.baseline} 不存在或为空", file=sys.stderr)
        sys.exit(1)

    cov_config = baseline.get("coverage_config", {})

    files = {}
    for src_path, finfo in baseline.get("files", {}).items():
        files[src_path] = {
            "file_md5": finfo.get("file_md5", ""),
            "test_path": finfo.get("test_path", ""),
            "status": "pending",
            "result": None,
        }

    process = {
        "version": "1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "baseline_ref": str(args.baseline),
        "max_iterations": args.max_iterations,
        "shards_root": args.shards_root,
        "coverage_config": cov_config,
        "files": files,
    }

    _write_json_atomic(process, Path(args.output))
    print(
        f"已生成调度状态文件: {args.output}\n"
        f"  文件数: {len(files)}\n"
        f"  最大迭代: {args.max_iterations}\n"
        f"  shards 根目录: {args.shards_root}",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# batch / claim: 选取 n 个待处理文件，输出子 agent 所需信息
# ---------------------------------------------------------------------------

def _terminal_statuses():
    return {"completed", "unmet", "abandoned"}


def _priority_score(src_path: str, bl_file: dict) -> float:
    """计算文件优先级分数，越高越先派发。

    因子：
      - 函数数量 × dimension 数量（复杂度高 → 先做）
      - security / exception 维度加权（高风险信号早暴露）
      - 有 mocks_needed 的函数占比（需要 mock 的先做，避免后期阻塞）
    """
    funcs = bl_file.get("functions", {})
    if not funcs:
        return 0.0

    total_dims = 0
    high_risk_dims = 0
    mock_funcs = 0

    for fmeta in funcs.values():
        dims = fmeta.get("dimensions", [])
        total_dims += len(dims)
        if any(d in dims for d in ("security", "exception")):
            high_risk_dims += 1
        if fmeta.get("mocks_needed"):
            mock_funcs += 1

    n_funcs = len(funcs)
    dim_factor = total_dims / n_funcs if n_funcs else 0
    risk_bonus = high_risk_dims * 3.0
    mock_bonus = (mock_funcs / n_funcs) * 2.0 if n_funcs else 0

    return n_funcs * dim_factor + risk_bonus + mock_bonus


def _select_candidates(proc_files, stale_seconds, bl_files=None):
    """候选 = 未创建 + stale 任务，按优先级排序。"""
    raw = [
        path for path, info in proc_files.items()
        if info.get("status") == "pending"
    ]
    if stale_seconds and stale_seconds > 0:
        now = datetime.now()
        for path, info in proc_files.items():
            if info.get("status") != "running":
                continue
            claimed_at = info.get("claimed_at")
            if not claimed_at:
                continue
            try:
                ts = datetime.fromisoformat(claimed_at)
            except ValueError:
                continue
            if (now - ts).total_seconds() > stale_seconds:
                raw.append(path)

    # 按优先级排序（高 → 低）
    if bl_files:
        raw.sort(key=lambda p: _priority_score(p, bl_files.get(p, {})), reverse=True)
    return raw


def _overall_state(proc_files):
    statuses = {info.get("status") for info in proc_files.values()}
    all_done = bool(proc_files) and statuses.issubset(_terminal_statuses())
    return {"all_done": all_done, "status_counts": _count_statuses(proc_files)}


def _count_statuses(proc_files):
    counts = {}
    for info in proc_files.values():
        counts[info.get("status", "?")] = counts.get(info.get("status", "?"), 0) + 1
    return counts


def _resolve_common(process, baseline):
    cov_config = process.get("coverage_config", baseline.get("coverage_config", {}))
    max_iter = process.get("max_iterations", 5)
    shards_root = process.get("shards_root", ".test")
    return cov_config, max_iter, shards_root


def cmd_batch(args):
    process = _load_json(args.process)
    baseline = _load_json(args.baseline)

    if not process:
        print(f"错误: 调度状态 {args.process} 不存在或为空", file=sys.stderr)
        sys.exit(1)
    if not baseline:
        print(f"错误: 基线 {args.baseline} 不存在或为空", file=sys.stderr)
        sys.exit(1)

    cov_config, max_iter, shards_root = _resolve_common(process, baseline)
    proc_files = process.get("files", {})
    bl_files = baseline.get("files", {})

    candidates = _select_candidates(proc_files, args.stale_seconds, bl_files)
    batch_paths = candidates[:args.number]

    overall = _overall_state(proc_files)
    if not batch_paths:
        print(json.dumps({
            "batch_size": 0,
            "files": [],
            **overall,
        }, ensure_ascii=False))
        return

    batch = [
        _build_file_info(p, proc_files, bl_files, cov_config, max_iter, shards_root)
        for p in batch_paths
    ]

    print(json.dumps({
        "batch_size": len(batch),
        "files": batch,
        **overall,
    }, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# claim: 原子选 N 个，改状态为 "running"，返回同构信息
# ---------------------------------------------------------------------------

def cmd_claim(args):
    process_path = Path(args.process)
    process = _load_json(process_path)
    baseline = _load_json(args.baseline)

    if not process:
        print(f"错误: 调度状态 {args.process} 不存在或为空", file=sys.stderr)
        sys.exit(1)
    if not baseline:
        print(f"错误: 基线 {args.baseline} 不存在或为空", file=sys.stderr)
        sys.exit(1)

    cov_config, max_iter, shards_root = _resolve_common(process, baseline)
    proc_files = process.get("files", {})
    bl_files = baseline.get("files", {})

    candidates = _select_candidates(proc_files, args.stale_seconds, bl_files)
    claim_paths = candidates[:args.number]

    now_iso = datetime.now().isoformat(timespec="seconds")
    reclaimed = []
    for p in claim_paths:
        info = proc_files.get(p, {})
        prev = info.get("status")
        info["status"] = "running"
        info["claimed_at"] = now_iso
        info["claim_round"] = info.get("claim_round", 0) + 1
        proc_files[p] = info
        if prev == "running":
            reclaimed.append(p)

    # 原子写回
    if claim_paths:
        _write_json_atomic(process, process_path)

    overall = _overall_state(proc_files)
    batch = [
        _build_file_info(p, proc_files, bl_files, cov_config, max_iter, shards_root)
        for p in claim_paths
    ]

    print(json.dumps({
        "batch_size": len(batch),
        "claimed_at": now_iso,
        "reclaimed_stale": reclaimed,
        "files": batch,
        **overall,
    }, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# report: 按文件输出测试分析报告
# ---------------------------------------------------------------------------

def _truncate_tb(tb: str, max_chars: int = 1200) -> str:
    if not tb:
        return ""
    tb = tb.strip()
    if len(tb) <= max_chars:
        return tb
    return tb[:max_chars] + "\n... [truncated]"


def _group_bugs_by_file(source_bugs: dict) -> dict:
    buckets = {}
    for b in source_bugs.get("bugs", []):
        buckets.setdefault(b.get("file", "(unknown)"), []).append(b)
    return buckets


def _collect_tests_by_case(run_result: dict) -> dict:
    by_case = {}
    for t in run_result.get("tests", []):
        cid = t.get("case_id")
        if cid:
            by_case[cid] = t
    return by_case


def _aggregate_run_results_dir(results_dir: Path) -> dict:
    """把 .test/run_results/*.json 合成一份单 run_result：tests 拼接，coverage 并集。

    如果多个 shard 报告了同一个 coverage key（文件路径），后者覆盖前者——
    并行模式下每个 shard 负责一个源文件，通常不会冲突。
    """
    merged_tests = []
    merged_cov = {}
    language = None
    generated_at = None
    return_code = 0
    md5_drifts = []

    if not results_dir.is_dir():
        return {
            "language": None, "generated_at": None, "return_code": 0,
            "tests": [], "coverage": {}, "md5_drifts": [],
            "summary": {"coverage": {}},
        }

    for shard in sorted(results_dir.glob("*.json")):
        try:
            data = json.loads(shard.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"警告: run_result shard {shard} 无法解析: {e}", file=sys.stderr)
            continue
        language = language or data.get("language")
        generated_at = generated_at or data.get("generated_at")
        if data.get("return_code"):
            return_code = data["return_code"]
        merged_tests.extend(data.get("tests", []) or [])
        for k, v in (data.get("coverage") or {}).items():
            merged_cov[k] = v
        md5_drifts.extend(data.get("md5_drifts", []) or [])

    # 重算 summary.coverage（文件并集，不跨 shard 计算比例）
    ts = cs = tb = cb = tf = cf = 0
    for f in merged_cov.values():
        ts += f.get("total_statements", 0)
        cs += f.get("covered_statements", 0)
        tb += f.get("total_branches", 0)
        cb += f.get("covered_branches", 0)
        for fc in f.get("functions", {}).values():
            tf += 1
            if fc.get("covered"):
                cf += 1

    return {
        "language": language,
        "generated_at": generated_at,
        "return_code": return_code,
        "md5_drifts": md5_drifts,
        "tests": merged_tests,
        "coverage": merged_cov,
        "summary": {
            "coverage": {
                "statement_rate": round(cs / ts * 100, 1) if ts else 0.0,
                "branch_rate": round(cb / tb * 100, 1) if tb else 0.0,
                "function_rate": round(cf / tf * 100, 1) if tf else 0.0,
                "covered_statements": cs, "total_statements": ts,
                "covered_branches": cb, "total_branches": tb,
                "covered_functions": cf, "total_functions": tf,
            }
        },
    }


def _render_markdown(baseline, run_state, run_result, source_bugs, process) -> str:
    rs_files = run_state.get("files", {})
    run_cov = run_result.get("coverage", {})
    tests_by_case = _collect_tests_by_case(run_result)
    bugs_by_file = _group_bugs_by_file(source_bugs)
    proc_files = (process or {}).get("files", {})

    output = []
    output.append("# 测试分析报告（按文件）\n")
    output.append(f"- 语言: {run_result.get('language', '-')}")
    output.append(f"- 生成时间: {run_result.get('generated_at', '-')}\n")

    for src_path in sorted(baseline.get("files", {}).keys()):
        bl_file = baseline["files"][src_path]
        rs_file = rs_files.get(src_path, {})
        file_cov = run_cov.get(src_path, {})
        file_bugs = bugs_by_file.get(src_path, [])
        proc_info = proc_files.get(src_path, {})

        output.append(f"\n## `{src_path}`\n")
        output.append(f"- 测试文件: `{bl_file.get('test_path', '-')}`")

        # 调度结果
        if proc_info.get("result"):
            result = proc_info["result"]
            unmet = result.get("unmet_reasons", [])
            dead = result.get("dead_code", False)
            if unmet:
                output.append(f"- **未达标项**: {'; '.join(unmet)}")
            if dead:
                output.append(f"- **存在 dead code**: 是")
            iters = result.get("iterations_used", "-")
            output.append(f"- 子 agent 迭代次数: {iters}")

        output.append(
            f"- 文件覆盖率: 语句 {file_cov.get('statement_rate', 0)}%, "
            f"分支 {file_cov.get('branch_rate', 0)}% "
            f"({file_cov.get('covered_statements', 0)}/{file_cov.get('total_statements', 0)} stmt, "
            f"{file_cov.get('covered_branches', 0)}/{file_cov.get('total_branches', 0)} branch)"
        )

        missed_lines = file_cov.get("missed_lines", [])
        missed_branches = file_cov.get("missed_branches", [])
        if missed_lines:
            output.append(f"- 未覆盖行: {missed_lines[:20]}{' ...' if len(missed_lines) > 20 else ''}")
        if missed_branches:
            output.append(f"- 未覆盖分支 (line, idx): {missed_branches[:20]}{' ...' if len(missed_branches) > 20 else ''}")

        funcs = bl_file.get("functions", {})
        if not funcs:
            output.append("- _(本文件没有可测试函数)_")
            continue

        for func_key in sorted(funcs.keys()):
            fmeta = funcs[func_key]
            rs_func = rs_file.get("functions", {}).get(func_key, {})
            cases = rs_func.get("cases", [])
            fcov = file_cov.get("functions", {}).get(func_key, {})

            passed = sum(1 for c in cases if c.get("status") == "passed")
            failed = sum(1 for c in cases if c.get("status") in ("failed", "failed_persistent"))
            source_bug_cnt = sum(1 for c in cases if c.get("status") == "source_bug")
            pending = sum(1 for c in cases if c.get("status") == "pending")

            stmt_rate = fcov.get("statement_rate", "-")
            output.append(
                f"\n### `{func_key}`  "
                f"_(lines {fmeta.get('line_range', [0, 0])[0]}–{fmeta.get('line_range', [0, 0])[1]})_\n"
            )
            output.append(f"- 签名: `{fmeta.get('signature', '-')}`")
            output.append(f"- 维度: {', '.join(fmeta.get('dimensions', [])) or '-'}")
            output.append(
                f"- 用例: {len(cases)} 个 "
                f"(通过 {passed} / 失败 {failed} / 源码bug {source_bug_cnt} / 待跑 {pending})"
            )
            output.append(f"- 函数级语句覆盖率: {stmt_rate}%")

            if fcov.get("missed_lines"):
                output.append(f"- 未覆盖行: {fcov['missed_lines']}")
            if fcov.get("missed_branches"):
                output.append(f"- 未覆盖分支: {fcov['missed_branches']}")

            if not cases:
                output.append("- _(无用例)_")
                continue

            output.append("\n| ID | 维度 | 状态 | 测试函数 | 说明 |")
            output.append("|---|---|---|---|---|")
            for c in cases:
                status = c.get("status", "-")
                reason = c.get("failure_reason") or ""
                status_cell = f"{status}" + (f" ({reason})" if reason else "")
                desc = (c.get("description") or "").replace("|", "\\|").replace("\n", " ")
                if len(desc) > 80:
                    desc = desc[:80] + "..."
                output.append(
                    f"| {c.get('id', '-')} | {c.get('dimension', '-')} | "
                    f"{status_cell} | `{c.get('test_name', '-')}` | {desc} |"
                )

            failures_to_show = [
                c for c in cases
                if c.get("status") in ("failed", "failed_persistent", "source_bug")
            ]
            if failures_to_show:
                output.append("\n**失败详情：**\n")
                for c in failures_to_show:
                    tinfo = tests_by_case.get(c.get("id"))
                    output.append(f"- **{c.get('id')}** ({c.get('status')}): {c.get('description', '')}")
                    if c.get("failure_reason"):
                        output.append(f"  - 分类: `{c['failure_reason']}`")
                    if tinfo and tinfo.get("traceback"):
                        tb = _truncate_tb(tinfo["traceback"])
                        output.append(f"  ```\n{tb}\n  ```")

        if file_bugs:
            output.append(f"\n### 源代码疑似 bug ({len(file_bugs)})\n")
            for b in file_bugs:
                output.append(
                    f"- **{b.get('function', '-')}** "
                    f"(case `{b.get('case_id', '-')}`, "
                    f"复现 {b.get('occurrence_count', 1)} 次): {b.get('reason', '')}"
                )

    return "\n".join(output) + "\n"


def _render_json_report(baseline, run_state, run_result, source_bugs, process) -> str:
    rs_files = run_state.get("files", {})
    run_cov = run_result.get("coverage", {})
    bugs_by_file = _group_bugs_by_file(source_bugs)
    proc_files = (process or {}).get("files", {})

    files = []
    for src_path in sorted(baseline.get("files", {}).keys()):
        bl_file = baseline["files"][src_path]
        rs_file = rs_files.get(src_path, {})
        file_cov = run_cov.get(src_path, {})
        proc_info = proc_files.get(src_path, {})

        func_entries = []
        for func_key in sorted(bl_file.get("functions", {}).keys()):
            fmeta = bl_file["functions"][func_key]
            rs_func = rs_file.get("functions", {}).get(func_key, {})
            cases = rs_func.get("cases", [])
            fcov = file_cov.get("functions", {}).get(func_key, {})
            tests_by_case = _collect_tests_by_case(run_result)

            case_entries = []
            for c in cases:
                tinfo = tests_by_case.get(c.get("id")) or {}
                case_entries.append({
                    "id": c.get("id"),
                    "dimension": c.get("dimension"),
                    "description": c.get("description"),
                    "test_name": c.get("test_name"),
                    "status": c.get("status"),
                    "failure_reason": c.get("failure_reason"),
                    "traceback": tinfo.get("traceback"),
                })

            func_entries.append({
                "function": func_key,
                "signature": fmeta.get("signature"),
                "line_range": fmeta.get("line_range"),
                "dimensions": fmeta.get("dimensions"),
                "statement_rate": fcov.get("statement_rate"),
                "missed_lines": fcov.get("missed_lines", []),
                "missed_branches": fcov.get("missed_branches", []),
                "cases": case_entries,
            })

        file_entry = {
            "file": src_path,
            "test_path": bl_file.get("test_path"),
            "file_coverage": {
                "statement_rate": file_cov.get("statement_rate"),
                "branch_rate": file_cov.get("branch_rate"),
                "missed_lines": file_cov.get("missed_lines", []),
                "missed_branches": file_cov.get("missed_branches", []),
            },
            "functions": func_entries,
            "source_bugs": bugs_by_file.get(src_path, []),
        }
        if proc_info.get("result"):
            file_entry["dispatch_result"] = proc_info["result"]
        files.append(file_entry)

    return json.dumps({
        "language": run_result.get("language"),
        "generated_at": run_result.get("generated_at"),
        "files": files,
    }, indent=2, ensure_ascii=False)


def cmd_report(args):
    baseline = _load_json(args.baseline)
    run_state = _load_json(args.run_state)
    source_bugs = _load_json(args.source_bugs) if args.source_bugs else {}
    process = _load_json(args.process) if args.process else {}

    if not baseline:
        print(f"错误: 基线 {args.baseline} 为空或不存在", file=sys.stderr)
        sys.exit(1)

    # run_result 优先级：--run-result > --run-results-dir > 空
    if args.run_result:
        run_result = _load_json(args.run_result)
    elif args.run_results_dir:
        run_result = _aggregate_run_results_dir(Path(args.run_results_dir))
    else:
        print("错误: 必须提供 --run-result 或 --run-results-dir 之一",
              file=sys.stderr)
        sys.exit(1)

    if args.format == "markdown":
        text = _render_markdown(baseline, run_state, run_result, source_bugs, process)
    else:
        text = _render_json_report(baseline, run_state, run_result, source_bugs, process)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"按文件报告已写入: {out}", file=sys.stderr)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="单测生成调度编排")
    sub = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = sub.add_parser("init", help="从基线生成 generate_process.json")
    p_init.add_argument("--baseline", required=True, help="test_cases.json 路径")
    p_init.add_argument("--output", required=True, help="generate_process.json 输出路径")
    p_init.add_argument("--max-iterations", type=int, default=5,
                        help="子 agent 最大内部迭代次数（默认 5）")
    p_init.add_argument("--shards-root", default=".test",
                        help="per-file shards 的根目录（默认 .test）")

    # batch
    p_batch = sub.add_parser("batch", help="只读查询 n 个待处理文件")
    p_batch.add_argument("--process", required=True, help="generate_process.json 路径")
    p_batch.add_argument("--baseline", required=True, help="test_cases.json 路径")
    p_batch.add_argument("--number", type=int, required=True, help="选取文件数")
    p_batch.add_argument("--stale-seconds", type=int, default=0,
                         help=">0 时，把 claimed_at 超过该秒数仍停在"
                              "\"执行中\"的任务视为 stale，一并纳入候选")

    # claim
    p_claim = sub.add_parser("claim",
                              help="原子选 N 个，标记为\"执行中\"；返回结构同 batch")
    p_claim.add_argument("--process", required=True)
    p_claim.add_argument("--baseline", required=True)
    p_claim.add_argument("--number", type=int, required=True)
    p_claim.add_argument("--stale-seconds", type=int, default=1800,
                         help="\"执行中\"超过该秒数自动回收（默认 1800=30 分钟）")

    # report
    p_report = sub.add_parser("report", help="按文件输出测试分析报告")
    p_report.add_argument("--baseline", required=True)
    p_report.add_argument("--run-state", required=True)
    p_report.add_argument("--run-result", default=None,
                          help="全量模式：单个聚合 run_result.json")
    p_report.add_argument("--run-results-dir", default=None,
                          help="并行模式：目录下每个 *.json 是 per-file shard，"
                               "自动聚合")
    p_report.add_argument("--source-bugs", default=None,
                          help="可选；source_bugs.json 路径")
    p_report.add_argument("--process", default=None,
                          help="可选；generate_process.json 路径（含子 agent 结果）")
    p_report.add_argument("--output", required=True)
    p_report.add_argument("--format", choices=["markdown", "json"], default="markdown")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "batch":
        cmd_batch(args)
    elif args.command == "claim":
        cmd_claim(args)
    elif args.command == "report":
        cmd_report(args)


if __name__ == "__main__":
    main()
