---
name: unit-test-python-generate-run
description: Python 单测生成流水线的生成-执行阶段：读取 init 阶段产出的只读基线 `test_cases.json`，通过文件级调度模型派发 Claude sub-agent 生成测试代码、执行测试、采集覆盖率、迭代补测。主 agent 负责调度编排和结果收集，sub-agent 负责生成-执行-覆盖率循环。所有运行状态写到 `generate_process.json` 和 `test_run_state.json`，**基线文件全程只读**。
---

# unit-test-python-generate-run

本 skill 是 Python 单测流水线的第二阶段。上游（`unit-test-gen-init`）扫描仓库产出 `test_cases.json` 基线，记录每个函数的 `dimensions` / `mocks_needed` / `func_md5` 等元数据。本 skill 负责：

1. 初始化调度状态（`generate_process.json`）
2. 按文件批量派发 Claude sub-agent
3. 每个 sub-agent 独立完成：生成测试 → 执行 → 采集覆盖率 → 失败修复 → 迭代补测
4. 收集结果，生成最终报告

支持 Python（pytest + coverage.py）。

## 硬规则

1. **基线只读**：`test_cases.json` 是 init 阶段的产物，本 skill 永远不修改它。
2. **源码不可写**：本 skill（含所有子 agent）只能在 `test/` 和 `.test/` 目录下创建/修改文件，**不得修改除 `test/` 以外的任何源码文件**。

## 职责分工

- **主 agent（Claude）**：调度编排、状态管理、结果收集、生成报告。
- **子 agent（Claude sub-agent）**：生成测试代码、执行测试、分类失败、修复测试、迭代补测、返回覆盖率结果。
- **脚本**：机械性数据处理（执行测试、采集覆盖率、解析失败、合并 cases），不写测试代码也不判断失败原因。

## 前置条件

- `test/generated_unit/test_cases.json` 已由 init 阶段产生，如果没有生成则直接终止
- Python 路径：`pip install pytest pytest-cov coverage`
- 并行加速：`pip install pytest-xdist psutil`（sub-agent 首次检测到 xdist 缺失时会尝试自动安装；安装失败则降级为串行）

## 预授权路径配置（可选，减少交互弹窗）

本 skill 会读/写以下路径。建议在被测仓库根目录的 `.claude/settings.json` 中加入相应 allow 规则：

```json
{
  "permissions": {
    "allow": [
      "Read",
      "Write(test/**)",
      "Write(.test/**)",
      "Bash(python3 */scripts/runner.py*)",
      "Bash(python3 */scripts/dispatch.py*)",
      "Bash(python3 */scripts/analyze.py*)",
      "Bash(pytest:*)",
      "Bash(pip install pytest-xdist*)"
    ]
  }
}
```

- **读**：全仓库（扫描源码与现有测试）
- **写**：`test/generated_unit/**`（测试代码）、`.test/**`（运行状态与覆盖率）
- **Bash**：三个脚本的所有子命令、pytest 执行、xdist 安装

## 关键文件

| 路径 | 谁写 | 说明 |
|---|---|---|
| `test/generated_unit/test_cases.json` | init（只读） | 函数元数据基线 |
| `test/generated_unit/generate_process.json` | `dispatch.py`（claim 子命令原子写） | 调度状态：文件级 status + claimed_at + 子 agent 结果 |
| `test/generated_unit/test_run_state.json` | 主 agent（`analyze merge-state` 产出） | 合并后的 cases 内容 / 状态 |
| `test/generated_unit/<src>/test_<name>.py` | 子 agent | 生成的 Python 测试 |
| `.test/run_results/<slug>.json` | 子 agent（`runner.py run`） | **per-file** 测试结果 + 覆盖率 shard |
| `.test/state_shards/<slug>.json` | 子 agent（`analyze update-state`） | **per-file** cases shard |
| `.test/bug_shards/<slug>.json` | 子 agent（`analyze record-bug`） | **per-file** 源码 bug shard |
| `.test/source_bugs.json` | 主 agent（`analyze merge-bugs` 产出） | 合并后的源码疑似 bug |

并行正确性的关键约束：**`.test/run_results`、`.test/state_shards`、`.test/bug_shards`是每个 sub-agent 自己的 shard 目录，互不冲突。**
全局单文件（`test_run_state.json`、`.test/source_bugs.json`）只在步骤 7 之后由主 agent 调用 merge 命令一次性生成。

## 脚本结构

所有脚本位于 `scripts/` 下，按子命令组织为 3 个文件：

| 脚本 | 子命令 | 使用者 | 说明 |
|------|--------|--------|------|
| `dispatch.py` | `init` / `batch` / `claim` / `report` | 主 agent | 调度编排 + 报告生成 |
| `runner.py` | `run` | 子 agent | 测试执行 + 覆盖率采集（支持单文件作用域） |
| `analyze.py` | `update-state` / `extract-failures` / `gaps` / `record-bug` / `merge-state` / `merge-bugs` | 子 agent（前 4 个）/ 主 agent（merge-*） | 状态管理 + 失败分析 + 缺口筛选 + bug 登记 + shard 合并 |

## 标准工作流

### 步骤 1：确认覆盖率阈值

在执行任何操作之前，读取基线的 `coverage_config` 并询问用户是否需要修改：

```bash
cat test/generated_unit/test_cases.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d.get('coverage_config',{}), indent=2))"
```

向用户展示当前阈值，询问是否修改：

> 覆盖率阈值配置（来自 test_cases.json）：
> - 语句覆盖率：90%
> - 分支覆盖率：90%
> - 函数覆盖率：100%
>
> 是否需要修改？

如果用户要求修改，用 Edit 工具直接修改 `test_cases.json` 的 `coverage_config` 字段。

### 步骤 2：环境探测与安装

在派发子 agent 之前，一次性探测并安装测试依赖，避免多个并行 sub-agent 同时 pip install 冲突：

```bash
pip install pytest pytest-cov coverage pytest-xdist psutil -q
```

验证关键包可用：

```bash
python3 -c "import pytest, pytest_cov, coverage, xdist; print('OK')"
```

如果 xdist 安装失败（网络/权限问题），sub-agent 会自动降级为串行执行，不影响功能。

### 步骤 3：初始化调度状态

如果 `generate_process.json` 已存在，可以直接进入步骤 4。

```bash
python scripts/dispatch.py init \
  --baseline test/generated_unit/test_cases.json \
  --output test/generated_unit/generate_process.json \
  --shards-root .test \
  --max-iterations 5
```

生成 `generate_process.json`，每个源文件一条记录，初始 status 为 `"pending"`。
`shards_root` 会被记住，后续 `batch / claim` 输出的 `paths.*` 都基于它。

### 步骤 4：原子 claim 一批文件

```bash
python scripts/dispatch.py claim \
  --process test/generated_unit/generate_process.json \
  --baseline test/generated_unit/test_cases.json \
  --number 3 \
  --stale-seconds 600
```

这一步同时完成选中 N 个文件并将其标注为 `"running"`，无需再用 Edit 改 JSON。返回的 JSON：

- `files[]`：与 batch 同构，每个文件带 `paths.{run_result,state_shard,bug_shard,slug}`
  —— 这些 shard 路径**必须**原样传给子 agent，否则并行时会互相覆盖
- `status_counts`：各状态计数
- `reclaimed_stale`：把超过 `--stale-seconds` 的 "running" 任务（子 agent 崩溃 / 超时）重新回收到本批
- `all_done`：全部进入终态时为 true

如果 `batch_size == 0` 且 `all_done == true`，跳到步骤 8。

### 步骤 5：派发子 agent

对 claim 返回的每个文件，用 Agent tool 派发一个 Claude sub-agent。子 agent prompt 含：

1. `agents/python-test-gen-agent.md` 的完整内容
2. claim 返回的该文件完整信息（含 `paths`、`source_path`、`test_path`、
   `functions`、`coverage_config`、`max_iterations`）
3. `repo_root` 和 `scripts_dir`（供子 agent 调用脚本）

可以在同一个消息里并行派发多个子 agent（每个文件一个）。**并行子 agent 数量不能超过 3 个**。

### 步骤 6：收集结果

子 agent 完成后返回结构化 JSON。主 agent 对每个文件：

1. 读取 `generate_process.json`
2. 将子 agent 返回的 result 写入对应文件的 `result` 字段
3. 根据 `result` 将 `status` 改为：
   - `"completed"`：`unmet_reasons == []`（达标）**或** `unmet_reasons` 非空但 `objective_blocker == true`（dead code / 抽象方法 / 不可达分支等客观原因，备注原因）
   - `"unmet"`：`unmet_reasons` 非空**且** `objective_blocker == false`——未达标原因是测试能力不足（迭代不够、测试方案问题等）
   - `"abandoned"`：所有 gap 函数都被 `source_bug` 阻塞

### 步骤 7：循环

- 仍有 `"pending"` 或 stale `"running"` → 回到步骤 4（下一批，claim 会自动回收 stale）
- 仍有 `"running"` 但未超时 → 等待子 agent 完成
- 全部终态 → 步骤 8

### 步骤 8：合并 shards → 生成报告

并行 shards 由主 agent 统一合并：

```bash
# 7.1 合并所有 per-file state shards
python scripts/analyze.py merge-state \
  --shards-dir .test/state_shards \
  --baseline test/generated_unit/test_cases.json \
  --output test/generated_unit/test_run_state.json

# 7.2 合并所有 per-file bug shards
python scripts/analyze.py merge-bugs \
  --shards-dir .test/bug_shards \
  --output .test/source_bugs.json

# 7.3 生成按文件的分析报告（从 .test/run_results 目录聚合覆盖率）
python scripts/dispatch.py report \
  --baseline test/generated_unit/test_cases.json \
  --run-state test/generated_unit/test_run_state.json \
  --run-results-dir .test/run_results \
  --source-bugs .test/source_bugs.json \
  --process test/generated_unit/generate_process.json \
  --output .test/per_file_report.md \
  --format markdown
```

读取报告文件并呈现给用户。同时从 `generate_process.json` 汇总：

- 达标文件数（`status == "completed"`）vs 未达标（`"unmet"`）vs 已放弃（`"abandoned"`）
- 每个文件的迭代次数（`result.iterations_used`）
- 未达标原因（`result.unmet_reasons`）和 dead code 标记（`result.dead_code`）

## generate_process.json 结构

```json
{
  "version": "1.0",
  "generated_at": "2026-04-21T12:00:00",
  "baseline_ref": "test/generated_unit/test_cases.json",
  "max_iterations": 5,
  "shards_root": ".test",
  "coverage_config": {
    "statement_threshold": 90,
    "branch_threshold": 90,
    "function_threshold": 100
  },
  "files": {
    "core/parser.py": {
      "file_md5": "abc123",
      "test_path": "test/generated_unit/core/test_parser.py",
      "status": "<pending|running|completed|unmet|abandoned>",
      "claimed_at": "2026-04-21T12:05:00",
      "claim_round": 1,
      "result": null
    }
  }
}
```

`status` 状态机：

```
pending ──(dispatch claim)──▶ running ──(子 agent 返回)──▶ completed / unmet / abandoned
                                 │
                                 └──(超过 --stale-seconds)──▶（下次 claim 时自动回收）
```

| status | 含义 |
|---|---|
| `pending` | 还没派发过 |
| `running` | `dispatch claim` 已写入 `claimed_at` |
| `completed` | 子 agent 返回 `unmet_reasons == []`，达到阈值 |
| `unmet` | 未达标且原因非客观（迭代不够 / 测试方案不足），`objective_blocker == false` |
| `abandoned` | 所有 gap 函数都被 source_bug 阻塞，补不了 |

## 子 agent 返回结果结构

```json
{
  "source_path": "core/parser.py",
  "test_path": "test/generated_unit/core/test_parser.py",
  "functions": {
    "parse_header": {
      "dimensions": ["functional", "boundary", "exception"],
      "coverage": {
        "line": { "target": 90, "actual": 95.5 },
        "branch": { "target": 90, "actual": 88.0 },
        "function": { "target": 100, "actual": 100 }
      }
    }
  },
  "unmet_reasons": ["parse_header branch 88% < 90%"],
  "objective_blocker": true,
  "dead_code": false,
  "iterations_used": 3
}
```

## 故障排查出口

- **测试全挂（导入错误）**：子 agent 在 `test/generated_unit/conftest.py` 新建最小实现
- **覆盖率为 0**：pytest-cov 未装；runner.py 会标记 `tool_status`
- **源码 md5 漂移**：runner.py 检测到漂移写入 `md5_drifts`，提醒用户重跑 init
- **子 agent 超时 / 崩溃**：不用手工清理。下一次 `dispatch claim --stale-seconds N`
  会把 `claimed_at` 早于 `now-N` 秒的"running"任务自动回收进本批，`reclaimed_stale`
  字段会列出被回收的文件路径。

## 依赖

- Python 3.9+
- `pip install pytest pytest-cov coverage`

## 参考文档

- `agents/python-test-gen-agent.md`：Python 子 agent 完整工作流定义
- `references/run-state-schema.md`：`test_run_state.json` 结构 + case 字段定义
- `references/run-result-schema.md`：`.test/run_result.json` 结构 + 覆盖率配置
- `references/failure-classification.md`：LLM 判定 test_code_bug / source_code_bug 的规则
