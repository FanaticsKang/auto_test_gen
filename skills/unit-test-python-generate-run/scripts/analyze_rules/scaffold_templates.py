"""
scaffold_templates — 按 signature/dimensions 推占位 case 的模板引擎。

对每个 gap 函数，根据：
  - 函数签名（参数类型、默认值）
  - 维度列表（functional / boundary / exception / security / ...）
  - 已有 case（去重）

生成占位 case 列表，LLM 只需填充具体断言。
"""

import re
from typing import List, Dict, Any, Optional

# ---------------------------------------------------------------------------
# 维度 → 默认 case 模板
# ---------------------------------------------------------------------------

_DIMENSION_TEMPLATES = {
    "functional": {
        "id_pattern": "{func_key}_func_{seq:02d}",
        "description": "正常输入：{args_hint}",
        "status": "pending",
    },
    "boundary": {
        "id_pattern": "{func_key}_bnd_{seq:02d}",
        "description": "边界值：{args_hint}",
        "status": "pending",
    },
    "exception": {
        "id_pattern": "{func_key}_exc_{seq:02d}",
        "description": "异常输入：预期抛出 {exc_type}",
        "status": "pending",
    },
    "security": {
        "id_pattern": "{func_key}_sec_{seq:02d}",
        "description": "安全边界：{args_hint}",
        "status": "pending",
    },
    "performance": {
        "id_pattern": "{func_key}_perf_{seq:02d}",
        "description": "性能测试：{args_hint}",
        "status": "pending",
    },
    "integration": {
        "id_pattern": "{func_key}_int_{seq:02d}",
        "description": "集成场景：{args_hint}",
        "status": "pending",
    },
}

# 默认参数类型提示
_TYPE_HINTS = {
    "int": ["0", "1", "-1", "999999"],
    "float": ["0.0", "1.0", "-1.0", "1e10"],
    "str": ["''", "'a'", "'x' * 1000"],
    "bool": ["True", "False"],
    "list": ["[]", "[1]", "[1, 2, 3]"],
    "dict": ["{}", "{'key': 'val'}"],
    "None": ["None"],
}


def scaffold_cases(
    func_key: str,
    signature: str,
    dimensions: List[str],
    existing_case_ids: Optional[List[str]] = None,
    docstring: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """为单个函数生成占位 case 列表。

    参数:
        func_key:          函数标识 (module.func)
        signature:         函数签名文本 (如 "add(a: int, b: int) -> int")
        dimensions:        需要覆盖的维度列表
        existing_case_ids: 已有 case ID，用于去重
        docstring:         函数文档字符串（用于提取 raise 信息）

    返回:
        case dict 列表，每个含 id / dimension / test_name / description / status
    """
    existing = set(existing_case_ids or [])
    args_hint = _parse_args_hint(signature)
    raises = _extract_raises(docstring) if docstring else []

    cases = []
    for dim in dimensions:
        tmpl = _DIMENSION_TEMPLATES.get(dim)
        if not tmpl:
            # 未知维度，用通用模板
            tmpl = {
                "id_pattern": f"{{func_key}}_{dim[:3]}_{{seq:02d}}",
                "description": f"{dim}: {{args_hint}}",
                "status": "pending",
            }

        # exception 维度特殊处理：用 docstring 里的 raises
        if dim == "exception" and raises:
            for i, exc_type in enumerate(raises, 1):
                case_id = tmpl["id_pattern"].format(func_key=func_key, seq=i)
                if case_id in existing:
                    continue
                cases.append({
                    "id": case_id,
                    "dimension": dim,
                    "test_name": f"test_{func_key}_{dim}_{i:02d}",
                    "description": tmpl["description"].format(
                        args_hint=args_hint, exc_type=exc_type,
                    ),
                    "status": "pending",
                })
        else:
            # 每个（非 exception）维度生成 1~2 个占位
            for seq in (1, 2):
                case_id = tmpl["id_pattern"].format(func_key=func_key, seq=seq)
                if case_id in existing:
                    continue
                cases.append({
                    "id": case_id,
                    "dimension": dim,
                    "test_name": f"test_{func_key}_{dim}_{seq:02d}",
                    "description": tmpl["description"].format(args_hint=args_hint),
                    "status": "pending",
                })
                # 如果维度是 functional，一般只需要 1 个
                if dim == "functional":
                    break

    return cases


def scaffold_gaps(gaps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """批量对 gaps 生成占位 case。

    参数:
        gaps: extract-failures 或 gaps 输出的 gap 列表

    返回:
        所有 gap 生成的 case 列表
    """
    all_cases = []
    for gap in gaps:
        func_key = gap.get("function", "")
        signature = gap.get("signature", "")
        dimensions = gap.get("dimensions", gap.get("missing_dimensions", []))
        existing_ids = [
            c.get("id", "") for c in gap.get("existing_cases", [])
        ]

        cases = scaffold_cases(
            func_key=func_key,
            signature=signature,
            dimensions=dimensions,
            existing_case_ids=existing_ids,
            docstring=gap.get("docstring"),
        )
        for c in cases:
            c["file"] = gap.get("file", "")
            c["test_path"] = gap.get("test_path", "")
        all_cases.extend(cases)

    return all_cases


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _parse_args_hint(signature: str) -> str:
    """从签名提取参数提示文本。"""
    # 匹配括号内的参数部分
    m = re.search(r"\(([^)]*)\)", signature)
    if not m:
        return "无参数"
    args_str = m.group(1).strip()
    if not args_str or args_str == "self":
        return "无参数"
    # 简化：只保留参数名
    parts = []
    for arg in args_str.split(","):
        arg = arg.strip()
        if arg in ("self", "cls", "*args", "**kwargs"):
            continue
        # 去掉类型注解和默认值
        name = arg.split(":")[0].split("=")[0].strip()
        if name and name not in ("*", "**"):
            parts.append(name)
    return ", ".join(parts) if parts else "无参数"


_RAISES_PATTERN = re.compile(r"raises?\s*:?\s*([\w\.]+)", re.IGNORECASE)


def _extract_raises(docstring: str) -> List[str]:
    """从 docstring 提取 raise 声明。"""
    return list(set(_RAISES_PATTERN.findall(docstring)))
