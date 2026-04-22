# `.test/run_results/<slug>.json` 字段说明

`runner.py run` 每轮输出的测试+覆盖率结构化结果。每轮覆盖写入（不追加）。

路径约定：
- 并行 sub-agent 模式：每个 sub-agent 写 `.test/run_results/<slug>.json`
  （`<slug>` 由 `dispatch.py claim` 根据源文件路径算好后一并下发），
  主 agent 用 `dispatch.py report --run-results-dir .test/run_results` 聚合。
- 全量模式：`--output .test/run_result.json`（单文件）。

并行模式下 `runner.py run` 要配合 `--test-file` 和 `--scope-sources` 使用，
把作用域锁在 sub-agent 自己的源文件上，覆盖率 summary 也只按该文件重算。

## 顶层字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `language` | string | `"python"` |
| `generated_at` | string | ISO 8601 时间戳 |
| `return_code` | int | 测试执行的 exit code |
| `tool_status` | object | 各工具的可用性，见下 |
| `md5_drifts` | array | 源文件 md5 与基线不符的清单 |
| `summary` | object | 测试通过/失败数 + 覆盖率摘要 |
| `tests` | array | 每个测试的执行结果 |
| `coverage` | object | 按文件的详细覆盖率 |
| `error` | string | 仅在早期故障时出现 |

## `tool_status`

```json
{"pytest": true, "pytest_cov": true, "coverage_json": true}
```

任一为 false 时，下游需要给出警告或降级（比如 pytest-cov 缺失→跳过覆盖率统计）。

## `md5_drifts`

```json
[
  {"path": "core/parser.py",
   "expected_md5": "abc123...",
   "actual_md5": "def456..."}
]
```

出现条目 → 源码与基线脱节，提醒用户重跑 init。

## `summary`

```json
{
  "total_tests": 120,
  "passed": 110,
  "failed": 5,
  "errors": 2,
  "skipped": 3,
  "pass_rate": 91.7,
  "coverage": {
    "statement_rate": 92.3,
    "branch_rate": 88.5,
    "function_rate": 95.0,
    "covered_statements": 420,
    "total_statements": 455,
    "covered_branches": 102,
    "total_branches": 115,
    "covered_functions": 38,
    "total_functions": 40
  }
}
```

## `tests[]`

```json
{
  "classname": "test.generated_unit.core.test_parser",
  "name": "test_parse_header_valid",
  "test_file": "test/generated_unit/core/test_parser.py",
  "case_id": "functional_01",
  "status": "passed",
  "duration_s": 0.04,
  "failure_type": null,
  "traceback": null
}
```

| 字段 | 说明 |
|---|---|
| `status` | `passed` / `failed` / `error` / `skipped` |
| `failure_type` | 异常类型（`AssertionError` / `ImportError` / `TypeError` 等），仅在失败时填 |
| `traceback` | 失败/错误时的完整 traceback 文本 |
| `case_id` | 由 CASE_ID 注释反查（见 `run-state-schema.md` 里的 "CASE_ID 约定"）；匹配不到则为 null |

## `coverage[<path>]`

按基线里的文件路径聚合（key 是相对仓库根的 POSIX 路径）：

```json
{
  "statement_rate": 85.0,
  "branch_rate": 75.0,
  "covered_statements": 34,
  "total_statements": 40,
  "covered_branches": 9,
  "total_branches": 12,
  "missed_lines": [12, 45, 67],
  "missed_branches": [[45, 0], [52, 1]],
  "functions": {
    "parse_header": {
      "line_range": [12, 45],
      "statement_rate": 90.0,
      "covered": false,
      "missed_lines": [23],
      "missed_branches": [[34, 1]]
    }
  }
}
```

- `missed_branches` 的每项 `[line, branch_index]` 表示第 `line` 行的第 `branch_index` 个分支没命中
- 函数级统计基于基线里的 `line_range` 聚合。行级数据全部落在函数区间外的话，函数层不会出现

## 典型异常

- pytest 不可用：顶层 `"error": "pytest 未安装..."`
- 覆盖率工具缺失：`summary.coverage` 各字段为 0，`tool_status.coverage_json` = false

## 覆盖率配置

### `coverage_config` 字段

这个字段在基线 `test_cases.json` 里，**由用户维护**，init 阶段只在缺失时写入默认值。本 skill 只读不写。

```json
{
  "statement_threshold": 90,
  "branch_threshold": 90,
  "function_threshold": 100,
  "exclude_dirs": ["tools", "experimental"]
}
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `statement_threshold` | 90 | 全局语句覆盖率阈值（百分比） |
| `branch_threshold` | 90 | 全局分支覆盖率阈值 |
| `function_threshold` | 100 | 函数覆盖率阈值（建议保持 100，所有函数都至少被一个测试覆盖） |
| `exclude_dirs` | `[]` | 不参与统计和补测的顶层目录 |

三项**全部**达标才算通过。任一未达标 → 走下一轮补测（最多 5 轮）。

### 迭代终止条件

同时满足三项才算达标并进入步骤 8 的"终止报告"：

- `summary.coverage.statement_rate ≥ statement_threshold`
- `summary.coverage.branch_rate ≥ branch_threshold`
- `summary.coverage.function_rate ≥ function_threshold`

强制终止（即使未达标）：

- `last_round == 5`（5 轮上限）
- 所有 gap 函数都因 `source_bug` 被永久跳过（没有可补的东西）

### Python 覆盖率细节

`runner.py run` 会调用：

```
pytest --junit-xml=... --cov=<source_dirs> --cov-branch \
       --cov-report=json:<path> --cov-report=
```

- `--cov-branch` 开启分支覆盖
- `--cov-report=json` 产出 coverage.py 的 JSON 报告（供 `runner.py run` 解析）
- `--cov-report=` 显式关掉终端输出，减少噪音

常见问题：

- **conftest.py 缺失**：测试导入源码失败。LLM 应在 `test/generated_unit/conftest.py` 放一个最小 shim：
  ```python
  import sys, pathlib
  sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
  ```
- **源目录含可执行脚本**：setup 脚本被 pytest 采到会触发 import 报错。把这种目录放进 `coverage_config.exclude_dirs`

### 排除目录的语义

`exclude_dirs` 里的路径（按 **顶层目录** 匹配）：

- 不会出现在 `analyze.py gaps` 的输出里
- 覆盖率统计会把这些文件剔除

典型需要排除的目录：`tools/` / `experimental/` / `benchmarks/` / `examples/`。

### 阈值调优建议

- **statement_threshold 默认 90**：大多数工业项目的业务逻辑层可达；核心路径可设 95
- **branch_threshold 默认 90**：比语句覆盖率更难，异常处理分支往往难以全覆盖
- **function_threshold 默认 100**：一次都没调用过的函数通常说明要么测试漏了，要么函数是死代码（后者应该删，不是测）

首次跑可以先把 `function_threshold` 降到 95 允许少量冗余函数，跑稳之后再提到 100。
