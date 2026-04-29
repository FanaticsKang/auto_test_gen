"""
lint_rules — case 质量校验规则。

检查项：
  1. 必填字段完整（id, dimension, test_name, description, status）
  2. dimension 值合法
  3. test_name 符合 pytest 命名规范
  4. id 唯一（md5 去重）
  5. mock 路径可解析（如果有 mocks 字段）
  6. assertion_origin 合法（Phase 1b）
  7. 弱断言检测（Phase 3，按 dimension 分级）

输出 findings 列表，每个含 case_id / rule / severity / message。
"""

import hashlib
import re
from typing import List, Dict, Any, Optional

# 合法维度
VALID_DIMENSIONS = {
    "functional", "boundary", "exception", "security",
    "performance", "integration", "regression", "edge_case",
}

# 必填字段
REQUIRED_FIELDS = ("id", "dimension", "test_name", "description", "status")

# 合法状态
VALID_STATUSES = {"pending", "passed", "failed", "failed_persistent",
                  "source_bug", "skipped", "fixed_pending_rerun", "orphaned"}

# 合法 assertion_origin 值
VALID_ASSERTION_ORIGINS = {"blind", "sighted"}

# pytest 函数名规范
_TEST_NAME_PATTERN = re.compile(r"^test_[a-zA-Z0-9_]+$")

# case ID 规范
_CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]+$")

# ---------------------------------------------------------------------------
# Phase 3: 弱断言检测模式（按 dimension 分级）
# ---------------------------------------------------------------------------

# 通用弱断言模式（所有维度都禁止）
_WEAK_ASSERTION_PATTERNS = [
    (re.compile(r"assert\s+True\b"), "assert True（永远通过）"),
    (re.compile(r"assert\s+\w+\s+is\s+not\s+None\b"), "assert x is not None（无具体值验证）"),
    (re.compile(r"assert\s+\w+\s+is\s+None\b"), "assert x is None（仅验证 None）"),
]

# functional 维度额外禁止
_WEAK_FUNCTIONAL_PATTERNS = [
    (re.compile(r"assert\s+isinstance\s*\("), "assert isinstance（无具体值断言）"),
    (re.compile(r"assert\s+len\s*\(\s*\S+\s*\)\s*>\s*0"), "assert len(x) > 0（仅验证非空）"),
]

# exception/execution 维度：必须有 pytest.raises
_MISSING_RAISES_PATTERN = re.compile(r"pytest\.raises\s*\(")


def lint_cases(
    cases: List[Dict[str, Any]],
    repo_root: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """批量校验 case 列表。

    参数:
        cases:     case dict 列表
        repo_root: 可选，仓库根目录（用于校验 mock 路径）

    返回:
        findings 列表: [{case_id, rule, severity, message}]
    """
    findings = []
    seen_ids = {}  # id → md5 of content for dedup

    for case in cases:
        case_id = case.get("id", "<unknown>")
        case_findings = []

        # Rule 1: 必填字段
        for field in REQUIRED_FIELDS:
            if not case.get(field):
                case_findings.append({
                    "case_id": case_id,
                    "rule": "missing_field",
                    "severity": "error",
                    "message": f"缺少必填字段: {field}",
                })

        # Rule 2: dimension 合法
        dim = case.get("dimension", "")
        if dim and dim not in VALID_DIMENSIONS:
            case_findings.append({
                "case_id": case_id,
                "rule": "invalid_dimension",
                "severity": "warning",
                "message": f"维度 '{dim}' 不在合法集合 {VALID_DIMENSIONS} 中",
            })

        # Rule 3: test_name 规范
        test_name = case.get("test_name", "")
        if test_name and not _TEST_NAME_PATTERN.match(test_name):
            case_findings.append({
                "case_id": case_id,
                "rule": "invalid_test_name",
                "severity": "error",
                "message": f"test_name '{test_name}' 不符合 pytest 命名规范 (test_xxx)",
            })

        # Rule 4: case_id 规范
        if case_id and case_id != "<unknown>" and not _CASE_ID_PATTERN.match(case_id):
            case_findings.append({
                "case_id": case_id,
                "rule": "invalid_case_id",
                "severity": "error",
                "message": f"case_id '{case_id}' 含非法字符",
            })

        # Rule 5: status 合法
        status = case.get("status", "")
        if status and status not in VALID_STATUSES:
            case_findings.append({
                "case_id": case_id,
                "rule": "invalid_status",
                "severity": "warning",
                "message": f"status '{status}' 不在合法集合中",
            })

        # Rule 5b: assertion_origin 合法
        assertion_origin = case.get("assertion_origin", "")
        if assertion_origin and assertion_origin not in VALID_ASSERTION_ORIGINS:
            case_findings.append({
                "case_id": case_id,
                "rule": "invalid_assertion_origin",
                "severity": "error",
                "message": f"assertion_origin '{assertion_origin}' 不在合法集合 {VALID_ASSERTION_ORIGINS} 中",
            })

        # Rule 6: ID 唯一性（基于内容 md5 去重）
        content_str = _case_content_key(case)
        content_md5 = hashlib.md5(content_str.encode("utf-8")).hexdigest()[:12]
        if case_id in seen_ids:
            if seen_ids[case_id] != content_md5:
                case_findings.append({
                    "case_id": case_id,
                    "rule": "duplicate_id_different_content",
                    "severity": "error",
                    "message": f"case_id '{case_id}' 重复但内容不同",
                })
        else:
            seen_ids[case_id] = content_md5

        # Rule 7: mock 路径可解析（如果有 mocks 字段且 repo_root 可用）
        mocks = case.get("mocks", [])
        if mocks and repo_root:
            from pathlib import Path
            root = Path(repo_root)
            for mock_path in mocks:
                if isinstance(mock_path, str) and mock_path.startswith(("src/", "lib/")):
                    if not (root / mock_path).exists():
                        case_findings.append({
                            "case_id": case_id,
                            "rule": "unresolved_mock_path",
                            "severity": "warning",
                            "message": f"mock 路径 '{mock_path}' 不存在",
                        })

        findings.extend(case_findings)

    # Rule 8: 内容 md5 去重（不同 id 但相同内容的 case）
    content_to_ids = {}
    for case in cases:
        key = _case_content_key(case)
        md5 = hashlib.md5(key.encode("utf-8")).hexdigest()[:12]
        cid = case.get("id", "<unknown>")
        if md5 in content_to_ids:
            existing_id = content_to_ids[md5]
            if existing_id != cid:
                findings.append({
                    "case_id": cid,
                    "rule": "duplicate_content",
                    "severity": "warning",
                    "message": f"case 内容与 '{existing_id}' 相同（md5={md5}）",
                })
        else:
            content_to_ids[md5] = cid

    return findings


def lint_cases_patch(patch: Dict[str, Any], repo_root: Optional[str] = None) -> List[Dict[str, Any]]:
    """从 cases-patch 格式中提取所有 case 并校验。

    参数:
        patch:    cases-patch dict（含 files → functions → cases）
        repo_root: 可选，仓库根目录

    返回:
        findings 列表
    """
    all_cases = []
    for file_path, file_data in patch.get("files", {}).items():
        funcs = file_data.get("functions", {})
        for func_key, func_data in funcs.items():
            for case in func_data.get("cases", []):
                all_cases.append(case)

    return lint_cases(all_cases, repo_root=repo_root)


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _case_content_key(case: Dict[str, Any]) -> str:
    """生成 case 内容去重用的 key（排除 id 和 status）。"""
    parts = []
    for k in sorted(case.keys()):
        if k in ("id", "status", "round_added", "fix_attempts", "failure_reason", "assertion_origin"):
            continue
        parts.append(f"{k}={case[k]}")
    return "|".join(str(p) for p in parts)


# ---------------------------------------------------------------------------
# Phase 3: 断言质量 lint（扫描测试文件源码）
# ---------------------------------------------------------------------------

def lint_assertions(
    test_source: str,
    case_dimensions: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """扫描测试源码中的弱断言，按 dimension 分级检测。

    参数:
        test_source:       测试文件的完整源码
        case_dimensions:   可选，test_name → dimension 映射

    返回:
        findings 列表: [{test_name, dimension, rule, severity, message}]
    """
    findings = []
    lines = test_source.splitlines()
    current_case_id = None
    current_dimension = None

    for i, line in enumerate(lines, 1):
        stripped = line.strip()

        # 跟踪 def test_ 函数（用函数名查 dimension）
        func_match = re.match(r"def\s+(test_\w+)\s*\(", stripped)
        if func_match:
            current_case_id = func_match.group(1)
            if case_dimensions:
                current_dimension = case_dimensions.get(current_case_id)
            continue

        # 跳过非 assert 行
        if not stripped.startswith("assert ") and "assert " not in stripped:
            continue

        # 通用弱断言检测
        for pattern, desc in _WEAK_ASSERTION_PATTERNS:
            if pattern.search(stripped):
                findings.append({
                    "line": i,
                    "test_name": current_case_id or "<unknown>",
                    "dimension": current_dimension,
                    "rule": "weak_assertion",
                    "severity": "warning",
                    "message": f"弱断言: {desc}",
                })
                break  # 每行只报一次通用弱断言

        # functional 维度额外检测
        if current_dimension == "functional":
            for pattern, desc in _WEAK_FUNCTIONAL_PATTERNS:
                if pattern.search(stripped):
                    findings.append({
                        "line": i,
                        "test_name": current_case_id or "<unknown>",
                        "dimension": current_dimension,
                        "rule": "weak_functional_assertion",
                        "severity": "warning",
                        "message": f"functional 维度弱断言: {desc}",
                    })

    # exception/execution 维度：检查函数是否包含 pytest.raises
    _check_exception_dimension(test_source, case_dimensions or {}, findings)

    return findings


def _check_exception_dimension(
    test_source: str,
    case_dimensions: Dict[str, str],
    findings: List[Dict[str, Any]],
) -> None:
    """检查 exception/execution 维度的测试函数是否包含 pytest.raises。"""
    lines = test_source.splitlines()
    current_func = None
    has_raises = False
    func_start = 0

    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        func_match = re.match(r"def\s+(test_\w+)\s*\(", stripped)
        if func_match:
            # 检查前一个函数
            if current_func:
                dim = case_dimensions.get(current_func)
                if dim in ("exception", "execution") and not has_raises:
                    findings.append({
                        "line": func_start,
                        "test_name": current_func,
                        "dimension": dim,
                        "rule": "missing_pytest_raises",
                        "severity": "warning",
                        "message": f"{dim} 维度缺少 pytest.raises",
                    })
            current_func = func_match.group(1)
            has_raises = False
            func_start = i
        elif "pytest.raises" in stripped:
            has_raises = True

    # 最后一个函数
    if current_func:
        dim = case_dimensions.get(current_func)
        if dim in ("exception", "execution") and not has_raises:
            findings.append({
                "line": func_start,
                "test_name": current_func,
                "dimension": dim,
                "rule": "missing_pytest_raises",
                "severity": "warning",
                "message": f"{dim} 维度缺少 pytest.raises",
            })
