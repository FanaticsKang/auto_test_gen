#!/usr/bin/env python3
"""
dispatch.py — 单测生成流水线的调度编排脚本。

子命令：
  init              从 test_cases.json 生成 generate_process.json
  batch             只读筛选：返回 n 个待处理文件的信息，不改状态
  claim             原子选 N 个文件并标 "running"（含 claimed_at），返回与 batch 同构的信息
  prepare-shard     为指定源文件生成 task_envelope.json
  verify-artifacts  验证 sub-agent 三个产物文件是否齐全
  report            按源文件输出测试分析报告（Markdown / JSON）

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
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# 盲测模式：docstring 行为描述关键词（带词边界防止误匹配标识符）
_ORACLE_HIGH_KEYWORDS = re.compile(
    r"(\breturns?\b|\braises?\s|\byield\b|参数|返回|抛出|边界|\brange\b|\bthreshold\b|\bwhen\s)",
    re.IGNORECASE,
)


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
# 盲测模式：docstring 提取 + oracle_quality 评估
# ---------------------------------------------------------------------------

def _extract_docstring(src_lines: list, signature_line: int) -> str:
    """从签名行之后提取 docstring（三引号块）。

    signature_line 是 1-indexed（行号），src_lines 是 0-indexed 数组。
    用 signature_line 作为 0-indexed 下标恰巧等于下一行。
    扫描范围：从签名下一行到首个非空非注释非 pass/... 语句为止。
    """
    if signature_line >= len(src_lines):
        return ""
    start = signature_line  # 0-indexed，指向签名行的下一行
    # 扫到第一个非装饰器、非空行、非注释的行（可能跨多行参数列表）
    end = min(len(src_lines), start + 20)  # 最多扫 20 行，覆盖多行参数列表
    for i in range(start, end):
        stripped = src_lines[i].strip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            quote = stripped[:3]
            # 单行 docstring
            if stripped.count(quote) >= 2 and len(stripped) > 3:
                # 找第二个引号位置
                second = stripped.index(quote, 3)
                return stripped[3:second].strip()
            # 多行 docstring
            parts = [src_lines[i].lstrip()[3:]]
            for j in range(i + 1, len(src_lines)):
                line = src_lines[j]
                # 在行首或行尾找结束引号
                idx = line.find(quote)
                if idx >= 0:
                    parts.append(line[:idx])
                    break
                parts.append(line)
            return "\n".join(parts).strip()
        elif stripped and not stripped.startswith("#") and not stripped.startswith("@"):
            # 非空非注释非装饰器 → 函数体开始，没有 docstring
            break
    return ""


def _assess_oracle_quality(docstring: str) -> str:
    """评估 docstring 作为 oracle 的质量。

    Returns:
        "high" - 含行为描述（Returns/Raises/具体边界）
        "medium" - 有 docstring 但无行为描述
        "low" - 无 docstring
    """
    if not docstring:
        return "low"
    if _ORACLE_HIGH_KEYWORDS.search(docstring):
        return "high"
    # docstring 存在但无行为描述
    if len(docstring.strip()) > 10:
        return "medium"
    return "low"


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
        "heartbeat": f"{root}/heartbeats/{slug}.txt",
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
            "claim_round": 0,
            "attempt_count": 0,
            "effective_attempt_count": 0,
            "last_error_category": None,
            "last_attempt_at": None,
            "abandon_reason": None,
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


def _select_candidates(proc_files, stale_seconds, bl_files=None, shards_root=None):
    """候选 = 未创建 + stale 任务，按优先级排序。

    stale 判定优先看 heartbeat 文件 mtime，无 heartbeat 时回退到 claimed_at。
    """
    raw = [
        path for path, info in proc_files.items()
        if info.get("status") == "pending"
    ]
    if stale_seconds and stale_seconds > 0:
        now = datetime.now()
        for path, info in proc_files.items():
            if info.get("status") != "running":
                continue
            is_stale = False
            heartbeat_found = False

            # 优先看 heartbeat mtime
            if shards_root:
                slug = _slug(path)
                hb = Path(f"{shards_root.rstrip('/')}/heartbeats/{slug}.txt")
                if hb.is_file():
                    heartbeat_found = True
                    try:
                        hb_mtime = datetime.fromtimestamp(hb.stat().st_mtime)
                        if (now - hb_mtime).total_seconds() > stale_seconds:
                            is_stale = True
                    except OSError:
                        pass

            # 仅在无 heartbeat 时回退到 claimed_at
            if not is_stale and not heartbeat_found:
                claimed_at = info.get("claimed_at")
                if not claimed_at:
                    continue
                try:
                    ts = datetime.fromisoformat(claimed_at)
                except ValueError:
                    continue
                if (now - ts).total_seconds() > stale_seconds:
                    is_stale = True

            if is_stale:
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

    candidates = _select_candidates(proc_files, args.stale_seconds, bl_files, shards_root)
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

def _compute_aimd_concurrency(proc_files, max_number):
    """AIMD 节流：根据最近完成窗口的质量决定并发度。

    只看 completed/unmet（排除 abandoned），看 effective_attempt_count
    （含 stale reclaim）判断是否有重试。
    规则：
    - 最近 3 个完成的 sub-agent 中 ≥2 个 effective_attempt_count > 1 → 降到 1
    - 最近 5 个连续 completed 且 effective_attempt_count <= 1 → 恢复到 max_number
    - 默认 min(2, max_number)
    """
    recent = []
    for src_path, info in proc_files.items():
        st = info.get("status")
        if st in ("completed", "unmet"):
            recent.append(info)

    recent.sort(key=lambda x: x.get("last_attempt_at") or x.get("claimed_at") or "", reverse=True)
    recent = recent[:10]

    if not recent:
        return min(2, max_number)

    # 429 / rate-limit 立即 MD
    if any(r.get("last_error_category") == "rate_limit" for r in recent[:5]):
        return 1

    last3 = recent[:3]
    retry_count = sum(1 for r in last3 if r.get("effective_attempt_count", 0) > 1)
    if retry_count >= 2:
        return 1

    last5 = recent[:5]
    if len(last5) >= 5 and all(r.get("effective_attempt_count", 0) <= 1 for r in last5):
        return max_number

    return min(2, max_number)


def _check_circuit_break(proc_files):
    """熔断检测：最近 2 个终态文件是否连续因 exhausted_attempts 而 abandoned。

    按 last_attempt_at 排序，只看最近的终态，不累积历史 abandoned。
    返回 (should_break, reason)。
    """
    terminal = []
    for src_path, info in proc_files.items():
        if info.get("status") in _terminal_statuses():
            terminal.append((src_path, info))
    terminal.sort(key=lambda x: x[1].get("last_attempt_at")
                  or x[1].get("claimed_at") or "", reverse=True)

    if len(terminal) < 2:
        return False, None

    # 只看最近 2 个终态
    for _, info in terminal[:2]:
        if info.get("status") != "abandoned":
            return False, None
        if info.get("abandon_reason") != "exhausted_attempts":
            return False, None

    paths = [p for p, _ in terminal[:2]]
    return True, f"最近 2 个文件因尝试耗尽 abandoned: {paths}"


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

    # AIMD 节流
    max_number = args.max_number or args.number
    recommended = _compute_aimd_concurrency(proc_files, max_number)

    # 并发窗口：running 数量已达上限时不再 claim
    running_count = sum(1 for info in proc_files.values() if info.get("status") == "running")
    slots_available = max(0, recommended - running_count)
    effective_number = min(args.number, slots_available)

    # 熔断检测
    circuit_break, circuit_reason = _check_circuit_break(proc_files)

    candidates = _select_candidates(proc_files, args.stale_seconds, bl_files, shards_root)

    # 过滤 effective_attempt_count >= 3 的文件（自动 abandoned）
    abandoned_now = []
    for p in list(candidates):
        info = proc_files.get(p, {})
        if (info.get("effective_attempt_count", 0) >= 3
                and info.get("status") != "abandoned"):
            info["status"] = "abandoned"
            info["abandon_reason"] = "exhausted_attempts"
            abandoned_now.append({
                "path": p,
                "reason": f"effective_attempt_count={info['effective_attempt_count']} 无产物",
                "attempt_count": info.get("attempt_count", 0),
                "effective_attempt_count": info.get("effective_attempt_count", 0),
                "last_error_category": info.get("last_error_category"),
            })
            candidates.remove(p)

    claim_paths = candidates[:effective_number]

    now_iso = datetime.now().isoformat(timespec="seconds")
    reclaimed = []
    for p in claim_paths:
        info = proc_files.get(p, {})
        prev = info.get("status")
        is_reclaim = (prev == "running")
        info["status"] = "running"
        info["claimed_at"] = now_iso
        info["claim_round"] = info.get("claim_round", 0) + 1
        # effective_attempt_count 在每次 claim（含 stale reclaim）都 +1
        # 用于 AIMD 判断是否有重试和自动 abandoned 阈值
        info["effective_attempt_count"] = info.get("effective_attempt_count", 0) + 1
        # attempt_count 只在首次 claim 时 +1（排除 stale reclaim）
        if not is_reclaim:
            info["attempt_count"] = info.get("attempt_count", 0) + 1
        info["last_attempt_at"] = now_iso
        proc_files[p] = info
        if is_reclaim:
            reclaimed.append(p)

    # 原子写回
    if claim_paths or abandoned_now:
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
        "auto_abandoned": abandoned_now,
        "recommended_concurrency": recommended,
        "running_count": running_count,
        "circuit_break": circuit_break,
        "circuit_break_reason": circuit_reason,
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

    bl_files = baseline.get("files", {})
    cov_summary = run_result.get("summary", {}).get("coverage", {})
    cov_config = baseline.get("coverage_config", {})

    # ---- Pre-compute statistics ----
    # File status groups
    completed = []
    unmet = []
    abandoned = []
    not_started = []
    for src_path in sorted(bl_files.keys()):
        proc_info = proc_files.get(src_path, {})
        status = proc_info.get("status", "")
        if status == "completed":
            completed.append(src_path)
        elif status == "unmet":
            unmet.append(src_path)
        elif status == "abandoned":
            abandoned.append(src_path)
        else:
            not_started.append(src_path)

    # Case counts & dimension stats
    total_cases = 0
    total_passed = 0
    total_failed = 0
    total_source_bugs = 0
    total_pending = 0
    total_skipped = 0
    dim_stats = {}
    failure_classes = {}

    for src_path, rs_file in rs_files.items():
        for func_key, rs_func in rs_file.get("functions", {}).items():
            for c in rs_func.get("cases", []):
                total_cases += 1
                st = c.get("status", "")
                dim = c.get("dimension", "unknown")
                if dim not in dim_stats:
                    dim_stats[dim] = {"total": 0, "passed": 0, "failed": 0, "source_bug": 0}
                dim_stats[dim]["total"] += 1
                if st == "passed":
                    total_passed += 1
                    dim_stats[dim]["passed"] += 1
                elif st in ("failed", "failed_persistent"):
                    total_failed += 1
                    dim_stats[dim]["failed"] += 1
                    fr = c.get("failure_reason") or "unclassified"
                    failure_classes[fr] = failure_classes.get(fr, 0) + 1
                elif st == "source_bug":
                    total_source_bugs += 1
                    dim_stats[dim]["source_bug"] += 1
                    failure_classes["source_code_bug"] = failure_classes.get("source_code_bug", 0) + 1
                elif st == "pending":
                    total_pending += 1
                elif st == "skipped":
                    total_skipped += 1

    # Coverage distribution
    _ranges = ["≥95%", "90-95%", "80-90%", "60-80%", "<60%"]
    stmt_dist = {r: 0 for r in _ranges}
    branch_dist = {r: 0 for r in _ranges}
    for fcov in run_cov.values():
        sr = fcov.get("statement_rate", 0)
        if sr >= 95: stmt_dist["≥95%"] += 1
        elif sr >= 90: stmt_dist["90-95%"] += 1
        elif sr >= 80: stmt_dist["80-90%"] += 1
        elif sr >= 60: stmt_dist["60-80%"] += 1
        else: stmt_dist["<60%"] += 1
        br = fcov.get("branch_rate", 0)
        if br >= 95: branch_dist["≥95%"] += 1
        elif br >= 90: branch_dist["90-95%"] += 1
        elif br >= 80: branch_dist["80-90%"] += 1
        elif br >= 60: branch_dist["60-80%"] += 1
        else: branch_dist["<60%"] += 1

    # Iteration stats
    iter_list = []
    early_stop_count = 0
    exhausted_count = 0
    one_pass_count = 0
    max_iters_config = (process or {}).get("max_iterations", 5)
    for src_path, info in proc_files.items():
        result = info.get("result")
        if not result:
            continue
        iters = result.get("iterations_used", 0)
        iter_list.append((src_path, iters))
        if iters <= 1:
            one_pass_count += 1
        unmet_reasons = result.get("unmet_reasons", [])
        if any("无进展" in r or "hard_to_test" in r for r in unmet_reasons):
            early_stop_count += 1
        if iters >= max_iters_config and unmet_reasons:
            exhausted_count += 1
    avg_iters = round(sum(it for _, it in iter_list) / len(iter_list), 1) if iter_list else 0
    max_iter_entry = max(iter_list, key=lambda x: x[1]) if iter_list else (None, 0)

    # Uncovered code
    uncovered_files = []
    for src_path, fcov in run_cov.items():
        ml = fcov.get("missed_lines", [])
        mb = fcov.get("missed_branches", [])
        if ml or mb:
            uncovered_files.append((src_path, ml, mb))

    # ---- Render report ----
    output = []
    output.append("# 单元测试总结报告\n")
    output.append(f"- 语言: {run_result.get('language', '-')}")
    output.append(f"- 生成时间: {run_result.get('generated_at', '-')}")

    # ---- 1. 全局统计 ----
    bl_func_count = sum(
        len([fk for fk, fm in bf.get("functions", {}).items()
             if not fm.get("test_optional")])
        for bf in bl_files.values()
    )
    pass_rate = round(total_passed / total_cases * 100, 1) if total_cases else 0
    output.append(f"\n## 1. 全局统计\n")
    output.append("| 指标 | 值 |")
    output.append("|------|-----|")
    tested_files = len(completed) + len(unmet) + len(abandoned)
    output.append(f"| 源文件数 | {len(bl_files)} |")
    output.append(f"| 已测试文件数 | {tested_files} |")
    output.append(f"| 函数总数 | {bl_func_count} |")
    output.append(f"| 测试用例总数 | {total_cases} |")
    output.append(f"| 通过 | {total_passed} ({pass_rate}%) |")
    output.append(f"| 失败 | {total_failed} |")
    output.append(f"| 源码 bug | {len(source_bugs.get('bugs', []))} |")
    output.append(f"| 待跑 | {total_pending} |")
    output.append(f"| 跳过 | {total_skipped} |")

    # ---- 2. 覆盖率概览 ----
    stmt_target = cov_config.get("statement_threshold", 90)
    branch_target = cov_config.get("branch_threshold", 90)
    func_target = cov_config.get("function_threshold", 100)
    stmt_actual = cov_summary.get("statement_rate", 0)
    branch_actual = cov_summary.get("branch_rate", 0)
    func_actual = cov_summary.get("function_rate", 0)
    output.append(f"\n## 2. 覆盖率概览\n")
    output.append("| 指标 | 实际（已测试文件） | 目标 | 状态 |")
    output.append("|------|------|------|------|")
    output.append(f"| 语句覆盖率 | {stmt_actual}% | {stmt_target}% | "
                  f"{'✓' if stmt_actual >= stmt_target else '✗'} |")
    output.append(f"| 分支覆盖率 | {branch_actual}% | {branch_target}% | "
                  f"{'✓' if branch_actual >= branch_target else '✗'} |")
    output.append(f"| 函数覆盖率 | {func_actual}% | {func_target}% | "
                  f"{'✓' if func_actual >= func_target else '✗'} |")
    if len(not_started) > 0:
        output.append(f"\n> 注：覆盖率仅基于 {tested_files} 个已测试文件计算，"
                      f"{len(not_started)} 个文件尚未测试。")
    all_cov_met = (stmt_actual >= stmt_target and branch_actual >= branch_target
                   and func_actual >= func_target)
    output.append(f"\n覆盖率达标: **{'是' if all_cov_met else '否'}**")

    # 未达标原因分析
    if not all_cov_met:
        output.append(f"\n### 未达标原因\n")
        # 按指标归类：找出拉低整体覆盖率的文件
        for metric, actual, target, cov_key in [
            ("语句覆盖率", stmt_actual, stmt_target, "statement_rate"),
            ("分支覆盖率", branch_actual, branch_target, "branch_rate"),
            ("函数覆盖率", func_actual, func_target, "function_rate"),
        ]:
            if actual >= target:
                continue
            gap = round(target - actual, 1)
            # 找低于阈值的文件
            below = []
            for src_path, fcov in run_cov.items():
                rate = fcov.get(cov_key, 0)
                if rate < target:
                    below.append((src_path, rate))
            below.sort(key=lambda x: x[1])
            output.append(f"**{metric}** ({actual}% < 目标 {target}%，差 {gap}pp)：")
            if below:
                for p, r in below[:5]:
                    proc_info = proc_files.get(p, {})
                    status = proc_info.get("status", "?")
                    result = proc_info.get("result") or {}
                    obj_blocker = result.get("objective_blocker", False)
                    reason_suffix = ""
                    if status == "abandoned":
                        reason_suffix = "（已放弃）"
                    elif status == "unmet" and obj_blocker:
                        reason_suffix = "（客观原因：dead code / 不可达分支）"
                    elif status == "unmet":
                        reason_suffix = "（迭代未达标）"
                    output.append(f"- `{p}`: {r}%{reason_suffix}")
                if len(below) > 5:
                    output.append(f"- ... 还有 {len(below) - 5} 个文件低于阈值")
            else:
                output.append("- 无单文件数据（可能为聚合精度问题）")
            output.append("")

    # ---- 3. 覆盖率分布 ----
    output.append(f"\n## 3. 覆盖率分布\n")
    output.append("| 区间 | 语句覆盖率 | 分支覆盖率 |")
    output.append("|------|-----------|-----------|")
    for rng in _ranges:
        output.append(f"| {rng} | {stmt_dist.get(rng, 0)} 个文件 | "
                      f"{branch_dist.get(rng, 0)} 个文件 |")

    # ---- 4. 文件完成状态 ----
    output.append(f"\n## 4. 文件完成状态\n")
    output.append(f"- **已达标**: {len(completed)}/{len(bl_files)} 个文件")
    output.append(f"- **未达标**: {len(unmet)} 个文件")
    output.append(f"- **已放弃**: {len(abandoned)} 个文件")
    if not_started:
        output.append(f"- **未开始**: {len(not_started)} 个文件")

    if completed:
        output.append(f"\n### 已达标文件\n")
        for p in completed:
            iters = proc_files[p].get("result", {}).get("iterations_used", "-")
            output.append(f"- `{p}` — {iters} 轮迭代")

    if unmet:
        output.append(f"\n### 未达标文件\n")
        for p in unmet:
            reasons = proc_files[p].get("result", {}).get("unmet_reasons", [])
            output.append(f"- `{p}` — {'; '.join(reasons[:3])}")

    if abandoned:
        output.append(f"\n### 已放弃文件\n")
        for p in abandoned:
            output.append(f"- `{p}`")

    # ---- 5. 维度覆盖 ----
    # ---- 5. 维度覆盖 ----
    output.append(f"\n## 5. 测试维度覆盖\n")
    if dim_stats:
        output.append("| 维度 | 用例数 | 通过 | 失败 | 源码 bug | 待跑 | 通过率 |")
        output.append("|------|--------|------|------|---------|------|--------|")
        for dim in sorted(dim_stats.keys()):
            ds = dim_stats[dim]
            executed = ds["passed"] + ds["failed"] + ds["source_bug"]
            rate = round(ds["passed"] / executed * 100, 1) if executed else 0
            pending_dim = ds["total"] - ds["passed"] - ds["failed"] - ds["source_bug"]
            output.append(f"| {dim} | {ds['total']} | {ds['passed']} | {ds['failed']} "
                          f"| {ds['source_bug']} | {pending_dim} | {rate}% |")
    else:
        output.append("*无维度数据*")

    # ---- 6. 迭代效率 ----
    output.append(f"\n## 6. 迭代效率\n")
    if iter_list:
        output.append(f"- 平均迭代次数: {avg_iters}")
        if max_iter_entry[0]:
            output.append(f"- 最多迭代: `{max_iter_entry[0]}` ({max_iter_entry[1]} 轮)")
        output.append(f"- 一次通过: {one_pass_count} 个文件")
        if early_stop_count:
            output.append(f"- 提前终止（早停/难测函数）: {early_stop_count} 个文件")
        if exhausted_count:
            output.append(f"- 迭代耗尽: {exhausted_count} 个文件")
    else:
        output.append("*无迭代数据*")

    # ---- 7. 失败分类汇总 ----
    output.append(f"\n## 7. 失败分类汇总\n")
    if failure_classes:
        total_failures = total_failed + total_source_bugs
        output.append(f"共 {total_failures} 个失败/bug 用例：\n")
        output.append("| 分类 | 数量 | 占比 |")
        output.append("|------|------|------|")
        for cls in sorted(failure_classes.keys()):
            cnt = failure_classes[cls]
            pct = round(cnt / total_failures * 100, 1) if total_failures else 0
            output.append(f"| {cls} | {cnt} | {pct}% |")
    else:
        output.append("*无失败用例*")

    # ---- 8. 源代码疑似 bug ----
    real_bugs = [b for b in source_bugs.get("bugs", [])
                 if b.get("function") != "NONE" or b.get("case_id") != "NONE"]
    total_bugs = len(real_bugs)
    output.append(f"\n## 8. 源代码疑似 bug\n")
    if total_bugs:
        output.append(f"共 {total_bugs} 个：\n")
        for b in real_bugs:
            output.append(f"- **{b.get('function', '-')}** "
                          f"({b.get('file', '-')}, "
                          f"case `{b.get('case_id', '-')}`): {b.get('reason', '')}")
    else:
        output.append("*无*")

    # ---- 9. 文件级详细分析 ----
    output.append("\n---\n")
    output.append("\n## 9. 文件级详细分析\n")

    # 文件详细部分：优先展示已完成的文件
    bl_keys = list(baseline.get("files", {}).keys())
    def _file_sort_key(p):
        proc_info = proc_files.get(p, {})
        status = proc_info.get("status", "")
        order = {"completed": 0, "unmet": 1, "abandoned": 2}
        return order.get(status, 3)
    bl_keys.sort(key=lambda p: (_file_sort_key(p), p))

    for src_path in bl_keys:
        bl_file = baseline["files"][src_path]
        rs_file = rs_files.get(src_path, {})
        file_cov = run_cov.get(src_path, {})
        file_bugs = bugs_by_file.get(src_path, [])
        proc_info = proc_files.get(src_path, {})
        file_status = proc_info.get("status", "")

        # 未开始/未测试的文件：只显示一行概要，不展开函数
        if file_status not in ("completed", "unmet", "abandoned"):
            n_funcs = len([fk for fk, fm in bl_file.get("functions", {}).items()
                           if not fm.get("test_optional")])
            output.append(f"\n## `{src_path}`\n")
            output.append(f"- 测试文件: `{bl_file.get('test_path', '-')}`")
            output.append(f"- 状态: 未测试 | 函数数: {n_funcs}")
            continue

        output.append(f"\n## `{src_path}`\n")
        output.append(f"- 测试文件: `{bl_file.get('test_path', '-')}`")

        # 调度结果
        if proc_info.get("result"):
            result = proc_info["result"]
            unmet_reasons = result.get("unmet_reasons", [])
            dead = result.get("dead_code", False)
            if unmet_reasons:
                output.append(f"- **未达标项**: {'; '.join(unmet_reasons)}")
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

        file_bugs = [b for b in file_bugs
                     if b.get("function") != "NONE" or b.get("case_id") != "NONE"]
        if file_bugs:
            output.append(f"\n### 源代码疑似 bug ({len(file_bugs)})\n")
            for b in file_bugs:
                output.append(
                    f"- **{b.get('function', '-')}** "
                    f"(case `{b.get('case_id', '-')}`, "
                    f"复现 {b.get('occurrence_count', 1)} 次): {b.get('reason', '')}"
                )

    # ---- 10. 未覆盖代码汇总 ----
    if uncovered_files:
        output.append(f"\n## 10. 未覆盖代码汇总\n")
        output.append("| 文件 | 未覆盖行数 | 未覆盖分支数 | 未覆盖行 | 未覆盖分支 |")
        output.append("|------|-----------|------------|---------|-----------|")
        for src_path, ml, mb in uncovered_files:
            ml_str = str(ml[:15]) + (" ..." if len(ml) > 15 else "")
            mb_str = str(mb[:10]) + (" ..." if len(mb) > 10 else "")
            output.append(f"| `{src_path}` | {len(ml)} | {len(mb)} | {ml_str} | {mb_str} |")

    # ---- 11. 建议 ----
    recommendations = []
    if unmet:
        recommendations.append(f"{len(unmet)} 个文件未达标，建议针对未覆盖分支补充测试")
    source_bug_count = len([b for b in source_bugs.get("bugs", [])
                            if b.get("function") != "NONE" or b.get("case_id") != "NONE"])
    if source_bug_count:
        recommendations.append(
            f"发现 {source_bug_count} 个源码疑似 bug，建议人工确认并修复源码")
    for src_path in unmet:
        result = proc_files.get(src_path, {}).get("result", {})
        if result.get("dead_code"):
            locs = result.get("dead_code_locations", [])
            recommendations.append(
                f"`{src_path}` 存在 dead code（{'; '.join(locs[:3])}），建议确认是否移除")
    for src_path in abandoned:
        proc_info = proc_files.get(src_path, {})
        reason = proc_info.get("abandon_reason", "")
        if reason == "all_source_bugs":
            recommendations.append(f"`{src_path}` 因所有 gap 被 source_bug 阻塞已放弃，建议修复源码后重新测试")
        else:
            recommendations.append(f"`{src_path}` 因尝试耗尽已放弃（{reason}），建议检查后重新测试")
    if recommendations:
        output.append(f"\n## 11. 建议\n")
        for i, rec in enumerate(recommendations, 1):
            output.append(f"{i}. {rec}")

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

def cmd_verify_artifacts(args):
    """验证 sub-agent 三个产物文件是否齐全，并原子回写 generate_process.json。"""
    process_path = Path(args.process)
    process = _load_json(process_path)
    if not process:
        print(f"错误: 调度状态 {args.process} 不存在或为空", file=sys.stderr)
        sys.exit(1)

    shards_root = process.get("shards_root", ".test")
    proc_files = process.get("files", {})
    src_path = args.file

    if src_path not in proc_files:
        print(f"错误: {src_path} 不在调度状态中", file=sys.stderr)
        sys.exit(1)

    info = proc_files[src_path]
    slug = _slug(src_path)
    root = shards_root.rstrip("/")

    artifacts = {
        "run_result": Path(f"{root}/run_results/{slug}.json"),
        "state_shard": Path(f"{root}/state_shards/{slug}.json"),
        "bug_shard": Path(f"{root}/bug_shards/{slug}.json"),
    }
    missing = []
    invalid = []
    for name, p in artifacts.items():
        if not p.is_file():
            missing.append(name)
        elif p.stat().st_size == 0 and name != "bug_shard":
            # bug_shard 允许空文件；run_result / state_shard 不允许
            missing.append(f"{name}(empty)")
        else:
            try:
                json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                invalid.append(name)

    all_problems = missing + invalid
    if all_problems:
        # 产物缺失或无效 → 回退
        prev_status = info.get("status", "")
        info["status"] = args.on_missing
        info["last_error_category"] = "no_artifact"
        if args.on_missing == "abandoned":
            info["abandon_reason"] = "exhausted_attempts"
        _write_json_atomic(process, process_path)
        print(json.dumps({
            "verified": False,
            "source_path": src_path,
            "missing_artifacts": missing,
            "invalid_artifacts": invalid,
            "previous_status": prev_status,
            "new_status": args.on_missing,
            "last_error_category": "no_artifact",
        }, ensure_ascii=False))
    else:
        # 产物齐全 → attempt_count 归零（成功完成）
        info["attempt_count"] = 0
        info["effective_attempt_count"] = 0
        info["last_error_category"] = None
        _write_json_atomic(process, process_path)
        print(json.dumps({
            "verified": True,
            "source_path": src_path,
            "artifacts": {name: str(p) for name, p in artifacts.items()},
            "attempt_count_reset": True,
        }, ensure_ascii=False))


# ---------------------------------------------------------------------------
# prepare-shard: 一次性产出 task_envelope
# ---------------------------------------------------------------------------

def cmd_prepare_shard(args):
    """为指定源文件生成 task_envelope.json，包含 sub-agent 所需的全部上下文。

    sub-agent 读取这一个文件就能开始工作，无需额外 Read。
    """
    process = _load_json(args.process)
    baseline = _load_json(args.baseline)

    if not process:
        print(f"错误: 调度状态 {args.process} 不存在或为空", file=sys.stderr)
        sys.exit(1)
    if not baseline:
        print(f"错误: 基线 {args.baseline} 不存在或为空", file=sys.stderr)
        sys.exit(1)

    src_path = args.file
    proc_files = process.get("files", {})
    if src_path not in proc_files:
        print(f"错误: {src_path} 不在调度状态中", file=sys.stderr)
        sys.exit(1)

    cov_config, max_iter, shards_root = _resolve_common(process, baseline)
    info = proc_files[src_path]
    bl_file = baseline.get("files", {}).get(src_path, {})
    paths = _shard_paths(shards_root, src_path)
    slug = paths["slug"]

    # 收集函数信息（排除 test_optional）
    functions = {}
    for func_key, fmeta in bl_file.get("functions", {}).items():
        if fmeta.get("test_optional"):
            continue
        functions[func_key] = {
            "dimensions": fmeta.get("dimensions", []),
            "line_range": fmeta.get("line_range", []),
            "signature": fmeta.get("signature", ""),
            "mocks_needed": fmeta.get("mocks_needed", []),
        }

    # 读取 run_state shard（获取已有 cases）
    state_shard_path = Path(paths["state_shard"])
    existing_cases = {}
    if state_shard_path.is_file():
        state_data = json.loads(state_shard_path.read_text(encoding="utf-8"))
        for func_key, func_data in state_data.get("files", {}).get(src_path, {}).get("functions", {}).items():
            cases = func_data.get("cases", [])
            if cases:
                existing_cases[func_key] = [
                    {"id": c.get("id"), "dimension": c.get("dimension"),
                     "status": c.get("status", "pending"),
                     "failure_reason": c.get("failure_reason")}
                    for c in cases
                ]

    # 读取 run_result shard（获取覆盖率）
    run_result_path = Path(paths["run_result"])
    coverage = {}
    coverage_summary = {}
    if run_result_path.is_file():
        rr = json.loads(run_result_path.read_text(encoding="utf-8"))
        coverage = rr.get("coverage", {}).get(src_path, {})
        coverage_summary = rr.get("summary", {}).get("coverage", {})

    # 读取 verdicts（如果存在）
    verdicts = []
    verdicts_path = Path(f"{shards_root.rstrip('/')}/verdicts/{slug}.json")
    if verdicts_path.is_file():
        vdata = json.loads(verdicts_path.read_text(encoding="utf-8"))
        verdicts = vdata.get("verdicts", [])

    # 读取 next_action（如果存在）
    next_action_path = Path(f"{shards_root.rstrip('/')}/next_actions/{slug}.json")
    next_action = None
    if next_action_path.is_file():
        next_action = json.loads(next_action_path.read_text(encoding="utf-8"))

    # O14: 预切 source_snippet（每个函数 ±20 行）
    # 盲测模式：round 1 只给签名 + docstring
    source_snippets = {}
    oracle_quality_map = {}
    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path(".")
    src_file = repo_root / src_path
    src_lines = []
    if src_file.is_file():
        try:
            src_lines = src_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            pass

    use_blind = getattr(args, "blind", False) and args.round <= 1

    if src_lines:
        # 先评估所有函数的 oracle_quality（盲测模式才需要）
        if use_blind:
            for func_key, fmeta in bl_file.get("functions", {}).items():
                if fmeta.get("test_optional"):
                    continue
                lr = fmeta.get("line_range", [0, 0])
                docstring = _extract_docstring(src_lines, lr[0])
                oq = _assess_oracle_quality(docstring)
                oracle_quality_map[func_key] = oq

        pad = 20
        for func_key, fmeta in bl_file.get("functions", {}).items():
            if fmeta.get("test_optional"):
                continue
            lr = fmeta.get("line_range", [0, 0])
            oq = oracle_quality_map.get(func_key)

            # 盲测模式 + oracle_quality 为 high/medium → 只给签名+docstring
            if use_blind and oq in ("high", "medium"):
                sig_line_idx = lr[0] - 1  # 1-indexed → 0-indexed
                if 0 <= sig_line_idx < len(src_lines):
                    sig_text = src_lines[sig_line_idx]
                    docstring = _extract_docstring(src_lines, lr[0])
                    snippet_text = sig_text
                    if docstring:
                        snippet_text += "\n\n" + docstring
                    source_snippets[func_key] = {
                        "start": lr[0],
                        "end": lr[0],
                        "text": snippet_text,
                        "docstring": docstring,
                        "mode": "blind" if oq == "high" else "narrowed",
                    }
            else:
                # 正常模式 或 blind+low：±20 行完整实现
                start = max(1, lr[0] - pad)
                end = min(len(src_lines), lr[1] + pad)
                source_snippets[func_key] = {
                    "start": start,
                    "end": end,
                    "text": "\n".join(
                        f"{i:>5}  {src_lines[i - 1]}"
                        for i in range(start, end + 1)
                    ),
                    "mode": "sighted",
                }

    # 构建 task_envelope
    envelope = {
        "shard_slug": slug,
        "source_path": src_path,
        "test_path": info.get("test_path", bl_file.get("test_path", "")),
        "file_md5": info.get("file_md5", ""),
        "round": args.round,
        "scope_sources": [src_path],
        "functions": functions,
        "existing_cases": existing_cases,
        "coverage": coverage,
        "coverage_summary": coverage_summary,
        "coverage_config": cov_config,
        "source_snippets": source_snippets,
        "oracle_quality": oracle_quality_map if oracle_quality_map else None,
        "blind_mode": use_blind,
        "verdicts": verdicts,
        "next_action": next_action,
        "paths": paths,
        "budgets": {
            "max_iterations": max_iter,
            "max_fix_attempts_per_case": 2,
        },
    }

    # 写入输出
    out_path = Path(args.output)
    _write_json_atomic(envelope, out_path)

    n_funcs = len(functions)
    n_cases = sum(len(c) for c in existing_cases.values())
    blind_tag = " [盲测]" if use_blind else ""
    print(
        f"task_envelope: {out_path}\n"
        f"  文件: {src_path}{blind_tag}\n"
        f"  函数: {n_funcs}, 已有 case: {n_cases}\n"
        f"  verdicts: {len(verdicts)}, next_action: {next_action.get('action') if next_action else '-'}",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# verify-repro: 验证复现脚本（Phase 5）
# ---------------------------------------------------------------------------

def cmd_verify_repro(args):
    """扫描 repro 目录下的 .py 脚本，逐个运行，输出验证结果。"""
    repro_dir = Path(args.repro_dir)
    if not repro_dir.is_dir():
        print(f"错误: 复现脚本目录 {repro_dir} 不存在", file=sys.stderr)
        sys.exit(1)

    scripts = sorted(repro_dir.glob("*.py"))
    if not scripts:
        print(f"复现脚本目录为空: {repro_dir}", file=sys.stderr)

    results = []
    for script in scripts:
        try:
            proc = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True, text=True, timeout=30,
            )
            results.append({
                "script": script.name,
                "return_code": proc.returncode,
                "reproducible": proc.returncode != 0,
                "stdout": proc.stdout[:500] if proc.stdout else "",
                "stderr": proc.stderr[:500] if proc.stderr else "",
            })
        except subprocess.TimeoutExpired:
            results.append({
                "script": script.name,
                "return_code": -1,
                "reproducible": False,
                "error": "timeout (30s)",
            })
        except Exception as e:
            results.append({
                "script": script.name,
                "return_code": -1,
                "reproducible": False,
                "error": str(e),
            })

    output = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total_scripts": len(results),
        "reproducible": sum(1 for r in results if r.get("reproducible")),
        "results": results,
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(output, out_path)

    n_repro = output["reproducible"]
    print(
        f"复现验证完成: {n_repro}/{len(results)} 可复现 → {out_path}",
        file=sys.stderr,
    )


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
    p_claim.add_argument("--max-number", type=int, default=None,
                         help="AIMD 允许的最大并发度（默认等于 --number）")
    p_claim.add_argument("--stale-seconds", type=int, default=600,
                         help="\"running\"超过该秒数自动回收（默认 600=10 分钟）")

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

    # verify-artifacts
    p_va = sub.add_parser("verify-artifacts",
                          help="验证 sub-agent 三个产物文件是否齐全，回写状态")
    p_va.add_argument("--process", required=True, help="generate_process.json 路径")
    p_va.add_argument("--file", required=True, help="源文件路径（key in process.files）")
    p_va.add_argument("--on-missing", choices=["pending", "abandoned"], default="pending",
                      help="产物缺失时回退到的状态（默认 pending）")

    # prepare-shard
    p_ps = sub.add_parser("prepare-shard",
                          help="为指定文件生成 task_envelope.json")
    p_ps.add_argument("--process", required=True, help="generate_process.json 路径")
    p_ps.add_argument("--baseline", required=True, help="test_cases.json 路径")
    p_ps.add_argument("--file", required=True, help="源文件路径")
    p_ps.add_argument("--round", type=int, default=1, help="当前轮数")
    p_ps.add_argument("--repo-root", default=".", help="仓库根目录")
    p_ps.add_argument("--output", required=True, help="task_envelope.json 输出路径")
    p_ps.add_argument("--blind", action="store_true", default=False,
                       help="盲测模式：round 1 的 source_snippets 只含签名+docstring")

    # verify-repro（Phase 5）
    p_vr = sub.add_parser("verify-repro",
                          help="验证复现脚本可独立运行")
    p_vr.add_argument("--repro-dir", required=True,
                      help="复现脚本目录（.test/repro）")
    p_vr.add_argument("--output", required=True,
                      help="验证结果输出路径")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "batch":
        cmd_batch(args)
    elif args.command == "claim":
        cmd_claim(args)
    elif args.command == "report":
        cmd_report(args)
    elif args.command == "verify-artifacts":
        cmd_verify_artifacts(args)
    elif args.command == "prepare-shard":
        cmd_prepare_shard(args)
    elif args.command == "verify-repro":
        cmd_verify_repro(args)


if __name__ == "__main__":
    main()
