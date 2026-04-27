"""
failure_classification — 硬编码失败分类规则（Phase 2 重写）。

对每个失败 case，基于多轮证据积累给出预判：
  - test_code_bug:  测试代码问题 → LLM 应修测试
  - source_code_bug: 疑似源码 bug → 登记 bug
  - ambiguous:  不确定 → 按测试修处理，证据积累后升级

证据来源：
  1. traceback 分析（异常类型、顶帧位置）
  2. run_state 历史（fix_attempts、sibling case 状态）
  3. 跨轮 fingerprint（相同失败模式重复出现）
  4. assertion_origin（盲测断言失败信号更强）

输出 verdicts 列表，每个含 case_id / preliminary_verdict / confidence / evidence / needs_llm_review。
"""

import hashlib
import re
from typing import List, Dict, Any, Optional, Tuple

# ---------------------------------------------------------------------------
# 规则定义（Phase 2 重写）
# ---------------------------------------------------------------------------

# 异常类型 → (默认 verdict 方向, 置信度加分)
# Phase 2 变更：AssertionError 归零，不引入方向性噪声
_EXCEPTION_SIGNALS = {
    "AssertionError": ("ambiguous", 0.0),
    "TypeError": ("ambiguous", 0.3),
    "AttributeError": ("ambiguous", 0.3),
    "ImportError": ("test_code_bug", 0.5),
    "ModuleNotFoundError": ("test_code_bug", 0.5),
    "NameError": ("test_code_bug", 0.7),
    "SyntaxError": ("test_code_bug", 0.8),
    "KeyError": ("source_code_bug", 0.3),
    "IndexError": ("source_code_bug", 0.3),
    "ValueError": ("source_code_bug", 0.3),
    "ZeroDivisionError": ("source_code_bug", 0.4),
    "NotImplementedError": ("source_code_bug", 0.5),
    "PermissionError": ("source_code_bug", 0.4),
    "TimeoutError": ("ambiguous", 0.2),
    "RuntimeError": ("ambiguous", 0.2),
}

# traceback 中的 mock 字样
_MOCK_PATTERN = re.compile(
    r"mock|Mock|patch|MagicMock|AsyncMock|spy|stub",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Traceback fingerprint
# ---------------------------------------------------------------------------

def compute_traceback_fingerprint(
    traceback_text: Optional[str],
    test_file: Optional[str] = "",
) -> str:
    """计算 traceback fingerprint（忽略测试文件行号变化）。

    取 traceback 中所有非 test 文件的 (file, line, exception) 组合做 hash。
    """
    if not traceback_text:
        return ""
    parts = []
    lines = traceback_text.strip().splitlines()
    for line in lines:
        m = re.search(r'File "(.*?)", line (\d+)', line)
        if m:
            filepath = m.group(1)
            # 跳过 test 文件的行号（它们会随代码修改变化）
            is_test = (
                "test_" in filepath
                or "/test/" in filepath
            )
            if is_test:
                parts.append(f"TEST:{filepath}")
            else:
                parts.append(f"{filepath}:{m.group(2)}")
    # 取最后一行异常类型
    exc_type = ""
    for line in reversed(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("File ") and ":" in stripped:
            exc_type = stripped.split(":")[0].strip()
            break
    fingerprint_str = "|".join(parts) + "|" + exc_type
    return hashlib.md5(fingerprint_str.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# 核心分类函数（Phase 2 重写）
# ---------------------------------------------------------------------------

def classify_failure(
    case_id: str,
    test_file: str,
    test_name: str,
    failure_type: Optional[str],
    traceback: Optional[str],
    # Phase 2 新增参数
    run_state_case: Optional[Dict[str, Any]] = None,
    sibling_stats: Optional[Dict[str, Any]] = None,
    historical_fingerprints: Optional[List[str]] = None,
    is_last_round: bool = False,
) -> Dict[str, Any]:
    """对单个失败 case 执行多信号分类。

    参数:
        case_id:               case 标识
        test_file:             测试文件路径
        test_name:             测试函数名
        failure_type:          异常类型（如 AssertionError）
        traceback:             完整 traceback 文本
        run_state_case:        run_state 中该 case 的历史信息
                               {"fix_attempts": int, "assertion_origin": str, ...}
        sibling_stats:         同函数其他 case 的统计
                               {"total": int, "passed": int, "failed": int}
        historical_fingerprints: 之前轮次的 fingerprint 列表
        is_last_round:         是否为末轮（末轮 fingerprint 权重翻倍）

    返回:
        verdict dict
    """
    evidence = []
    confidence = 0.0
    verdict = "ambiguous"
    tb = traceback or ""
    rsc = run_state_case or {}
    sib = sibling_stats or {}
    hist_fps = historical_fingerprints or []

    # --- Rule 1: 顶帧位置 ---
    top_frame_in_test = _is_top_frame_in_test(tb, test_file)
    top_frame_in_source = False
    if not top_frame_in_test:
        top_frame_in_source = _is_top_frame_in_source(tb, test_file)

    if top_frame_in_test:
        evidence.append("顶帧在测试文件")
        # Phase 2: 顶帧在 test 文件只是 pytest 机制，neutral，不加减
    elif top_frame_in_source:
        evidence.append("顶帧在源码文件")
        confidence += 0.4
        verdict = "source_code_bug"

    # --- Rule 2: 异常类型 ---
    exc_verdict, exc_conf = _EXCEPTION_SIGNALS.get(
        failure_type or "", ("ambiguous", 0.0)
    )
    if exc_conf > 0:
        evidence.append(f"异常类型: {failure_type}")
        confidence += exc_conf
        if verdict == "ambiguous" and exc_verdict != "ambiguous":
            verdict = exc_verdict
        elif verdict == exc_verdict and exc_verdict != "ambiguous":
            confidence += 0.1  # 一致加分（排除 ambiguous==ambiguous）

    # --- Rule 3: mock 字样 ---
    has_mock = bool(_MOCK_PATTERN.search(tb))
    if has_mock:
        evidence.append("traceback 含 mock/patch 字样")
        if verdict == "source_code_bug":
            verdict = "ambiguous"
            confidence -= 0.2
        elif verdict == "ambiguous":
            verdict = "test_code_bug"
            confidence += 0.2

    # --- Rule 4: 相同 fingerprint 跨轮重现（Phase 2 新增）---
    # 注意：hist_fps 可能包含当轮 fp（sync 写入），切掉最后一项才是历史
    current_fp = compute_traceback_fingerprint(tb, test_file)
    prior_fps = hist_fps[:-1] if len(hist_fps) >= 2 else []
    if current_fp and current_fp in prior_fps:
        repeat_count = prior_fps.count(current_fp)
        fp_weight = 0.4 if not is_last_round else 0.8
        evidence.append(f"相同 fingerprint 跨轮重现 (×{repeat_count})")
        confidence += fp_weight
        if verdict == "ambiguous":
            verdict = "source_code_bug"

    # --- Rule 5: fix_attempts ≥ 1（Phase 2 新增）---
    _STRONG_TEST_BUG_SIGNALS = {"NameError", "SyntaxError", "ImportError",
                                "ModuleNotFoundError"}
    fix_attempts = rsc.get("fix_attempts", 0)
    if fix_attempts >= 1:
        evidence.append(f"已尝试修复 {fix_attempts} 次仍失败")
        confidence += 0.2
        if verdict == "ambiguous":
            verdict = "source_code_bug" if confidence >= 0.4 else "ambiguous"
        elif verdict == "test_code_bug" and (failure_type or "") not in _STRONG_TEST_BUG_SIGNALS:
            # NameError/SyntaxError/ImportError 修过还挂 → 再修一次，不翻
            if fix_attempts >= 2 and confidence >= 0.5:
                verdict = "source_code_bug"

    # --- Rule 6: sibling_stats（Phase 2 新增）---
    sib_total = sib.get("total", 0)
    sib_passed = sib.get("passed", 0)
    if sib_total >= 3 and sib_passed == sib_total - 1:
        # ≥3 siblings，除当前 case 外全部 passed
        evidence.append(f"同函数 {sib_total-1}/{sib_total-1} 个 sibling case 通过")
        confidence += 0.2

    # --- Rule 7: blind 断言 fingerprint 加成（Phase 2 新增）---
    assertion_origin = rsc.get("assertion_origin", "sighted")
    if assertion_origin == "blind" and current_fp and current_fp in prior_fps:
        evidence.append("盲测断言 fingerprint 重现加成")
        confidence += 0.1

    # --- 修正置信度 ---
    confidence = max(0.0, min(1.0, confidence))

    # 低置信度 → 需 LLM 复审
    needs_llm = confidence < 0.5

    return {
        "case_id": case_id,
        "test_name": test_name,
        "test_file": test_file,
        "preliminary_verdict": verdict,
        "confidence": round(confidence, 2),
        "evidence": evidence,
        "needs_llm_review": needs_llm,
        "traceback_fingerprint": current_fp,
    }


def classify_failures_batch(
    failures: List[Dict[str, Any]],
    run_state: Optional[Dict[str, Any]] = None,
    is_last_round: bool = False,
) -> List[Dict[str, Any]]:
    """批量分类失败 case（Phase 2 增强）。

    参数:
        failures:      list of test result dicts
        run_state:     完整的 run_state（用于查找 case 历史、sibling 统计）
        is_last_round: 是否末轮

    返回:
        verdicts list
    """
    verdicts = []

    # 预处理 run_state: 按 case_id 索引
    case_state_map: Dict[str, Dict[str, Any]] = {}
    sibling_map: Dict[str, Dict[str, Any]] = {}  # func_key → {"total", "passed", "failed"}
    fingerprint_history: Dict[str, List[str]] = {}  # case_id → [fp1, fp2, ...]

    if run_state:
        for fi in run_state.get("files", {}).values():
            for func_key, func_data in fi.get("functions", {}).items():
                cases = func_data.get("cases", [])
                func_stats = {"total": 0, "passed": 0, "failed": 0}
                for c in cases:
                    cid = c.get("id", "")
                    # 附加 function_key 以便查找 sibling stats
                    c_with_key = dict(c)
                    c_with_key["function_key"] = func_key
                    case_state_map[cid] = c_with_key
                    st = c.get("status", "")
                    func_stats["total"] += 1
                    if st == "passed":
                        func_stats["passed"] += 1
                    elif st in ("failed", "failed_persistent"):
                        func_stats["failed"] += 1
                    # 收集历史 fingerprint
                    hist = c.get("traceback_fingerprints", [])
                    if hist:
                        fingerprint_history[cid] = hist
                sibling_map[func_key] = func_stats

    for f in failures:
        cid = f.get("case_id", "")
        rsc = case_state_map.get(cid)
        sib = None
        # 通过 run_state 找 func_key → sibling stats
        if rsc:
            func_key = rsc.get("function_key", "")
            sib = sibling_map.get(func_key)
        hist_fps = fingerprint_history.get(cid, [])

        verdicts.append(classify_failure(
            case_id=cid,
            test_file=f.get("test_file", ""),
            test_name=f.get("name", ""),
            failure_type=f.get("failure_type"),
            traceback=f.get("traceback"),
            run_state_case=rsc,
            sibling_stats=sib,
            historical_fingerprints=hist_fps,
            is_last_round=is_last_round,
        ))
    return verdicts


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _is_top_frame_in_test(traceback: str, test_file: str) -> bool:
    """判断 traceback 顶帧（最后一个 File 行）是否在测试文件中。"""
    lines = traceback.strip().splitlines()
    for line in reversed(lines):
        m = re.search(r'File "(.*?)", line (\d+)', line)
        if m:
            filepath = m.group(1)
            if test_file and (
                filepath.endswith(test_file)
                or test_file.endswith(filepath)
                or "/test/" in filepath
                or filepath.rsplit("/", 1)[-1].startswith("test_")
            ):
                return True
            return False
    return False


def _is_top_frame_in_source(traceback: str, test_file: str) -> bool:
    """判断顶帧是否在源码文件（非测试文件）中。"""
    lines = traceback.strip().splitlines()
    for line in reversed(lines):
        m = re.search(r'File "(.*?)", line (\d+)', line)
        if m:
            filepath = m.group(1)
            if "test_" not in filepath:
                return True
            return False
    return False
