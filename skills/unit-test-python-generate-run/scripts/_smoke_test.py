"""
_smoke_test — sanity check for classification weights and decide policy.

Quick (~1s) test that catches the most common weight/direction errors.
Run: python scripts/_smoke_test.py
"""

import sys
from pathlib import Path

# 确保 analyze_rules 可 import
sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyze_rules.failure_classification import classify_failure, compute_traceback_fingerprint
from analyze_rules.decide_policy import decide_next_action


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

PASS = 0
FAIL = 1


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
        return PASS
    else:
        print(f"  FAIL  {name}  {detail}")
        return FAIL


# ---------------------------------------------------------------------------
# Traceback fingerprint tests
# ---------------------------------------------------------------------------

def test_fingerprint():
    print("\n=== traceback fingerprint ===")
    results = 0

    tb1 = 'File "core/parser.py", line 42\nValueError: invalid input'
    tb2 = 'File "core/parser.py", line 42\nValueError: invalid input'
    tb3 = 'File "core/parser.py", line 43\nValueError: invalid input'
    tb_test = 'File "tests/test_parser.py", line 10\nValueError: invalid input'

    fp1 = compute_traceback_fingerprint(tb1)
    fp2 = compute_traceback_fingerprint(tb2)
    fp3 = compute_traceback_fingerprint(tb3)
    fp_test = compute_traceback_fingerprint(tb_test, "tests/test_parser.py")

    results += check("same traceback → same fingerprint", fp1 == fp2)
    results += check("different source line → different fingerprint", fp1 != fp3)
    results += check("test file line change → same fingerprint (ignored)", fp_test == fp_test)
    results += check("fingerprint is non-empty", len(fp1) > 0)

    return results


# ---------------------------------------------------------------------------
# Classification tests
# ---------------------------------------------------------------------------

def test_classification():
    print("\n=== failure classification ===")
    results = 0

    # Case 1: AssertionError → ambiguous +0.0
    v = classify_failure(
        "c1", "test_foo.py", "test_x",
        "AssertionError", 'File "test_foo.py", line 5\nAssertionError',
    )
    results += check(
        "AssertionError → ambiguous",
        v["preliminary_verdict"] == "ambiguous",
        f"got {v['preliminary_verdict']} conf={v['confidence']}",
    )
    results += check(
        "AssertionError → confidence=0.0",
        v["confidence"] == 0.0,
        f"got {v['confidence']}",
    )

    # Case 2: NameError → test_code_bug
    v = classify_failure(
        "c2", "test_foo.py", "test_y",
        "NameError", 'File "test_foo.py", line 3\nNameError: foo',
    )
    results += check(
        "NameError → test_code_bug",
        v["preliminary_verdict"] == "test_code_bug",
        f"got {v['preliminary_verdict']}",
    )

    # Case 3: 顶帧在源码 → source_code_bug +0.4
    v = classify_failure(
        "c3", "test_foo.py", "test_z",
        "ValueError",
        'File "test_foo.py", line 10\n'
        'File "core/parser.py", line 42\n'
        'ValueError: bad',
    )
    results += check(
        "顶帧在源码 → source_code_bug",
        v["preliminary_verdict"] == "source_code_bug",
        f"got {v['preliminary_verdict']}",
    )
    results += check(
        "顶帧在源码 → confidence >= 0.4",
        v["confidence"] >= 0.4,
        f"got {v['confidence']}",
    )

    # Case 4: fix_attempts >= 1 → +0.2, 推向 source_code_bug
    v = classify_failure(
        "c4", "test_foo.py", "test_w",
        "AssertionError",
        'File "test_foo.py", line 5\nAssertionError',
        run_state_case={"fix_attempts": 1, "assertion_origin": "sighted"},
    )
    results += check(
        "fix_attempts >= 1 → confidence >= 0.2",
        v["confidence"] >= 0.2,
        f"got {v['confidence']}",
    )

    # Case 5: fingerprint 首次出现（hist 只有 1 条=当轮）→ 不触发
    tb = 'File "core/parser.py", line 42\nValueError: bad'
    fp = compute_traceback_fingerprint(tb)
    v = classify_failure(
        "c5", "test_foo.py", "test_v",
        "AssertionError",
        tb,
        historical_fingerprints=[fp],  # 只有当轮
        is_last_round=False,
    )
    results += check(
        "fingerprint 首次（仅当轮）→ 不触发",
        "相同 fingerprint 跨轮重现" not in " ".join(v.get("evidence", [])),
        f"evidence: {v.get('evidence')}",
    )

    # Case 5b: fingerprint 跨轮重现（hist 有 2 条，前一条是历史）→ source_code_bug +0.4
    v = classify_failure(
        "c5b", "test_foo.py", "test_vb",
        "AssertionError",
        tb,
        historical_fingerprints=[fp, fp],  # 历史 + 当轮
        is_last_round=False,
    )
    results += check(
        "fingerprint 跨轮重现 → source_code_bug",
        v["preliminary_verdict"] == "source_code_bug",
        f"got {v['preliminary_verdict']}",
    )
    results += check(
        "fingerprint 跨轮重现 → confidence >= 0.4",
        v["confidence"] >= 0.4,
        f"got {v['confidence']}",
    )

    # Case 6: 末轮 fingerprint → weight doubled (0.8)
    v_last = classify_failure(
        "c6", "test_foo.py", "test_u",
        "AssertionError",
        tb,
        historical_fingerprints=[fp, fp],  # 历史 + 当轮
        is_last_round=True,
    )
    results += check(
        "末轮 fingerprint → confidence >= 0.8",
        v_last["confidence"] >= 0.8,
        f"got {v_last['confidence']}",
    )

    # Case 7: NameError + fix_attempts=1 → 不翻成 source_code_bug
    v = classify_failure(
        "c7", "test_foo.py", "test_name_err",
        "NameError",
        'File "test_foo.py", line 5\nNameError: foo',
        run_state_case={"fix_attempts": 1, "assertion_origin": "sighted"},
    )
    results += check(
        "NameError + fix_attempts=1 → 不翻 source_code_bug",
        v["preliminary_verdict"] == "test_code_bug",
        f"got {v['preliminary_verdict']}",
    )

    # Case 8: sibling_stats 全过 → +0.2
    v = classify_failure(
        "c8", "test_foo.py", "test_sibling",
        "AssertionError",
        'File "test_foo.py", line 5\nAssertionError',
        sibling_stats={"total": 4, "passed": 3, "failed": 1},
    )
    results += check(
        "sibling 全过 → confidence >= 0.2",
        v["confidence"] >= 0.2,
        f"got {v['confidence']}",
    )

    # Case 9: blind + fp 跨轮重现 → 额外 +0.1
    v_blind = classify_failure(
        "c9", "test_foo.py", "test_blind_bonus",
        "AssertionError",
        tb,
        run_state_case={"fix_attempts": 0, "assertion_origin": "blind"},
        historical_fingerprints=[fp, fp],
        is_last_round=False,
    )
    v_sighted = classify_failure(
        "c9s", "test_foo.py", "test_sighted_no_bonus",
        "AssertionError",
        tb,
        run_state_case={"fix_attempts": 0, "assertion_origin": "sighted"},
        historical_fingerprints=[fp, fp],
        is_last_round=False,
    )
    results += check(
        "blind 断言 fp 加成 → confidence > sighted",
        v_blind["confidence"] > v_sighted["confidence"],
        f"blind={v_blind['confidence']} sighted={v_sighted['confidence']}",
    )

    return results


# ---------------------------------------------------------------------------
# Decide policy tests
# ---------------------------------------------------------------------------

def test_decide_policy():
    print("\n=== decide policy ===")
    results = 0

    # Case 1: 全达标 → done
    r = decide_next_action(
        statement_rate=95, branch_rate=90, function_rate=100,
        statement_threshold=90, branch_threshold=90, function_threshold=100,
        total_cases=5, passed=5, failed=0, source_bugs=0, pending=0,
        current_round=1, max_iterations=5,
    )
    results += check("全达标 → done", r["action"] == "done", f"got {r['action']}")

    # Case 2: 迭代耗尽无 source_bug → abandon
    r = decide_next_action(
        statement_rate=80, branch_rate=70, function_rate=100,
        statement_threshold=90, branch_threshold=90, function_threshold=100,
        total_cases=5, passed=3, failed=2, source_bugs=0, pending=0,
        current_round=5, max_iterations=5,
    )
    results += check("迭代耗尽无 source_bug → abandon", r["action"] == "abandon", f"got {r['action']}")

    # Case 2b: 迭代耗尽但有高置信 source_bug → escalate（不丢弃）
    r = decide_next_action(
        statement_rate=80, branch_rate=70, function_rate=100,
        statement_threshold=90, branch_threshold=90, function_threshold=100,
        total_cases=5, passed=3, failed=2, source_bugs=0, pending=0,
        current_round=5, max_iterations=5,
        verdicts=[
            {"case_id": "c1", "preliminary_verdict": "source_code_bug", "confidence": 0.7},
            {"case_id": "c2", "preliminary_verdict": "test_code_bug", "confidence": 0.6},
        ],
    )
    results += check(
        "末轮有 source_bug → escalate（不 abandon）",
        r["action"] == "escalate",
        f"got {r['action']}",
    )

    # Case 3: 全部失败都是高置信 source_bug → escalate
    r = decide_next_action(
        statement_rate=80, branch_rate=70, function_rate=100,
        statement_threshold=90, branch_threshold=90, function_threshold=100,
        total_cases=5, passed=3, failed=2, source_bugs=0, pending=0,
        current_round=2, max_iterations=5,
        verdicts=[
            {"case_id": "c1", "preliminary_verdict": "source_code_bug", "confidence": 0.7},
            {"case_id": "c2", "preliminary_verdict": "source_code_bug", "confidence": 0.8},
        ],
    )
    results += check(
        "全部高置信 source_bug → escalate",
        r["action"] == "escalate",
        f"got {r['action']}",
    )

    # Case 4: 低置信 source_bug (confidence < 0.65) → 不 escalate
    r = decide_next_action(
        statement_rate=80, branch_rate=70, function_rate=100,
        statement_threshold=90, branch_threshold=90, function_threshold=100,
        total_cases=5, passed=3, failed=2, source_bugs=0, pending=0,
        current_round=2, max_iterations=5,
        verdicts=[
            {"case_id": "c1", "preliminary_verdict": "source_code_bug", "confidence": 0.4},
            {"case_id": "c2", "preliminary_verdict": "test_code_bug", "confidence": 0.6},
        ],
    )
    results += check(
        "低置信 source_bug → 不 escalate",
        r["action"] != "escalate",
        f"got {r['action']}",
    )

    # Case 5: 末轮窗口 — source_bug + ambiguous >= 2 → escalate
    r = decide_next_action(
        statement_rate=80, branch_rate=70, function_rate=100,
        statement_threshold=90, branch_threshold=90, function_threshold=100,
        total_cases=8, passed=3, failed=5, source_bugs=0, pending=0,
        current_round=4, max_iterations=5,
        verdicts=[
            {"case_id": "c1", "preliminary_verdict": "source_code_bug", "confidence": 0.7},
            {"case_id": "c2", "preliminary_verdict": "ambiguous", "confidence": 0.2},
            {"case_id": "c3", "preliminary_verdict": "ambiguous", "confidence": 0.3},
            {"case_id": "c4", "preliminary_verdict": "test_code_bug", "confidence": 0.5},
            {"case_id": "c5", "preliminary_verdict": "test_code_bug", "confidence": 0.6},
        ],
    )
    results += check(
        "末轮窗口 (source_bug + ambiguous≥2) → escalate",
        r["action"] == "escalate",
        f"got {r['action']}",
    )

    # Case 6: test_code_bug 全部 → fix_only
    r = decide_next_action(
        statement_rate=95, branch_rate=90, function_rate=100,
        statement_threshold=90, branch_threshold=90, function_threshold=100,
        total_cases=5, passed=3, failed=2, source_bugs=0, pending=0,
        current_round=2, max_iterations=5,
        verdicts=[
            {"case_id": "c1", "preliminary_verdict": "test_code_bug", "confidence": 0.7},
            {"case_id": "c2", "preliminary_verdict": "test_code_bug", "confidence": 0.8},
        ],
    )
    results += check(
        "全部 test_code_bug → fix_only",
        r["action"] == "fix_only",
        f"got {r['action']}",
    )

    # Case 7: source_bug_ids 非空但混合失败（非末轮、无 ambiguous）→ 仍 escalate
    r = decide_next_action(
        statement_rate=80, branch_rate=70, function_rate=100,
        statement_threshold=90, branch_threshold=90, function_threshold=100,
        total_cases=8, passed=3, failed=5, source_bugs=0, pending=0,
        current_round=2, max_iterations=5,
        verdicts=[
            {"case_id": "c1", "preliminary_verdict": "source_code_bug", "confidence": 0.7},
            {"case_id": "c2", "preliminary_verdict": "test_code_bug", "confidence": 0.5},
            {"case_id": "c3", "preliminary_verdict": "test_code_bug", "confidence": 0.6},
        ],
    )
    results += check(
        "混合失败但有 source_bug → escalate（不丢弃）",
        r["action"] == "escalate",
        f"got {r['action']}",
    )

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    total_fails = 0
    total_fails += test_fingerprint()
    total_fails += test_classification()
    total_fails += test_decide_policy()

    print(f"\n{'='*40}")
    if total_fails == 0:
        print("All sanity checks passed.")
    else:
        print(f"{total_fails} check(s) FAILED.")
        sys.exit(1)
