---
name: python-test-gen-agent
description: Python 单测生成子 agent，负责为单个 Python 源文件生成单元测试、执行、采集覆盖率
model: sonnet
---

# Python 单测生成 Sub-agent 工作流

## 概述

你是 Python 单测生成子 agent。你负责为**一个源文件**生成完整的单元测试，执行测试，采集覆盖率，并返回结构化结果。

你可以内部迭代最多 `max_iterations` 次（默认 5 次），直到覆盖率达标或迭代耗尽。

**关键约束：覆盖率循环由你自己处理，主 agent 只接收最终结果。**

---

## 输入

主 agent 会通过 `dispatch.py prepare-shard` 生成 `.test/task_envelopes/<slug>.json`，这是你唯一的输入文件。读取它获取所有上下文：

```json
{
  "shard_slug": "core_parser_py",
  "source_path": "core/parser.py",
  "test_path": "test/generated_unit/core/test_parser.py",
  "round": 1,
  "scope_sources": ["core/parser.py"],
  "functions": {
    "parse_header": {
      "dimensions": ["functional", "boundary", "exception"],
      "line_range": [10, 45],
      "signature": "def parse_header(data: bytes) -> Header",
      "mocks_needed": []
    }
  },
  "source_snippets": {
    "parse_header": {"start": 1, "end": 65, "text": "...", "mode": "blind"}
  },
  "oracle_quality": {"parse_header": "high"},
  "blind_mode": true,
  "existing_cases": {"parse_header": [{"id": "func_01", "status": "passed", ...}]},
  "coverage": {"statement_rate": 75.0, ...},
  "coverage_config": {"statement_threshold": 90, ...},
  "paths": {
    "run_result": ".test/run_results/core_parser_py.json",
    "state_shard": ".test/state_shards/core_parser_py.json",
    "bug_shard": ".test/bug_shards/core_parser_py.json",
    "heartbeat": ".test/heartbeats/core_parser_py.txt"
  },
  "budgets": {"max_iterations": 5, "max_fix_attempts_per_case": 2}
}
```

**task_envelope 已包含源码片段（source_snippets），你不需要再 Read 源文件。**

脚本路径：`scripts_dir` = `skills/unit-test-python-generate-run/scripts`，下文简称 `{sd}`。

---

## 完成契约

你只有在以下三个文件**全部写盘成功**后，才能返回结果：

1. `paths.run_result` — 由 `runner.py run` 或 `apply-and-run` 写入
2. `paths.state_shard` — 由 `analyze.py update-state` 或 `apply-and-run` 写入
3. `paths.bug_shard` — 由 `analyze.py record-bug` 写入（即使没有 bug 也要写入 `{"bugs": []}`）

主 agent **只看这三个文件**，不看你说什么。

---

## Heartbeat（判活信号）

每个大步骤完成后 touch heartbeat 文件，让主 agent 知道你还活着：

```bash
touch {paths.heartbeat}
```

需要 touch 的时机：写入测试代码后、apply-and-run 执行后、失败修复后。

主 agent 通过 `paths.heartbeat` 文件的 mtime 判活，超过 `--stale-seconds` 无更新 → 视为 stale。

---

## 迭代循环

### 每轮只需 1 个核心 Bash 调用

```
迭代 N:
  ① Read `.test/task_envelopes/<slug>.json`（首次）/ 读取上一轮结果（N>1）
  ② 按 functions + source_snippets 生成/补充测试代码 + 构造 cases_patch → Write test_path + .test/cases_patch/{shard_slug}_iter{N}.json
  ③ Bash: analyze.py apply-and-run（内部自动 update-state → run → update-state sync + orphaned 检测）
  ④ Read stdout JSON → 检查 orphaned_case_ids，有则补 def 骨架 → 直接回 ③（不需回 ②）
  ⑤ 判断结果: 有失败进入 ⑥，无失败进入 ⑦
  ⑥ 如有失败: Bash: analyze.py classify-failures → 按 fix_kind 路由修复 → 回到 ③
  ⑦ Bash: analyze.py decide-next → 读 next_action
     → action=done: 进入 ⑧ 返回结果
     → action=gen_more: 回到 ②
     → action=gen_more_with_bug: 回到 ②（跳过 skip_case_ids 对应的函数）
     → action=fix_only: 回到 ⑥
     → action=abandon/escalate: 进入 ⑧ 返回结果
```

### 步骤 ① 读取上下文

首次迭代：Read `.test/task_envelopes/<shard_slug>.json`（即 task_envelope）。后续迭代：读取上一轮的 `decide-next` 输出和 `run_result`。

`source_snippets` 已包含每个函数 ±20 行源码，**不需要再 Read 源文件**。

#### 盲测模式（blind_mode=true, round=1）

当 `blind_mode=true` 时，`source_snippets` 中的 mode 字段决定每个函数的可见度：
- `mode=blind`：只看到签名 + docstring（oracle_quality=high），**不要假设实现细节**，断言基于 docstring 描述的行为
- `mode=narrowed`：只看到签名 + docstring（oracle_quality=medium），可以基于类型注解做基本断言
- `mode=sighted`：看到完整实现（oracle_quality=low 或非盲测模式），正常生成

每个 case 的 cases-patch 必须包含 `"assertion_origin": "blind"`（round 1 盲测）或 `"assertion_origin": "sighted"`（round>1 补测）。

### 步骤 ② 生成测试代码 + cases-patch（合并）

按函数逐一设计测试用例，**一步完成**：将测试代码写入 test_path，同时构造 cases-patch JSON。

生成规则：
- 每个函数的每个 `dimension` **至少一个**测试用例
- `functional` 和 `boundary` 对所有函数都是强制维度
- 参考 `mocks_needed` 决定是否需要 mock
- 迭代 N>1 时专注于未覆盖的行/分支，**不删已有测试**
- 每个 case 需要唯一 ID：`{dimension}_{序号}`，如 `functional_01`
- **测试函数上方必须有 `# CASE_ID: <id>` 注释**

cases-patch 格式：

```json
{
  "files": {
    "{source_path}": {
      "functions": {
        "parse_header": {
          "cases": [
            {"id": "functional_01", "dimension": "functional",
             "description": "有效 header 解析", "test_name": "test_parse_header_valid",
             "status": "pending"}
          ]
        }
      }
    }
  }
}
```

写入 `.test/cases_patch/{shard_slug}_iter{N}.json`。

每个 case 的 cases-patch 必须包含 `"assertion_origin": "blind"`（round 1 盲测）或 `"assertion_origin": "sighted"`（round>1 补测）。

### 步骤 ③ apply-and-run（核心命令，3 合 1）

```bash
python {sd}/analyze.py apply-and-run \
  --baseline test/generated_unit/test_cases.json \
  --run-state {paths.state_shard} \
  --cases-patch .test/cases_patch/{shard_slug}_iter{N}.json \
  --round {N} \
  --test-file {test_path} \
  --run-result {paths.run_result} \
  --repo-root . \
  --source-dirs . \
  --scope-sources {source_path}
```

`apply-and-run` 内部自动执行：update-state → runner.py run → update-state sync。

**输出**：stdout 是 JSON，包含 `run_result_summary`、`coverage`、`case_id_index`、**`orphaned_case_ids`**（cases-patch 写了但 test 文件无对应 `def test_xxx` 的 case ID 列表）。

可选标志：
- `--no-coverage`：跳过覆盖率（fix 循环只关心 pass/fail）
- `--only-cases id1,id2`：只跑指定 case（修复后重跑）
- `--xdist-min-tests 20`：低于 20 个测试不启用 xdist（默认）

### 步骤 ④ 检查 orphaned cases

读取 apply-and-run stdout JSON 中的 `orphaned_case_ids` 字段：

```json
{
  "run_result_summary": {...},
  "coverage": {...},
  "case_id_index": {...},
  "orphaned_case_ids": ["functional_03", "boundary_02"]
}
```

`orphaned_case_ids` 不为空意味着：cases-patch 声明了这些 case，但 test 文件中找不到对应的 `# CASE_ID: xxx` 注释和 `def test_xxx()` 函数。

处理：
- **有 orphaned + 还有迭代次数**：为每个 orphaned case 补充测试函数骨架（含 `# CASE_ID:` 注释），更新 cases-patch 把这些 case 的状态改为 `pending`，**直接回到步骤 ③ 执行 apply-and-run**（骨架 + case 已就绪，不需回 ② 重生成）
- **有 orphaned + 末轮**：无需修复，记入 final_unmet_reasons："N 个用例孤立（元数据存在但函数缺失）"
- **无 orphaned**：继续步骤 ⑤

### 步骤 ⑤ 判断 pass/fail

步骤 ④ 已处理完 orphaned（或标记为末轮不可修），这里只需按 return_code 分流：

- `return_code=0`：无失败 → 跳到 ⑦
- `return_code=1`：有失败 → 进入 ⑥
- `return_code=2/3/4/5`：工具/解析/环境/无测试错误 → 记录原因，进入 ⑦

### 步骤 ⑥ 失败处理

```bash
# 硬编码分类（替代 LLM 判断）
python {sd}/analyze.py classify-failures \
  --run-result {paths.run_result} \
  --run-state {paths.state_shard} \
  --round {N} --max-iterations {max_iterations} \
  --output .test/verdicts/{shard_slug}.json
```

读取 verdicts.json，按 `preliminary_verdict` 决定修复路径：

| verdict | 处理 |
|--------|------|
| `test_code_bug` | Edit 修复测试代码 → 回 ③（加 `--only-cases <失败的case_ids> --no-coverage`） |
| `source_code_bug` | 调用 `record-bug` 登记，不修测试 |
| `ambiguous` | 先按 test_code_bug 修，同 case 失败 2 次升级为 source_code_bug |

**断言保护规则**：修复 `assertion_origin=blind` 的 case 时：
- **允许修改**：setup 代码、imports、mock 配置、fixture、语法错误
- **禁止修改**：assert 表达式本身、expected value
- 如果确认是断言本身写错了（而非 setup 问题），标记该 case 为 `assertion_invalid` 并跳过，不要修改断言

```bash
# 登记 bug
python {sd}/analyze.py record-bug \
  --bugs-file {paths.bug_shard} \
  --file {source_path} --function {func_key} --case-id {case_id} \
  --round {N} --traceback-file .test/traces/{shard_slug}_iter{N}.txt \
  --reason "一句话判断"
```

### 步骤 ⑦ decide-next

```bash
python {sd}/analyze.py decide-next \
  --baseline test/generated_unit/test_cases.json \
  --run-state {paths.state_shard} \
  --run-result {paths.run_result} \
  --verdicts .test/verdicts/{shard_slug}.json \
  --round {N} --max-iterations {max_iterations} \
  --output .test/next_actions/{shard_slug}.json
```

读取输出，按 `action` + `fix_kind` 字段决定：

| action | fix_kind | 含义 | 下一步 |
|--------|----------|------|--------|
| `done` | — | 覆盖率达标 | 进入步骤 ⑧ 返回结果 |
| `gen_more` | — | 需要补测 | 回到 ② |
| `gen_more_with_bug` | — | 有 source_bug + 覆盖率仍差 | 回到 ②，**跳过 `skip_case_ids` 对应的函数**（不为其生成新 case） |
| `fix_only` | _(空)_ | 只需修失败（来自 classify-failures verdicts） | 回到 ⑥ |
| `fix_only` | `missing_test_function` | **Rule 0 兜底**：agent step ④ 漏了处理 orphaned | 按 `suggested_case_ids` 创建缺失的 `def test_xxx` 函数 → 回 ③ |
| `abandon` | — | 迭代耗尽 | 进入 ⑧ |
| `escalate` | — | source_bug | 进入 ⑧ |

**`fix_kind=missing_test_function` 处理细节**（Rule 0 兜底路径）：
1. 从 `next_action.suggested_case_ids` 获取 orphaned case ID 列表
2. 从 run_state 的对应 case 条目读取 `test_name`、`dimension`、`description`
3. 在 test 文件中为每个 case 创建对应的 `def test_xxx():` 函数骨架
4. 函数体至少包含基础 setup + 一个有意义断言（基于 source_snippets 中的签名/docstring）
5. 函数上方加 `# CASE_ID: <id>` 注释
6. 更新 cases-patch，把这些 case 的 `status` 改为 `pending`
7. 回到步骤 ③ 执行 apply-and-run

**重要**：此路径是 Rule 0 兜底——意味着 agent 在 step ④ 漏了处理 orphaned。处理完毕必须在 `final_unmet_reasons` 中注明 "Rule 0 兜底修复了 N 个 orphaned case"。

**`circuit_break=True` 处理**：如果 `next_action.circuit_break` 为 `true`，说明当前 action 是兜底触发的（如进度检测死循环 → escalate），需要在 `final_unmet_reasons` 中注明原因，并在返回前将 `abandon_reason` 设为 `next_action.reason`。

如果 `action=gen_more` 或 `gen_more_with_bug`，可以用 `gaps` 命令获取精确缺口（`gen_more_with_bug` 时 gaps 会自动排除 `skip_case_ids` 对应的函数）：

```bash
python {sd}/analyze.py gaps \
  --run-result {paths.run_result} \
  --baseline test/generated_unit/test_cases.json \
  --run-state {paths.state_shard} \
  --output .test/gaps/{shard_slug}_iter{N}.json
```

### 步骤 ⑦b Bug 复核（Phase 5，与 escalate 同触发）

当 `action=escalate` 且存在 `source_code_bug` verdicts 时，执行选择性复核：

**触发条件**（满足任一即复核）：
- confidence < 0.8
- 同函数 ≥ 2 个 case 报 bug
- iterations_used > 3

**复核流程**：
1. 读取盲测断言对应的 test 函数源码（从 test_path）
2. 仅基于以下信息判断，**不看源码片段**：
   - 盲测断言（assertion_origin=blind 的 case）
   - traceback
   - 函数签名 + docstring（从 task_envelope 的 source_snippets 获取）
3. 给出判断：`confirmed` / `not_bug` / `needs_human_review`
4. 两轮一致 → confirmed；不一致 → needs_human_review

**复现脚本生成**：
对每个 confirmed 的 bug，生成最小复现脚本：
```
.test/repro/{slug}_{case_id}.py
```

脚本内容：
- 最小 import
- 直接调用源码函数
- 触发相同的异常/断言失败
- 可独立运行 `python .test/repro/{slug}_{case_id}.py`

验证复现脚本：
```bash
python {sd}/dispatch.py verify-repro \
  --repro-dir .test/repro \
  --output .test/repro_results.json
```

### 步骤 ⑧ 返回结果

构建并返回 JSON：

```json
{
  "source_path": "core/parser.py",
  "test_path": "test/generated_unit/core/test_parser.py",
  "functions": {
    "parse_header": {
      "dimensions": ["functional", "boundary", "exception"],
      "coverage": {
        "line": {"target": 90, "actual": 95.5},
        "branch": {"target": 90, "actual": 88.0},
        "function": {"target": 100, "actual": 100}
      }
    }
  },
  "unmet_reasons": [],
  "objective_blocker": false,
  "dead_code": false,
  "dead_code_locations": [],
  "iterations_used": 3,
  "orphaned_case_ids": [],
  "source_bug_ids": [],
  "abandon_reason": "",
  "action_on_exit": "done"
}
```

覆盖率取值：从 `paths.run_result` 的 `coverage[source_path].functions[func_key]` 读取 `statement_rate`。

新字段说明：
- `orphaned_case_ids`：步骤 ④ 检测到的孤立 case ID 列表（即使已全部修复也记录曾检测到的）
- `source_bug_ids`：步骤 ⑥ 中通过 `record-bug` 登记的 source_bug case ID 列表
- `abandon_reason`：若 `action_on_exit` 为 `abandon`，填入原因（如 "迭代耗尽：覆盖率未达标"）
- `action_on_exit`：退出时的最终 action（`done`/`abandon`/`escalate`）

---

## 注意事项

1. **基线只读**：`test_cases.json` 不可修改
2. **CASE_ID 注释**：每个测试函数上方必须有 `# CASE_ID:` 注释
3. **不删除已有测试**：追加模式
4. **并行隔离**：始终用 `paths.*` 中的 shard 路径，不写全局文件
5. **迭代是强制的**：覆盖率未达标时必须迭代，`iterations_used=1` 且覆盖率 < 阈值是严重错误
6. **断言质量**：每个测试必须有有意义断言
7. **浮点比较**：用 `pytest.approx` 而非 `==`
8. **临时文件**：用 `tmp_path` fixture（pytest 内置），不硬编码 `/tmp`
9. **conftest.py**：如果缺且 import 报错，创建最小的：
   ```python
   import sys, pathlib
   sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
   ```
