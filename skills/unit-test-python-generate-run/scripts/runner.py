#!/usr/bin/env python3
"""
runner.py — 执行 Python 测试并采集覆盖率，输出统一结构的 run_result.json。

子命令：
  run   执行测试 + 采集覆盖率

本脚本不修改测试代码，不判断失败原因，不重跑。

Sub-agent 并行场景下，用 --test-file 和 --scope-sources 把作用域限制到
单个源文件 + 对应测试文件；--output 指向 per-file shard 路径（例如
.test/run_results/<slug>.json），避免多个 sub-agent 互相覆盖结果。

用法（单文件 + fix 快速重跑）：
  python runner.py run --language python --repo-root . \
    --test-file test/generated_unit/core/test_parser.py \
    --source-dirs . --scope-sources core/parser.py \
    --baseline test/generated_unit/test_cases.json \
    --no-coverage --only-cases functional_01,boundary_02 \
    --output .test/run_results/core__parser_py.json

return_code 语义：
  0 = 全部通过
  1 = 有测试失败/错误
  2 = 覆盖率工具问题
  3 = 结果解析错误
  4 = 环境错误（pytest 未安装等）
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

CASE_ID_PATTERN = re.compile(r"#\s*CASE_ID\s*:\s*([A-Za-z0-9_\-]+)")

# return_code 语义
RC_ALL_PASS = 0
RC_TESTS_FAILED = 1
RC_COVERAGE_ERROR = 2
RC_PARSE_ERROR = 3
RC_ENV_ERROR = 4
RC_NO_TESTS = 5


# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------

def _md5_file(path: Path) -> str:
    if not path.is_file():
        return ""
    return hashlib.md5(path.read_bytes()).hexdigest()


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


def _run(cmd, cwd=None, env=None, capture=True):
    kwargs = dict(cwd=cwd, env=env, shell=isinstance(cmd, str))
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc = subprocess.run(cmd, **kwargs)
    out = proc.stdout.decode("utf-8", errors="replace") if capture and proc.stdout else ""
    err = proc.stderr.decode("utf-8", errors="replace") if capture and proc.stderr else ""
    return proc.returncode, out, err


def _count_tests_in_file(test_file: Path) -> int:
    """快速统计 py 文件里的 test 函数数量，不解析完整 AST。"""
    if not test_file.is_file():
        return 0
    text = test_file.read_text(encoding="utf-8", errors="replace")
    return len(re.findall(r"^\s*def\s+test_", text, re.MULTILINE))


# ---------------------------------------------------------------------------
# O15: tool_status 缓存
# ---------------------------------------------------------------------------

def _tool_cache_path(repo_root: Path) -> Path:
    return repo_root / ".test" / "_tool_cache.json"


def _compute_tool_status(py: str) -> dict:
    """检测 pytest / pytest-cov / coverage / xdist 是否可用。"""
    status = {"pytest": False, "pytest_cov": False, "coverage_json": False, "xdist": False}
    rc, _, _ = _run([py, "-c", "import pytest"])
    status["pytest"] = rc == 0
    rc, _, _ = _run([py, "-c", "import pytest_cov"])
    status["pytest_cov"] = rc == 0
    rc, _, _ = _run([py, "-c", "import coverage"])
    status["coverage_json"] = rc == 0
    rc, _, _ = _run([py, "-c", "import xdist"])
    status["xdist"] = rc == 0
    return status


def _get_tool_status(py: str, repo_root: Path) -> dict:
    """带缓存的 tool_status 检测。缓存基于 venv python 路径的 md5，有效期 1 小时。"""
    cache_path = _tool_cache_path(repo_root)
    cache_key = hashlib.md5(py.encode()).hexdigest()

    if cache_path.is_file():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            entry = cache.get(cache_key, {})
            if entry.get("status") and time.time() - entry.get("ts", 0) < 3600:
                return entry["status"]
        except Exception:
            pass

    status = _compute_tool_status(py)

    # 写入缓存（合并已有缓存）
    cache = {}
    if cache_path.is_file():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    cache[cache_key] = {"status": status, "ts": time.time()}
    _write_json_atomic(cache, cache_path)
    return status


# ---------------------------------------------------------------------------
# CASE_ID 映射
# ---------------------------------------------------------------------------

def _parse_case_id_map(tests_dir: Path) -> dict:
    mapping = {}
    for root, _, files in os.walk(tests_dir):
        for fname in files:
            fpath = Path(root) / fname
            if fpath.suffix.lower() != ".py":
                continue
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            lines = text.splitlines()
            pending_case = None
            for i, line in enumerate(lines):
                m = CASE_ID_PATTERN.search(line)
                if m:
                    pending_case = m.group(1)
                    continue
                if pending_case is None:
                    continue

                match = re.match(r"\s*def\s+(test_[A-Za-z0-9_]+)\s*\(", line)
                if match:
                    mapping[(str(fpath), match.group(1))] = pending_case
                    pending_case = None
                    continue

                stripped = line.strip()
                if stripped and not stripped.startswith(("#",)):
                    if not re.match(r"\s*(def|@)", stripped):
                        pending_case = None

    return mapping


def _check_baseline_md5(baseline_path: Path, repo_root: Path):
    if not baseline_path.is_file():
        return []
    try:
        with open(baseline_path, "r", encoding="utf-8") as f:
            baseline = json.load(f)
    except Exception:
        return []

    drifts = []
    for src_path, finfo in baseline.get("files", {}).items():
        expected = finfo.get("file_md5", "")
        actual = _md5_file(repo_root / src_path)
        if expected and actual and expected != actual:
            drifts.append({"path": src_path, "expected_md5": expected, "actual_md5": actual})
    return drifts


# ---------------------------------------------------------------------------
# JUnit XML 解析
# ---------------------------------------------------------------------------

def _parse_junit_xml(xml_path: Path) -> list:
    if not xml_path.is_file():
        return []
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as e:
        print(f"[parse] 警告: 无法解析 junit xml {xml_path}: {e}", file=sys.stderr)
        return []

    root = tree.getroot()
    tests = []
    suites = root.findall(".//testsuite") or [root]
    for suite in suites:
        for tc in suite.findall("testcase"):
            entry = {
                "classname": tc.get("classname", ""),
                "name": tc.get("name", ""),
                "duration_s": float(tc.get("time", "0") or 0),
                "status": "passed",
                "failure_type": None,
                "traceback": None,
                "test_file": "",
            }
            failure = tc.find("failure")
            error = tc.find("error")
            skipped = tc.find("skipped")
            if failure is not None:
                entry["status"] = "failed"
                entry["failure_type"] = failure.get("type", "AssertionError")
                entry["traceback"] = failure.text or failure.get("message", "")
            elif error is not None:
                entry["status"] = "error"
                entry["failure_type"] = error.get("type", "Error")
                entry["traceback"] = error.text or error.get("message", "")
            elif skipped is not None:
                entry["status"] = "skipped"
                entry["traceback"] = skipped.text or skipped.get("message", "")
            tests.append(entry)
    return tests


# ---------------------------------------------------------------------------
# Python: pytest + coverage.py
# ---------------------------------------------------------------------------

def _run_python(args, baseline):
    repo_root = Path(args.repo_root).resolve()
    # 单文件模式：只跑 --test-file；否则按 --tests 目录跑
    if args.test_file:
        pytest_target = Path(args.test_file).resolve()
        tests_scan_dir = pytest_target.parent if pytest_target.is_file() else pytest_target
    else:
        pytest_target = Path(args.tests).resolve()
        tests_scan_dir = pytest_target

    py = sys.executable or "python"

    # O15: 带缓存的 tool_status 检测
    tool_status = _get_tool_status(py, repo_root)

    # O13: 阶段化 stderr
    print("[tool-check] pytest={}, pytest-cov={}, coverage={}, xdist={}".format(
        "ok" if tool_status["pytest"] else "MISSING",
        "ok" if tool_status["pytest_cov"] else "MISSING",
        "ok" if tool_status["coverage_json"] else "MISSING",
        "ok" if tool_status["xdist"] else "MISSING",
    ), file=sys.stderr)

    if not tool_status["xdist"]:
        print("[tool-check] pytest-xdist 未安装，降级为串行执行", file=sys.stderr)

    if not tool_status["pytest"]:
        return {
            "language": "python",
            "error": "pytest 未安装，请 pip install pytest pytest-cov coverage",
            "tool_status": tool_status,
            "return_code": RC_ENV_ERROR,
        }

    want_coverage = not args.no_coverage

    with tempfile.TemporaryDirectory(prefix="autogen_run_") as tmpdir:
        tmp = Path(tmpdir)
        junit_xml = tmp / "junit.xml"
        cov_data = tmp / ".coverage"
        cov_json = tmp / "coverage.json"

        # O3: --only-cases → pytest -k <keyword>
        k_filter = None
        if args.only_cases:
            case_ids = [c.strip() for c in args.only_cases.split(",") if c.strip()]
            only_case_map = _parse_case_id_map(tests_scan_dir)
            reverse_map = {}
            for (fpath, tname), cid in only_case_map.items():
                reverse_map.setdefault(cid, set()).add(tname)
            test_names = set()
            unresolved = []
            for cid in case_ids:
                if cid in reverse_map:
                    test_names.update(reverse_map[cid])
                else:
                    unresolved.append(cid)
            if unresolved:
                print(f"[parse] 警告: --only-cases 未找到: {unresolved}", file=sys.stderr)
            if test_names:
                k_filter = " or ".join(sorted(test_names))

        cmd = [
            py, "-m", "pytest",
            str(pytest_target),
            f"--junit-xml={junit_xml}",
            "-q", "--no-header", "--tb=short",
        ]
        if k_filter:
            cmd.extend(["-k", k_filter])

        # O5: xdist 阈值化
        use_xdist = False
        if tool_status["xdist"] and not args.no_parallel:
            xdist_min = args.xdist_min_tests
            # 快速统计测试数（仅在单文件模式下有效）
            test_count = 0
            if args.test_file:
                tf = Path(args.test_file)
                if tf.is_absolute():
                    tf = tf.relative_to(repo_root) if tf.is_relative_to(repo_root) else tf
                test_count = _count_tests_in_file(Path(args.test_file) if Path(args.test_file).is_absolute() else repo_root / args.test_file)
            if test_count >= xdist_min:
                n_workers = args.xdist_workers or "logical"
                cmd.extend(["-n", str(n_workers)])
                use_xdist = True
            else:
                print(f"[tool-check] 测试数 {test_count} < xdist-min-tests {xdist_min}，串行执行", file=sys.stderr)

        env = dict(os.environ)
        if want_coverage and tool_status["pytest_cov"] and tool_status["coverage_json"]:
            source_dirs = (args.source_dirs or ".").split(",")
            for sd in source_dirs:
                sd = sd.strip()
                if sd:
                    cmd.append(f"--cov={sd}")
            cmd.extend([
                "--cov-branch",
                f"--cov-report=json:{cov_json}",
                "--cov-report=",
            ])
            if args.cov_append:
                cmd.append("--cov-append")
            env["COVERAGE_FILE"] = str(cov_data)

            # O2: cov-append 模式 — 复制已有 .coverage 到临时目录
            if args.cov_append and args.cov_file:
                cov_src = Path(args.cov_file)
                if cov_src.is_file():
                    shutil.copy2(str(cov_src), str(cov_data))

        print(f"[pytest] $ {' '.join(cmd)}", file=sys.stderr)
        rc, out, err = _run(cmd, cwd=str(repo_root), env=env)
        print(out, file=sys.stderr)
        if err:
            print(err, file=sys.stderr)

        tests = _parse_junit_xml(junit_xml)
        case_map = _parse_case_id_map(tests_scan_dir)
        tests = _attach_case_ids_python(tests, case_map, repo_root)

        # O13: return_code 语义化
        if rc == 5:
            # pytest rc=5: no tests collected
            semantic_rc = RC_NO_TESTS
        elif rc != 0 and rc != 1:
            # pytest 返回非 0/1/5，可能是内部错误
            semantic_rc = RC_PARSE_ERROR
        elif any(t["status"] in ("failed", "error") for t in tests):
            semantic_rc = RC_TESTS_FAILED
        else:
            semantic_rc = RC_ALL_PASS

        coverage = {}
        coverage_summary = {
            "statement_rate": 0.0, "branch_rate": 0.0, "function_rate": 0.0,
            "covered_statements": 0, "total_statements": 0,
            "covered_branches": 0, "total_branches": 0,
            "covered_functions": 0, "total_functions": 0,
        }
        if want_coverage and cov_json.is_file():
            try:
                coverage, coverage_summary = _parse_coverage_json(cov_json, baseline, repo_root)
                coverage, coverage_summary = _apply_scope(
                    coverage, coverage_summary, args.scope_sources
                )
            except Exception as e:
                print(f"[coverage] 解析覆盖率失败: {e}", file=sys.stderr)
                semantic_rc = RC_COVERAGE_ERROR
        elif want_coverage and not cov_json.is_file():
            print("[coverage] 覆盖率 JSON 未生成", file=sys.stderr)
            if semantic_rc == RC_ALL_PASS:
                semantic_rc = RC_COVERAGE_ERROR

        summary = _summarize_tests(tests)
        summary["coverage"] = coverage_summary

        # O20: case_id_index — O(1) 查找
        case_id_index = {}
        for i, t in enumerate(tests):
            cid = t.get("case_id")
            if cid:
                case_id_index[cid] = i

        print(
            f"[summary] {summary.get('passed', 0)}/{summary.get('total_tests', 0)} 通过 "
            f"(failed={summary.get('failed', 0)}, error={summary.get('errors', 0)})",
            file=sys.stderr,
        )
        if want_coverage:
            print(
                f"[coverage] stmt={coverage_summary.get('statement_rate', 0)}%, "
                f"branch={coverage_summary.get('branch_rate', 0)}%, "
                f"func={coverage_summary.get('function_rate', 0)}%",
                file=sys.stderr,
            )

        # O2: cov-append 模式 — 写回 .coverage 到 shard 路径
        if args.cov_append and args.cov_file and cov_data.is_file():
            cov_dst = Path(args.cov_file)
            cov_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(cov_data), str(cov_dst))

    return {
        "language": "python",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "return_code": semantic_rc,
        "tool_status": tool_status,
        "scope_sources": _parse_scope(args.scope_sources),
        "summary": summary,
        "tests": tests,
        "coverage": coverage,
        "case_id_index": case_id_index,
    }


def _attach_case_ids_python(tests, case_map, repo_root):
    for t in tests:
        classname = t["classname"]
        name = t["name"]
        guessed_file = None
        if classname:
            rel = classname.replace(".", "/") + ".py"
            p = repo_root / rel
            if p.is_file():
                guessed_file = str(p)
        if guessed_file:
            t["test_file"] = str(Path(guessed_file).relative_to(repo_root))
            t["case_id"] = case_map.get((guessed_file, name))
        else:
            t["case_id"] = None
    return tests


def _parse_coverage_json(cov_json, baseline, repo_root):
    with open(cov_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    per_file = {}
    total_stmts = total_branches = total_funcs = 0
    cov_stmts = cov_branches = cov_funcs = 0
    baseline_files = (baseline or {}).get("files", {})

    for abs_path, finfo in data.get("files", {}).items():
        try:
            rel_path = str(Path(abs_path).resolve().relative_to(repo_root))
        except ValueError:
            rel_path = abs_path

        s_total = finfo["summary"].get("num_statements", 0)
        s_covered = finfo["summary"].get("covered_lines", 0)
        b_total = finfo["summary"].get("num_branches", 0) or 0
        b_covered = finfo["summary"].get("covered_branches", 0) or 0
        missed_lines = finfo.get("missing_lines", []) or []
        missed_branches = [tuple(x) for x in finfo.get("missing_branches", []) or []]

        file_cov_funcs = 0
        file_total_funcs = 0
        func_coverage = {}
        baseline_file = baseline_files.get(rel_path, {})
        for func_key, fmeta in baseline_file.get("functions", {}).items():
            line_range = fmeta.get("line_range", [0, 0])
            start, end = line_range[0], line_range[1]
            func_lines = set(finfo.get("executed_lines", []) or []) | set(missed_lines)
            total_in_func = len([ln for ln in func_lines if start <= ln <= end])
            missed_in_func = [ln for ln in missed_lines if start <= ln <= end]
            missed_br_in_func = [b for b in missed_branches if start <= b[0] <= end]
            covered_in_func = total_in_func - len(missed_in_func)
            rate = (covered_in_func / total_in_func * 100) if total_in_func else 100.0
            func_coverage[func_key] = {
                "line_range": line_range,
                "statement_rate": round(rate, 1),
                "covered": rate == 100.0,
                "missed_lines": missed_in_func,
                "missed_branches": missed_br_in_func,
            }
            total_funcs += 1
            file_total_funcs += 1
            if rate == 100.0:
                cov_funcs += 1
                file_cov_funcs += 1

        per_file[rel_path] = {
            "statement_rate": round(s_covered / s_total * 100, 1) if s_total else 100.0,
            "branch_rate": round(b_covered / b_total * 100, 1) if b_total else 100.0,
            "function_rate": round(file_cov_funcs / file_total_funcs * 100, 1) if file_total_funcs else 0.0,
            "covered_statements": s_covered, "total_statements": s_total,
            "covered_branches": b_covered, "total_branches": b_total,
            "covered_functions": file_cov_funcs, "total_functions": file_total_funcs,
            "missed_lines": missed_lines, "missed_branches": missed_branches,
            "functions": func_coverage,
        }
        total_stmts += s_total; cov_stmts += s_covered
        total_branches += b_total; cov_branches += b_covered

    summary = {
        "statement_rate": round(cov_stmts / total_stmts * 100, 1) if total_stmts else 0.0,
        "branch_rate": round(cov_branches / total_branches * 100, 1) if total_branches else 0.0,
        "function_rate": round(cov_funcs / total_funcs * 100, 1) if total_funcs else 0.0,
        "covered_statements": cov_stmts, "total_statements": total_stmts,
        "covered_branches": cov_branches, "total_branches": total_branches,
        "covered_functions": cov_funcs, "total_functions": total_funcs,
    }
    return per_file, summary


# ---------------------------------------------------------------------------
# 作用域过滤（单文件模式）
# ---------------------------------------------------------------------------

def _parse_scope(scope_sources):
    if not scope_sources:
        return []
    return [s.strip() for s in scope_sources.split(",") if s.strip()]


def _apply_scope(coverage, summary, scope_sources):
    """把 per-file coverage 和 summary 缩到 scope_sources 指定的文件集。"""
    scope = _parse_scope(scope_sources)
    if not scope:
        return coverage, summary

    scope_set = set(scope)
    filtered = {k: v for k, v in coverage.items() if k in scope_set}

    ts = cs = tb = cb = tf = cf = 0
    for f in filtered.values():
        ts += f.get("total_statements", 0)
        cs += f.get("covered_statements", 0)
        tb += f.get("total_branches", 0)
        cb += f.get("covered_branches", 0)
        for fc in f.get("functions", {}).values():
            tf += 1
            if fc.get("covered"):
                cf += 1

    scoped_summary = {
        "statement_rate": round(cs / ts * 100, 1) if ts else 0.0,
        "branch_rate": round(cb / tb * 100, 1) if tb else 0.0,
        "function_rate": round(cf / tf * 100, 1) if tf else 0.0,
        "covered_statements": cs, "total_statements": ts,
        "covered_branches": cb, "total_branches": tb,
        "covered_functions": cf, "total_functions": tf,
    }
    return filtered, scoped_summary


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def _summarize_tests(tests):
    total = len(tests)
    passed = sum(1 for t in tests if t["status"] == "passed")
    failed = sum(1 for t in tests if t["status"] == "failed")
    errors = sum(1 for t in tests if t["status"] == "error")
    skipped = sum(1 for t in tests if t["status"] == "skipped")
    return {
        "total_tests": total,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "skipped": skipped,
        "pass_rate": round(passed / total * 100, 1) if total else 0.0,
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="执行 Python 测试并采集覆盖率")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="执行测试 + 采集覆盖率")
    p_run.add_argument("--language", choices=["python"], default="python")
    p_run.add_argument("--repo-root", default=".")
    p_run.add_argument("--tests", default="test/generated_unit",
                       help="全量模式：测试目录")
    p_run.add_argument("--test-file", default=None,
                       help="单文件模式：只跑这一个测试文件（覆盖 --tests）")
    p_run.add_argument("--source-dirs", default=".",
                       help="覆盖率源目录，逗号分隔")
    p_run.add_argument("--scope-sources", default=None,
                       help="仅在报告里保留这些源文件（逗号分隔的相对路径），"
                            "并按它们重新计算 summary。适合 sub-agent 的 per-file 模式。")
    p_run.add_argument("--baseline", default=None, help="基线路径")
    # O3: --only-cases
    p_run.add_argument("--only-cases", default=None,
                       help="只跑指定 case 的测试函数，逗号分隔（如 functional_01,boundary_02）。"
                            "通过 case_id→test_name 映射转为 pytest -k 表达式")
    # O4: --no-coverage
    p_run.add_argument("--no-coverage", action="store_true", default=False,
                       help="跳过覆盖率采集，用于 fix 循环只关心 pass/fail 的场景")
    # O2: --cov-append
    p_run.add_argument("--cov-append", action="store_true", default=False,
                       help="增量覆盖率模式：追加到现有 .coverage 文件而非覆盖。"
                            "配合 --cov-file 指定 per-shard 的 .coverage 路径")
    p_run.add_argument("--cov-file", default=None,
                       help="指定 .coverage 文件路径（per-shard 模式）。"
                            "启用 --cov-append 时，每轮追加到此文件；"
                            "最终由主 agent 调用 coverage combine 合并")
    # O5: xdist 阈值化
    p_run.add_argument("--xdist-min-tests", type=int, default=20,
                       help="低于此数量的测试不启用 xdist 并行（默认 20）")
    p_run.add_argument("--xdist-workers", default=None,
                       help="覆盖 xdist worker 数量（默认 logical）。仅在超过 --xdist-min-tests 时生效")
    # 原有
    p_run.add_argument("--no-parallel", action="store_true", default=False,
                       help="完全禁用 pytest-xdist（用于 debug）")
    p_run.add_argument("--output", required=True)

    args = parser.parse_args()

    if args.command == "run":
        baseline = None
        if args.baseline and Path(args.baseline).is_file():
            with open(args.baseline, "r", encoding="utf-8") as f:
                baseline = json.load(f)

        drifts = []
        if baseline:
            drifts = _check_baseline_md5(Path(args.baseline), Path(args.repo_root).resolve())
            if drifts:
                print(f"[parse] 警告: {len(drifts)} 个源文件的 md5 与基线不符，建议重跑 init：",
                      file=sys.stderr)
                for d in drifts[:5]:
                    print(f"  - {d['path']}", file=sys.stderr)

        result = _run_python(args, baseline)
        result["md5_drifts"] = drifts
        _write_json_atomic(result, Path(args.output))

        rc = result.get("return_code", 0)
        summary = result.get("summary", {})
        cov = summary.get("coverage", {})
        print(
            f"\n[done] → {args.output}  (rc={rc})",
            file=sys.stderr,
        )
        sys.exit(rc)


if __name__ == "__main__":
    main()
