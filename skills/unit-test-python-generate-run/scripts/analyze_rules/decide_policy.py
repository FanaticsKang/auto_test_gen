"""
decide_policy — 下一步决策的硬编码规则（Phase 2 重写）。

根据覆盖率 / verdicts / fix_attempts / escalate 状态决定 next_action：
  gen_more:   需要补充测试用例
  fix_only:   只修已有失败
  done:       覆盖率达标
  abandon:    尝试耗尽
  escalate:   发现高置信 source_code_bug

Phase 2 变更：
  - escalate 门槛：有 ≥1 高置信 source_bug + 非source_bug 失败中 ≥50% 已尝试修复
  - 末轮 escalate 窗口：current_round == max_iterations - 1 时放宽条件
  - 无 Phase 5 复核时，escalate 门槛临时用 confidence >= 0.65
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
) -> Dict[str, Any]:
    """根据当前状态决定下一步行动。

    返回:
        {
            "action": "gen_more|fix_only|done|abandon|escalate",
            "reason": str,
            "metrics": {...},
            "thresholds_hit": [...],
            "circuit_break": bool,
            "suggested_case_ids": [...],
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

    # --- Rule 2: source_code_bug escalate（必须在 abandon 之前）---
    source_bug_ids = []
    ambiguous_ids = []
    if verdicts:
        for v in verdicts:
            pv = v.get("preliminary_verdict", "")
            conf = v.get("confidence", 0)
            cid = v.get("case_id", "")
            if pv == "source_code_bug" and conf >= _ESCALATE_CONFIDENCE_THRESHOLD:
                source_bug_ids.append(cid)
            elif pv == "ambiguous":
                ambiguous_ids.append(cid)
    elif source_bugs > 0:
        # 无 verdicts 但 run_state 有 source_bug 记录 → escalate
        return {
            "action": "escalate",
            "reason": f"run_state 记录 {source_bugs} 个 source_bug",
            "metrics": metrics,
            "thresholds_hit": thresholds_hit,
            "circuit_break": False,
            "suggested_case_ids": [],
        }

    if source_bug_ids:
        # 所有失败都是 source_bug → 直接 escalate
        if failed <= len(source_bug_ids):
            return {
                "action": "escalate",
                "reason": f"高置信 source_code_bug: {source_bug_ids}",
                "metrics": metrics,
                "thresholds_hit": thresholds_hit,
                "circuit_break": False,
                "suggested_case_ids": [],
            }

        # 末轮 escalate 窗口：ambiguous 失败 ≥ 2 时放宽
        if is_last_round and len(ambiguous_ids) >= 2:
            return {
                "action": "escalate",
                "reason": f"末轮窗口: {len(source_bug_ids)} 个 source_bug + {len(ambiguous_ids)} 个 ambiguous",
                "metrics": metrics,
                "thresholds_hit": thresholds_hit,
                "circuit_break": False,
                "suggested_case_ids": [],
            }

        # 有确认 source_bug 但非末轮/无足够 ambiguous → 仍 escalate（不丢弃）
        return {
            "action": "escalate",
            "reason": f"发现 {len(source_bug_ids)} 个高置信 source_code_bug: {source_bug_ids}",
            "metrics": metrics,
            "thresholds_hit": thresholds_hit,
            "circuit_break": False,
            "suggested_case_ids": [],
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
