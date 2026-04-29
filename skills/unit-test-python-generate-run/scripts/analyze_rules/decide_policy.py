"""
decide_policy — 下一步决策的硬编码规则。

根据覆盖率 / verdicts / fix_attempts / escalate 状态决定 next_action：
  gen_more:         需要补充测试用例
  gen_more_with_bug: 有 source_bug 但覆盖率仍差，跳过 buggy 函数继续补测
  fix_only:         只修已有失败
  done:             覆盖率达标
  abandon:          尝试耗尽
  escalate:         发现高置信 source_code_bug

决策树（按优先级）：
  Rule 0:  有 orphaned case                                          → fix_only + fix_kind="missing_test_function"
  Rule 1:  全达标 + 无失败 + 无 orphaned                              → done
  Rule 2a: 覆盖率达标 + 剩余失败全为 source_bug                      → escalate
  Rule 2b: 末轮 + 有 source_bug + ambiguous ≥ 2                     → escalate
  Rule 2c: 有 source_bug + 覆盖率仍差                                → gen_more_with_bug
           含进度检测：两轮无新 case 且覆盖率无变化 → escalate
  Rule 3:  current_round >= max_iterations                          → abandon
  Rule 4:  全部失败均为 test_code_bug                                → fix_only
  Rule 5:  覆盖率未达标 or 有 pending                                → gen_more
  Rule 6:  有失败但无明确 verdict                                    → fix_only
  Fallback:                                                         → gen_more
"""

from typing import Dict, Any, List, Optional

# Phase 5 未上线时，使用较高的 escalate 门槛
_ESCALATE_CONFIDENCE_THRESHOLD = 0.65


def decide_next_action(
    # 覆盖率指标
    statement_rate: float,
    branch_rate: float,
    function_rate: float,
    # 阈值
    statement_threshold: float,
    branch_threshold: float,
    function_threshold: float,
    # 用例状态
    total_cases: int,
    passed: int,
    failed: int,
    source_bugs: int,
    pending: int,
    # 迭代状态
    current_round: int,
    max_iterations: int,
    # 失败 verdicts
    verdicts: Optional[List[Dict[str, Any]]] = None,
    # case 级 fix_attempts
    max_fix_attempts_per_case: int = 2,
    # 进度检测（防 gen_more_with_bug 死循环）
    previous_round_metrics: Optional[Dict[str, Any]] = None,
    # orphaned 兜底（Rule 0）
    orphaned: int = 0,
    orphaned_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """根据当前状态决定下一步行动。

    返回:
        {
            "action": "gen_more|gen_more_with_bug|fix_only|done|abandon|escalate",
            "reason": str,
            "metrics": {...},
            "thresholds_hit": [...],
            "circuit_break": bool,
            "suggested_case_ids": [...],
            "fix_kind": Optional[str],
        }
    """
    metrics = {
        "stmt": statement_rate,
        "branch": branch_rate,
        "func": function_rate,
    }
    thresholds = {
        "stmt": statement_threshold,
        "branch": branch_threshold,
        "func": function_threshold,
    }
    thresholds_hit = []
    for key, actual in metrics.items():
        target = thresholds[key]
        if actual < target:
            thresholds_hit.append(key)

    is_last_round = current_round >= max_iterations - 1

    # --- Rule 0: orphaned case 兜底（agent 端 step ④ 漏了的兜回来）---
    # 仅在还有迭代余量时触发；迭代耗尽时让 Rule 3 接管，避免死循环
    if orphaned and orphaned > 0 and current_round < max_iterations:
        return {
            "action": "fix_only",
            "reason": f"{orphaned} 个 case 仍 orphaned（agent step ④ 未处理或处理失败），"
                      "需补充缺失的 def test_xxx 函数",
            "metrics": metrics,
            "thresholds_hit": thresholds_hit,
            "circuit_break": False,
            "suggested_case_ids": orphaned_ids or [],
            "fix_kind": "missing_test_function",
        }

    # --- Rule 1: 全达标 → done ---
    if not thresholds_hit and failed == 0 and pending == 0:
        return {
            "action": "done",
            "reason": "覆盖率达标，无失败用例",
            "metrics": metrics,
            "thresholds_hit": [],
            "circuit_break": False,
            "suggested_case_ids": [],
        }

    # --- Rule 2: source_code_bug 处理（重构）---
    source_bug_ids, ambiguous_ids = [], []
    if verdicts:
        for v in verdicts:
            pv = v.get("preliminary_verdict", "")
            conf = v.get("confidence", 0)
            cid = v.get("case_id", "")
            if pv == "source_code_bug" and conf >= _ESCALATE_CONFIDENCE_THRESHOLD:
                source_bug_ids.append(cid)
            elif pv == "ambiguous":
                ambiguous_ids.append(cid)

    coverage_done = not thresholds_hit
    all_failures_are_source_bugs = (
        source_bug_ids and failed > 0 and failed <= len(source_bug_ids)
    )

    # 2a: 覆盖率已达标 + 剩余失败全为 source_bug → escalate
    if all_failures_are_source_bugs and coverage_done:
        return {
            "action": "escalate",
            "reason": f"覆盖率达标 + 剩余失败全为 source_bug: {source_bug_ids}",
            "metrics": metrics,
            "thresholds_hit": thresholds_hit,
            "circuit_break": False,
            "suggested_case_ids": [],
        }

    # 2b: 末轮 escalate 窗口：ambiguous 失败 ≥ 2 时放宽
    if is_last_round and source_bug_ids and len(ambiguous_ids) >= 2:
        return {
            "action": "escalate",
            "reason": f"末轮窗口: {len(source_bug_ids)} 个 source_bug + {len(ambiguous_ids)} 个 ambiguous",
            "metrics": metrics,
            "thresholds_hit": thresholds_hit,
            "circuit_break": False,
            "suggested_case_ids": [],
        }

    # 2c: 有 source_bug 但覆盖率仍差 → gen_more_with_bug，含进度检测
    if source_bug_ids and (thresholds_hit or pending > 0):
        # 进度检测：若上一轮已是 gen_more_with_bug 状态且本轮 metrics 没改善
        if previous_round_metrics:
            prev_passed = previous_round_metrics.get("passed", -1)
            prev_total = previous_round_metrics.get("total_cases", -1)
            prev_stmt = previous_round_metrics.get("stmt", -1)
            no_new_cases = (passed == prev_passed and total_cases == prev_total)
            no_cov_gain = abs(statement_rate - prev_stmt) < 1.5
            if no_new_cases and no_cov_gain and pending == 0:
                return {
                    "action": "escalate",
                    "reason": (f"已登记 {len(source_bug_ids)} 个 source_bug 后无法继续补测："
                               f"连续两轮无进展，剩余覆盖率 gap 全部落在 buggy 函数内"),
                    "metrics": metrics,
                    "thresholds_hit": thresholds_hit,
                    "circuit_break": True,
                    "suggested_case_ids": [],
                }
        return {
            "action": "gen_more_with_bug",
            "reason": f"已登记 {len(source_bug_ids)} 个 source_bug；覆盖率仍差，继续补测其他函数",
            "metrics": metrics,
            "thresholds_hit": thresholds_hit,
            "circuit_break": False,
            "suggested_case_ids": [],
            "skip_case_ids": source_bug_ids,
        }

    # --- Rule 3: 迭代耗尽 → abandon ---
    if current_round >= max_iterations:
        reasons = []
        if thresholds_hit:
            reasons.append(
                f"覆盖率未达标: {', '.join(f'{k}={v}%' for k, v in metrics.items() if k in thresholds_hit)}"
            )
        if failed:
            reasons.append(f"{failed} 个用例失败")
        return {
            "action": "abandon",
            "reason": f"迭代耗尽 (round={current_round}/{max_iterations})。"
                      + ("；".join(reasons) if reasons else ""),
            "metrics": metrics,
            "thresholds_hit": thresholds_hit,
            "circuit_break": False,
            "suggested_case_ids": [],
        }

    # --- Rule 4: 有失败但全部 test_code_bug + fix_attempts 未耗尽 → fix_only ---
    test_bug_ids = []
    if verdicts:
        for v in verdicts:
            if v.get("preliminary_verdict") == "test_code_bug":
                test_bug_ids.append(v.get("case_id", ""))

    if failed > 0 and len(test_bug_ids) >= failed and current_round < max_iterations:
        return {
            "action": "fix_only",
            "reason": f"{failed} 个失败（{len(test_bug_ids)} 个判断为 test_code_bug），修测试",
            "metrics": metrics,
            "thresholds_hit": thresholds_hit,
            "circuit_break": False,
            "suggested_case_ids": test_bug_ids,
        }

    # --- Rule 5: 覆盖率未达标 or 有 pending → gen_more ---
    if thresholds_hit or pending > 0:
        gaps_desc = []
        for key in thresholds_hit:
            actual = metrics[key]
            target = thresholds[key]
            gaps_desc.append(f"{key}={actual}% < {target}%")
        if pending:
            gaps_desc.append(f"{pending} 个 pending case")
        return {
            "action": "gen_more",
            "reason": "；".join(gaps_desc),
            "metrics": metrics,
            "thresholds_hit": thresholds_hit,
            "circuit_break": False,
            "suggested_case_ids": [],
        }

    # --- Rule 6: 有失败但无明确 verdict → fix_only ---
    if failed > 0:
        return {
            "action": "fix_only",
            "reason": f"{failed} 个用例失败，需修复",
            "metrics": metrics,
            "thresholds_hit": thresholds_hit,
            "circuit_break": False,
            "suggested_case_ids": [],
        }

    # --- Fallback: 继续生成 ---
    return {
        "action": "gen_more",
        "reason": "默认继续生成",
        "metrics": metrics,
        "thresholds_hit": thresholds_hit,
        "circuit_break": False,
        "suggested_case_ids": [],
    }
