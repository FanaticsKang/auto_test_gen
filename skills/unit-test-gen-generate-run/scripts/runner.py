#!/usr/bin/env python3
"""
runner.py — 执行测试并采集覆盖率，输出统一结构的 run_result.json。

子命令：
  run   执行测试 + 采集覆盖率

Python 路径: pytest + pytest-cov + coverage.py
C++ 路径: cmake build + ctest + gcov/lcov

本脚本不修改测试代码，不判断失败原因，不重跑。

Sub-agent 并行场景下，用 --test-file 和 --scope-sources 把作用域限制到
单个源文件 + 对应测试文件；--output 指向 per-file shard 路径（例如
.test/run_results/<slug>.json），避免多个 sub-agent 互相覆盖结果。

用法（Python，全量）：
  python runner.py run --language python --repo-root . --tests test/generated_unit --source-dirs . --baseline test/generated_unit/test_cases.json --output .test/run_result.json

用法（Python，单文件）：
  python runner.py run --language python --repo-root . --test-file test/generated_unit/core/test_parser.py --source-dirs core --scope-sources core/parser.py --baseline test/generated_unit/test_cases.json --output .test/run_results/core__parser_py.json

用法（C++）：
  python runner.py run --language cpp --repo-root . --build-cmd "cmake --build build" --test-cmd "ctest --test-dir build" --gcov-root build --baseline test/generated_unit/test_cases.json --output .test/run_result.json
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
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

CASE_ID_PATTERN = re.compile(r"(?:#|//)\s*CASE_ID\s*:\s*([A-Za-z0-9_\-]+)")


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


def _run(cmd, cwd=None, env=None, capture=True):
    kwargs = dict(cwd=cwd, env=env, shell=isinstance(cmd, str))
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc = subprocess.run(cmd, **kwargs)
    out = proc.stdout.decode("utf-8", errors="replace") if capture and proc.stdout else ""
    err = proc.stderr.decode("utf-8", errors="replace") if capture and proc.stderr else ""
    return proc.returncode, out, err


# ---------------------------------------------------------------------------
# CASE_ID 映射
# ---------------------------------------------------------------------------

def _parse_case_id_map(tests_dir: Path) -> dict:
    mapping = {}
    for root, _, files in os.walk(tests_dir):
        for fname in files:
            fpath = Path(root) / fname
            ext = fpath.suffix.lower()
            if ext not in (".py", ".cpp", ".cc", ".cxx", ".h", ".hpp"):
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

                if ext == ".py":
                    match = re.match(r"\s*def\s+(test_[A-Za-z0-9_]+)\s*\(", line)
                    if match:
                        mapping[(str(fpath), match.group(1))] = pending_case
                        pending_case = None
                        continue

                match = re.match(
                    r"\s*TEST(?:_F|_P)?\s*\(\s*([A-Za-z0-9_]+)\s*,\s*([A-Za-z0-9_]+)\s*\)",
                    line,
                )
                if match:
                    full_name = f"{match.group(1)}.{match.group(2)}"
                    mapping[(str(fpath), full_name)] = pending_case
                    pending_case = None
                    continue

                stripped = line.strip()
                if stripped and not stripped.startswith(("#", "//", "/*", "*")):
                    if not re.match(r"\s*(def|TEST|@)", stripped):
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
        print(f"警告: 无法解析 junit xml {xml_path}: {e}", file=sys.stderr)
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
    tool_status = {"pytest": False, "pytest_cov": False, "coverage_json": False}

    py = sys.executable or "python"
    rc, _, _ = _run([py, "-c", "import pytest"])
    tool_status["pytest"] = rc == 0
    rc, _, _ = _run([py, "-c", "import pytest_cov"])
    tool_status["pytest_cov"] = rc == 0
    rc, _, _ = _run([py, "-c", "import coverage"])
    tool_status["coverage_json"] = rc == 0

    if not tool_status["pytest"]:
        return {
            "language": "python",
            "error": "pytest 未安装，请 pip install pytest pytest-cov coverage",
            "tool_status": tool_status,
        }

    with tempfile.TemporaryDirectory(prefix="autogen_run_") as tmpdir:
        tmp = Path(tmpdir)
        junit_xml = tmp / "junit.xml"
        cov_data = tmp / ".coverage"
        cov_json = tmp / "coverage.json"

        cmd = [
            py, "-m", "pytest",
            str(pytest_target),
            f"--junit-xml={junit_xml}",
            "-q", "--no-header", "--tb=short",
        ]
        env = dict(os.environ)
        if tool_status["pytest_cov"] and tool_status["coverage_json"]:
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
            env["COVERAGE_FILE"] = str(cov_data)

        print(f"$ {' '.join(cmd)}", file=sys.stderr)
        rc, out, err = _run(cmd, cwd=str(repo_root), env=env)
        print(out, file=sys.stderr)
        if err:
            print(err, file=sys.stderr)

        tests = _parse_junit_xml(junit_xml)
        case_map = _parse_case_id_map(tests_scan_dir)
        tests = _attach_case_ids_python(tests, case_map, repo_root)

        coverage = {}
        coverage_summary = {
            "statement_rate": 0.0, "branch_rate": 0.0, "function_rate": 0.0,
            "covered_statements": 0, "total_statements": 0,
            "covered_branches": 0, "total_branches": 0,
            "covered_functions": 0, "total_functions": 0,
        }
        if cov_json.is_file():
            coverage, coverage_summary = _parse_coverage_json(cov_json, baseline, repo_root)
            coverage, coverage_summary = _apply_scope(
                coverage, coverage_summary, args.scope_sources
            )

        summary = _summarize_tests(tests)
        summary["coverage"] = coverage_summary

    return {
        "language": "python",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "return_code": rc,
        "tool_status": tool_status,
        "scope_sources": _parse_scope(args.scope_sources),
        "summary": summary,
        "tests": tests,
        "coverage": coverage,
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
            if rate == 100.0:
                cov_funcs += 1

        per_file[rel_path] = {
            "statement_rate": round(s_covered / s_total * 100, 1) if s_total else 100.0,
            "branch_rate": round(b_covered / b_total * 100, 1) if b_total else 100.0,
            "covered_statements": s_covered, "total_statements": s_total,
            "covered_branches": b_covered, "total_branches": b_total,
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
# C++: build + ctest + lcov
# ---------------------------------------------------------------------------

def _run_cpp(args, baseline):
    repo_root = Path(args.repo_root).resolve()
    tool_status = {
        "build": False, "ctest": False,
        "gcov": bool(shutil.which("gcov")), "lcov": bool(shutil.which("lcov")),
    }

    if not args.build_cmd or not args.test_cmd:
        return {
            "language": "cpp",
            "error": "C++ 路径需要 --build-cmd 和 --test-cmd 参数",
            "tool_status": tool_status,
        }

    print(f"$ {args.build_cmd}", file=sys.stderr)
    rc_build, out_b, err_b = _run(args.build_cmd, cwd=str(repo_root))
    print(out_b, file=sys.stderr)
    if err_b:
        print(err_b, file=sys.stderr)
    tool_status["build"] = rc_build == 0
    if rc_build != 0:
        return {
            "language": "cpp",
            "error": f"build 失败 (rc={rc_build})",
            "build_stderr": err_b[-4000:],
            "tool_status": tool_status,
        }

    with tempfile.TemporaryDirectory(prefix="autogen_cpp_") as tmpdir:
        tmp = Path(tmpdir)
        junit_xml = tmp / "junit.xml"
        env = dict(os.environ)
        env["GTEST_OUTPUT"] = f"xml:{junit_xml}"
        if args.test_filter:
            env["GTEST_FILTER"] = args.test_filter

        print(f"$ {args.test_cmd}" + (f" (GTEST_FILTER={args.test_filter})" if args.test_filter else ""),
              file=sys.stderr)
        rc_test, out_t, err_t = _run(args.test_cmd, cwd=str(repo_root), env=env)
        print(out_t, file=sys.stderr)
        if err_t:
            print(err_t, file=sys.stderr)
        tool_status["ctest"] = True

        tests = []
        if junit_xml.is_file():
            tests = _parse_junit_xml(junit_xml)
        else:
            gcov_root = repo_root / (args.gcov_root or "build")
            for xml_file in gcov_root.rglob("*.xml"):
                try:
                    parsed = _parse_junit_xml(xml_file)
                    if parsed:
                        tests.extend(parsed)
                except Exception:
                    continue

        case_map = _parse_case_id_map(Path(args.tests or "test/generated_unit"))
        tests = _attach_case_ids_cpp(tests, case_map)

        coverage = {}
        coverage_summary = {
            "statement_rate": 0.0, "branch_rate": 0.0, "function_rate": 0.0,
            "covered_statements": 0, "total_statements": 0,
            "covered_branches": 0, "total_branches": 0,
            "covered_functions": 0, "total_functions": 0,
        }
        if tool_status["lcov"] and args.gcov_root:
            lcov_info = tmp / "coverage.info"
            rc_lc, _, err_lc = _run(
                f"lcov --capture --directory {args.gcov_root} --output-file {lcov_info} "
                f"--rc lcov_branch_coverage=1 --quiet",
                cwd=str(repo_root),
            )
            if rc_lc == 0 and lcov_info.is_file():
                coverage, coverage_summary = _parse_lcov_info(lcov_info, baseline, repo_root)
                coverage, coverage_summary = _apply_scope(
                    coverage, coverage_summary, args.scope_sources
                )
            else:
                print(f"警告: lcov 采集失败: {err_lc}", file=sys.stderr)

        summary = _summarize_tests(tests)
        summary["coverage"] = coverage_summary

    return {
        "language": "cpp",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "return_code": rc_test,
        "tool_status": tool_status,
        "scope_sources": _parse_scope(args.scope_sources),
        "summary": summary,
        "tests": tests,
        "coverage": coverage,
    }


def _attach_case_ids_cpp(tests, case_map):
    for t in tests:
        full = f"{t['classname']}.{t['name']}" if t.get("classname") else t["name"]
        case_id = None
        for (_tf, tname), cid in case_map.items():
            if tname == full:
                case_id = cid
                t["test_file"] = _tf
                break
        t["case_id"] = case_id
    return tests


def _parse_lcov_info(info_path, baseline, repo_root):
    per_file = {}
    current = None
    baseline_files = (baseline or {}).get("files", {})

    with open(info_path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if line.startswith("SF:"):
                abs_path = line[3:]
                try:
                    rel = str(Path(abs_path).resolve().relative_to(repo_root))
                except ValueError:
                    rel = abs_path
                current = {
                    "rel_path": rel,
                    "hit_lines": {},
                    "br_entries": [],
                    "fn_hits": {},
                }
            elif line.startswith("DA:") and current is not None:
                parts = line[3:].split(",")
                ln = int(parts[0]); hits = int(parts[1])
                current["hit_lines"][ln] = hits
            elif line.startswith("BRDA:") and current is not None:
                parts = line[5:].split(",")
                ln = int(parts[0]); block = parts[1]; branch = parts[2]
                taken_raw = parts[3]
                taken = 0 if taken_raw == "-" else int(taken_raw)
                current["br_entries"].append((ln, block, branch, taken))
            elif line.startswith("FNDA:") and current is not None:
                parts = line[5:].split(",", 1)
                hits = int(parts[0]); name = parts[1] if len(parts) > 1 else ""
                current["fn_hits"][name] = hits
            elif line == "end_of_record" and current is not None:
                rel = current["rel_path"]
                hit_lines = current["hit_lines"]
                total_s = len(hit_lines)
                cov_s = sum(1 for h in hit_lines.values() if h > 0)
                total_b = len(current["br_entries"])
                cov_b = sum(1 for e in current["br_entries"] if e[3] > 0)
                missed_lines = sorted([ln for ln, h in hit_lines.items() if h == 0])
                missed_branches = [(e[0], int(e[2])) for e in current["br_entries"] if e[3] == 0]

                func_coverage = {}
                bfile = baseline_files.get(rel, {})
                for func_key, fmeta in bfile.get("functions", {}).items():
                    start, end = fmeta.get("line_range", [0, 0])
                    func_lines = [ln for ln in hit_lines if start <= ln <= end]
                    if not func_lines:
                        continue
                    f_total = len(func_lines)
                    f_cov = sum(1 for ln in func_lines if hit_lines[ln] > 0)
                    f_missed_lines = [ln for ln in func_lines if hit_lines[ln] == 0]
                    f_missed_br = [b for b in missed_branches if start <= b[0] <= end]
                    rate = (f_cov / f_total * 100) if f_total else 100.0
                    func_coverage[func_key] = {
                        "line_range": [start, end],
                        "statement_rate": round(rate, 1),
                        "covered": rate == 100.0,
                        "missed_lines": f_missed_lines,
                        "missed_branches": f_missed_br,
                    }

                per_file[rel] = {
                    "statement_rate": round(cov_s / total_s * 100, 1) if total_s else 100.0,
                    "branch_rate": round(cov_b / total_b * 100, 1) if total_b else 100.0,
                    "covered_statements": cov_s, "total_statements": total_s,
                    "covered_branches": cov_b, "total_branches": total_b,
                    "missed_lines": missed_lines, "missed_branches": missed_branches,
                    "functions": func_coverage,
                }
                current = None

    ts = tb = tf = cs = cb = cf = 0
    for f in per_file.values():
        ts += f["total_statements"]; cs += f["covered_statements"]
        tb += f["total_branches"]; cb += f["covered_branches"]
        for fc in f.get("functions", {}).values():
            tf += 1
            if fc["covered"]:
                cf += 1

    summary = {
        "statement_rate": round(cs / ts * 100, 1) if ts else 0.0,
        "branch_rate": round(cb / tb * 100, 1) if tb else 0.0,
        "function_rate": round(cf / tf * 100, 1) if tf else 0.0,
        "covered_statements": cs, "total_statements": ts,
        "covered_branches": cb, "total_branches": tb,
        "covered_functions": cf, "total_functions": tf,
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
    parser = argparse.ArgumentParser(description="执行测试并采集覆盖率")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="执行测试 + 采集覆盖率")
    p_run.add_argument("--language", choices=["python", "cpp"], required=True)
    p_run.add_argument("--repo-root", default=".")
    p_run.add_argument("--tests", default="test/generated_unit",
                       help="全量模式：测试目录")
    p_run.add_argument("--test-file", default=None,
                       help="[python] 单文件模式：只跑这一个测试文件（覆盖 --tests）")
    p_run.add_argument("--test-filter", default=None,
                       help="[cpp] gtest 过滤表达式（如 ParseHeaderTest.*），"
                            "通过 GTEST_FILTER 环境变量传给测试可执行文件")
    p_run.add_argument("--source-dirs", default=".",
                       help="[python] 覆盖率源目录，逗号分隔")
    p_run.add_argument("--scope-sources", default=None,
                       help="仅在报告里保留这些源文件（逗号分隔的相对路径），"
                            "并按它们重新计算 summary。适合 sub-agent 的 per-file 模式。")
    p_run.add_argument("--build-cmd", default=None, help="[cpp] 构建命令")
    p_run.add_argument("--test-cmd", default=None, help="[cpp] 测试命令")
    p_run.add_argument("--gcov-root", default="build", help="[cpp] gcov 数据根目录")
    p_run.add_argument("--baseline", default=None, help="基线路径")
    p_run.add_argument("--output", required=True)

    args = parser.parse_args()

    baseline = None
    if args.command == "run":
        if args.baseline and Path(args.baseline).is_file():
            with open(args.baseline, "r", encoding="utf-8") as f:
                baseline = json.load(f)

        drifts = []
        if baseline:
            drifts = _check_baseline_md5(Path(args.baseline), Path(args.repo_root).resolve())
            if drifts:
                print(f"警告: {len(drifts)} 个源文件的 md5 与基线不符，建议重跑 init：",
                      file=sys.stderr)
                for d in drifts[:5]:
                    print(f"  - {d['path']}", file=sys.stderr)

        if args.language == "python":
            result = _run_python(args, baseline)
        else:
            result = _run_cpp(args, baseline)

        result["md5_drifts"] = drifts
        _write_json_atomic(result, Path(args.output))

        summary = result.get("summary", {})
        cov = summary.get("coverage", {})
        print(
            f"\n执行完成 → {args.output}\n"
            f"  测试: {summary.get('passed', 0)}/{summary.get('total_tests', 0)} 通过 "
            f"(failed={summary.get('failed', 0)}, error={summary.get('errors', 0)})\n"
            f"  覆盖率: stmt={cov.get('statement_rate', 0)}%, "
            f"branch={cov.get('branch_rate', 0)}%, "
            f"func={cov.get('function_rate', 0)}%",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
