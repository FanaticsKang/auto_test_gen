# `test_run_state.json` 字段说明

运行状态文件，存放所有 cases 及本 skill 的执行历史。**由本 skill 独占写入，基线永远只读**。

路径：`test/generated_unit/test_run_state.json`

并行 sub-agent 模式下，每个 sub-agent 先写到自己的 shard
`.test/state_shards/<slug>.json`，主 agent 在所有 sub-agent 结束后用
`analyze.py merge-state` 合并成这个最终文件。shard 的字段集和最终文件完全一致，
只是 `files` 里通常只有一个源文件条目。

## 顶层字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `version` | string | 当前固定 `"1.0"` |
| `baseline_ref` | string | 引用的基线文件路径 |
| `baseline_version` | string | 基线的版本号快照（便于发现基线格式不兼容） |
| `generated_at` | string | 最后一次更新的 ISO 8601 时间戳 |
| `last_round` | int | 最后一次执行的轮数 |
| `summary` | object | 全局摘要，见下 |
| `rounds` | array | 每轮的元数据快照，见下 |
| `files` | object | 每个源文件的 cases 和状态 |

## `summary`

```json
{
  "total_cases": 120,
  "passed": 110,
  "failed": 5,
  "source_bugs": 3,
  "coverage": {
    "statement_rate": 92.3,
    "branch_rate": 88.5,
    "function_rate": 95.0
  }
}
```

`analyze.py update-state` 刷新本分片内的 `total_cases`；`analyze.py merge-state`
合并所有 shard 时会重新汇总 `total_cases` / `passed` / `failed` / `source_bugs`。
`coverage` 字段的真实数据来源是 `.test/run_results/*.json`（由
`dispatch.py report --run-results-dir` 聚合），不需要在 run_state 里再存一份。

## `rounds[]`

每轮一条，追加写入：

```json
{
  "round": 2,
  "started_at": "2026-04-21T10:12:00",
  "ended_at": "2026-04-21T10:18:43",
  "duration_s": 403,
  "new_cases": 24,
  "passed": 20,
  "failed": 4,
  "fixed_after_llm": 3,
  "recorded_source_bugs": 1,
  "coverage_after": {
    "statement_rate": 88.5, "branch_rate": 80.2, "function_rate": 95.0
  },
  "threshold_met": false
}
```

## `files[<src>]`

结构镜像基线的 `files[<src>]`，但**只存与运行状态相关的字段**。key 与基线完全一致。

| 字段 | 类型 | 说明 |
|---|---|---|
| `file_md5_at_gen` | string | 生成 cases 时基线里该文件的 md5 快照；和当前基线对比可检测漂移 |
| `test_path` | string | 对应测试文件路径（可能由 LLM 在 patch 中刷新） |
| `functions` | object | 函数 key → 函数运行状态 |

## `files[<src>].functions[<key>]`

| 字段 | 类型 | 说明 |
|---|---|---|
| `func_md5_at_gen` | string | 生成该函数 cases 时基线里的 `func_md5` 快照 |
| `cases` | array | case 对象数组（字段见下方"case 字段"） |

## MD5 漂移处理

- `file_md5_at_gen` vs 基线当前 `file_md5` 不一致 → 基线在 init 层已经换了；`runner.py run` 会在 `md5_drifts` 字段报警
- `func_md5_at_gen` vs 基线当前 `func_md5` 不一致 → 对应函数的 cases 可能已失效，建议 LLM 下轮重新生成该函数的所有 cases

本 skill 不强制清空历史 cases，留给 LLM 判断。用户可在步骤 1 开始前手动删除 run_state 重来。

## `files[<src>].functions[<key>].cases[]` 字段

每个测试用例存放在函数的 `cases` 数组中。

| 字段 | 类型 | 必需 | 说明 |
|---|---|---|---|
| `id` | string | 是 | 用例唯一 ID（同一函数内唯一）。建议 `<dimension>_<nn>`，如 `functional_01`、`boundary_03`、`exception_02` |
| `dimension` | string | 是 | 所属维度：`functional` / `boundary` / `exception` / `data_integrity` / `performance` / `security` |
| `description` | string | 是 | 一句话说明测什么（便于人阅读和 LLM 下轮决策） |
| `test_name` | string | 是 | 生成的测试函数名，供失败定位使用 |
| `inputs` | object | 可选 | 结构化输入描述 |
| `expected` | any | 可选 | 期望输出描述 |
| `status` | string | 是 | 状态枚举，见下 |
| `round_added` | int | 是 | 在第几轮被加入（由 `analyze.py update-state` 自动填） |
| `round_last_run` | int | 可选 | 最后一次执行时的轮数 |
| `failure_reason` | string \| null | 可选 | LLM 分类结果：`test_code_bug` / `source_code_bug` / `ambiguous` / null |
| `fix_attempts` | int | 可选 | 测试代码修复次数，超过 3 自动熔断 |

### status 枚举

| 值 | 含义 |
|---|---|
| `pending` | 刚生成，尚未执行 |
| `passed` | 执行通过 |
| `failed` | 失败，尚未分类 |
| `fixed_pending_rerun` | LLM 判为测试代码 bug，已修测试代码等待重跑 |
| `source_bug` | LLM 判为源代码 bug，已落盘 `.test/source_bugs.json`，不再重跑 |
| `failed_persistent` | 修复 3 次仍失败，已按 source_bug 登记 |
| `skipped` | pytest 明确 skip（pytest.mark.skip） |

### CASE_ID 约定

每个生成的测试函数上方**必须**带一行 `CASE_ID` 注释。`runner.py run` 会扫描目标测试目录（或 `--test-file` 的父目录），为每个测试建立 `test_name → case_id` 映射。

```python
# CASE_ID: functional_01
def test_parse_header_valid():
    ...
```

参数化测试只写一次即可（多次匹配同一 id）：

```python
# CASE_ID: boundary_01
@pytest.mark.parametrize("value", [0, 1, -1])
def test_parse_header_boundary(value):
    ...
```

### case 完整示例

```json
{
  "id": "boundary_02",
  "dimension": "boundary",
  "description": "空字节串输入应触发 ValueError",
  "test_name": "test_parse_header_empty_buffer",
  "inputs": {"data": "b''"},
  "expected": "ValueError 被抛出",
  "status": "passed",
  "round_added": 1,
  "round_last_run": 2,
  "failure_reason": null,
  "fix_attempts": 1
}
```

## 示例（节选）

```json
{
  "version": "1.0",
  "baseline_ref": "test/generated_unit/test_cases.json",
  "baseline_version": "1.0",
  "generated_at": "2026-04-21T10:18:43",
  "last_round": 2,
  "summary": {
    "total_cases": 120, "passed": 110, "failed": 5, "source_bugs": 3,
    "coverage": {"statement_rate": 92.3, "branch_rate": 88.5, "function_rate": 95.0}
  },
  "rounds": [
    {"round": 1, "new_cases": 96, "passed": 88, "failed": 8, "duration_s": 520,
     "coverage_after": {"statement_rate": 78.0, "branch_rate": 70.0, "function_rate": 90.0},
     "threshold_met": false},
    {"round": 2, "new_cases": 24, "passed": 22, "failed": 2, "duration_s": 403,
     "coverage_after": {"statement_rate": 92.3, "branch_rate": 88.5, "function_rate": 95.0},
     "threshold_met": false}
  ],
  "files": {
    "core/parser.py": {
      "file_md5_at_gen": "abc123...",
      "test_path": "test/generated_unit/core/test_parser.py",
      "functions": {
        "parse_header": {
          "func_md5_at_gen": "def456...",
          "cases": [
            {"id": "functional_01", "dimension": "functional",
             "description": "有效输入返回 Header", "test_name": "test_parse_header_valid",
             "status": "passed", "round_added": 1, "round_last_run": 2,
             "failure_reason": null, "fix_attempts": 0}
          ]
        }
      }
    }
  }
}
```
