---
name: unit-test-cplusplus-generate-run
description: 单测生成流水线的生成-运行阶段:基于 `test_cases.json` 基线,为 C++ 源文件分批派发 `cpp-test-gen-agent` 子 agent 并行生成测试、编译、采集覆盖率,最后汇总结果回写基线。
---

# unit-test-cplusplus-generate-run

基于 `unit-test-gen-init` 产出的 `test_cases.json` 基线,把仓库里的 C++ 待测文件按函数数贪心打包成批次,派发 `cpp-test-gen-agent` 子 agent 并行生成测试代码、编译、运行、采集覆盖率,最后把结果回写到 `test_cases.json`,并产出全局汇总。

本 skill **只处理 C++**。Python 测试生成走独立的 skill。

## 设计原则

本 skill 走"脚本化"路线:**所有机械数据处理由 `scripts/` 下的 Python 脚本完成**,Claude 主要负责:

1. 与用户交互(确认排除目录、处理 CMake 缺失等)
2. 按顺序调用脚本、读取结果
3. 派发 sub-agent 并收集结果
4. 把最终产物展示给用户

Claude 本身**不构造大段 JSON、不解析 test_cases.json、不做 LPT 打包算法、不做 atomic write**。这些都在脚本里。脚本接口契约见 `scripts/README.md`。

## 前置依赖

- 上游:`unit-test-gen-init` 已成功跑过,`test/generated_unit/test_cases.json` 存在且 `languages` 里含 `"cpp"`
- 环境:Linux + Python 3.9+
- Sub-agent:`.claude/agents/cpp-test-gen-agent.md` 必须存在

## 标准工作流

### 步骤 1:环境预检

```bash
python3 scripts/check_env.py --auto-install-gcovr
```

拿 stdout 的 JSON。如果 `all_ok: true`,进入下一步。

如果 `all_ok: false`,读 `missing` 数组和 `install_hints`,向用户展示缺失工具和安装命令。

然后终止。**保留 stdout 拿到的 `tools` 字段**,后续步骤 10 要用它做 `tool_status`。

### 步骤 2:确认仓库有 CMakeLists.txt

用 bash 检查 `<repo_root>/CMakeLists.txt`:

```bash
test -f <repo_root>/CMakeLists.txt && echo exists || echo missing
```

**存在**:跳到步骤 3。

**不存在**:用 `AskUserQuestion` 询问:

- **question**: `"仓库根没有 CMakeLists.txt。本 skill 需要它来探测 include 路径和 C++ 标准。请选择:"`
- **options**: `["让 skill 自动生成一个最小化 CMakeLists.txt", "终止,我去手动准备 CMakeLists.txt 后再跑"]`
- **header**: `"缺少 CMakeLists"`

**选"自动生成"**:用 bash 一行生成最小化 CMakeLists(无需单独脚本):

```bash
cd <repo_root> && \
SRCS=$(find . -type f \( -name "*.cpp" -o -name "*.cc" -o -name "*.cxx" \) \
       | grep -v '/build/' | grep -v '/\.test/' | tr '\n' ' ') && \
cat > CMakeLists.txt <<EOF
cmake_minimum_required(VERSION 3.14)
project(auto_gen_for_testing LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
add_library(auto_gen_lib STATIC $SRCS)
EOF
```

告诉用户:"已在仓库根生成最小 CMakeLists.txt(仅供本 skill 探测 include 和 C++ 标准用,如需适配请自行调整)"。

**选"终止"**:打印"请准备好 CMakeLists.txt 后重新执行本 skill",退出。

### 步骤 3:生成 compile_commands.json

检查是否已存在且新鲜(mtime 在 24h 内):

```bash
find <repo_root>/build/compile_commands.json -mtime -1 2>/dev/null
```

**有输出**(存在且新鲜):跳到步骤 4。

**无输出**:跑 cmake configure:

```bash
cmake -S <repo_root> -B <repo_root>/build \
      -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
      -DCMAKE_BUILD_TYPE=Debug
```

只 configure,不 `cmake --build`。失败时展示 cmake 输出末 30 行,提示用户"仓库 CMake 配置自身有问题,请先让 `cmake -S . -B build` 在仓库里跑通",终止。

### 步骤 4:生成 build_context.json

```bash
python3 scripts/build_build_context.py \
    --repo-root <repo_root> \
    --compile-commands build/compile_commands.json \
    --output .test/build_context.json
```

**退出码 0**:进入步骤 5。

**退出码 3**(抽不到 cxx_standard):用 `AskUserQuestion` 问用户:

- **question**: `"没能从 CMakeLists 抽取 C++ 标准版本,请选择:"`
- **options**: `["11", "14", "17", "20"]`
- **header**: `"C++ 标准"`

拿到 `<std>` 后带参数重跑脚本:

```bash
python3 scripts/build_build_context.py \
    --repo-root <repo_root> \
    --compile-commands build/compile_commands.json \
    --output .test/build_context.json \
    --cxx-standard <std>
```

**其他退出码**:展示 stderr,终止。

### 步骤 5:询问排除目录

```bash
python3 scripts/list_top_dirs.py \
    --baseline test/generated_unit/test_cases.json \
    --language cpp
```

拿 stdout(每行一个目录)。调用 `AskUserQuestion`,`multiSelect: true`:

- **question**: `"以下目录包含基线中的 C++ 源文件,请选择【不需要】生成测试的目录(勾选的目录会被跳过):"`
- **options**: stdout 每行一项
- **header**: `"排除目录"`

记用户勾选结果为 `skip_dirs`(空格分隔字符串,可能为空)。

### 步骤 6 + 7:筛选文件 + 打包批次

一个脚本完成:

```bash
python3 scripts/pack_batches.py \
    --baseline test/generated_unit/test_cases.json \
    --skip-dirs <skip_dirs> \
    --output .test/batches.json
```

脚本自动合并 baseline 的 `coverage_config.exclude_dirs`。

**退出码 0**:读 stdout 的 `agent_count` 和 `batch_count`,告诉用户:"将生成 `<agent_count>` 个 sub-agent,分 `<batch_count>` 批并发运行,每批 3 个"。

**退出码 3**:告知"过滤后没有可处理的 C++ 文件",终止。

### 步骤 8 + 9:分批派发 sub-agent

读 `.test/batches.json` 的 `batch_count` 后,循环处理每个 batch(`batch_id` 从 0 到 `batch_count - 1`)。

**对每个 batch**:

**8a. 为本批每个 agent 生成 input JSON**

```bash
for agent_id in 0 1 2; do
    python3 scripts/build_agent_input.py \
        --baseline test/generated_unit/test_cases.json \
        --batches .test/batches.json \
        --batch-id <batch_id> \
        --agent-id $agent_id \
        --repo-root <repo_root_abs> \
        --build-context-path .test/build_context.json \
        --scripts-dir <scripts_dir_abs> \
        --output-dir .test/ 2>/dev/null || true
done
```

收集 stdout 打印的 input JSON 绝对路径(每次成功调用打印一个路径)。最后一批可能不足 3 个,脚本遇到越界 agent_id 退出码 2 即跳过。

**8b. 并发派发 sub-agent**

对本批收集到的每个 input JSON 路径,在**同一条回复里**并列用 `Agent` 工具启动 sub-agent,`subagent_type` 为 `cpp-test-gen-agent`,提示词模板:

> 读取 `<input JSON 绝对路径>`,按其 JSON 内容中定义的任务执行。

等待本批所有 sub-agent 全部完成后再进行下一步。

**8c. 汇报本批进度**

```bash
ls .test/*/run_result.json 2>/dev/null | wc -l
```

打印给用户:"批次 `<batch_id + 1>/<batch_count>` 完成,累计 run_result: `<count>` 个"。

**硬性约束**:

- 每批之间严格串行,不跨批并发
- 不做动态 work queue
- 某个 sub-agent 失败不影响本批其他 sub-agent
- 不重试失败的 sub-agent(它们内部已迭代 2 轮)

### 步骤 10:汇总结果

**10a. 收集所有 run_result**

```bash
python3 scripts/collect_results.py \
    --batches .test/batches.json \
    --output .test/all_results.json
```

读 stdout 的统计。如果有缺失 run_result,记下供后续报告提到。

**10b. 把步骤 1 的 tool_status 落盘**

Claude 用 bash heredoc 把步骤 1 保存的 `tools` 字段写入文件:

```bash
cat > .test/tool_status.json <<'EOF'
<粘贴步骤 1 拿到的 tools JSON>
EOF
```

**10c. 回写 test_cases.json**

```bash
python3 scripts/writeback_baseline.py \
    --baseline test/generated_unit/test_cases.json \
    --results .test/all_results.json \
    --tool-status .test/tool_status.json
```

读 stdout 拿"更新了 N 个文件、M 个函数、写入 K 个 cases"。

**10d. 生成 summary.json 和 summary.md**

这步 Claude 亲自做(无专用脚本):

1. 读 `.test/all_results.json`,统计各 status 计数、算平均覆盖率
2. 写 `.test/summary.json`(机器可读):
   ```
   {
     "generated_at": "<ISO 时间>",
     "total_files": N,
     "status_counts": {"success": ..., "coverage_not_met": ..., "source_bug": ..., ...},
     "coverage_summary": {"line_avg": ..., "branch_avg": ..., "function_avg": ...},
     "per_file": [ {"source_path": "...", "status": "...", "coverage": {...}, "run_result_path": "..."} ],
     "skipped_dirs": [...],
     "dead_code_found": N
   }
   ```
3. 写 `.test/summary.md`(人类可读),章节:
   - 概览(文件数、总函数、各 status 计数、平均覆盖率)
   - 未达标明细(`coverage_not_met` 文件列表 + unmet_reasons)
   - 源码 bug 报告(`source_bug` 文件列表 + error 描述)
   - 测试生成失败(`test_gen_failed` 文件列表)
   - 死代码建议(所有 `dead_code_locations`)
   - 跳过目录

### 步骤 11:控制台报告

向用户输出:

1. 标题:`"C++ 单测生成完成"`
2. 把 `.test/summary.md` 的"概览"章节前 20 行贴出来(`head -20`)
3. 三条关键路径:
   - 基线(已回写 cases):`test/generated_unit/test_cases.json`
   - 机器可读汇总:`.test/summary.json`
   - 人类可读报告:`.test/summary.md`
4. 如果 `status_counts` 里 `coverage_not_met` / `source_bug` / `test_gen_failed` 任一 > 0,明确提示读 `summary.md` 相应章节

## 目录产物布局

```
.test/
├── build_context.json             步骤 4 产物
├── batches.json                   步骤 6+7 产物
├── tool_status.json               步骤 10b 产物
├── all_results.json               步骤 10a 产物
├── summary.json                   步骤 10d 产物
├── summary.md                     步骤 10d 产物
├── cpp_gen_batch_0_agent_0/       每个 sub-agent 一个工作目录
│   └── agent_input.json
├── core_parser_cpp/               每个源文件一个工作目录
│   ├── input.json                 sub-agent 保存的 input 片段
│   ├── process.log                sub-agent 过程日志
│   ├── run_result.json            sub-agent 最终结果
│   └── build/                     编译产物
│       ├── CMakeLists.txt
│       ├── cmake_configure.log
│       ├── cmake_build.log
│       ├── test_run.log
│       ├── gcovr.log
│       ├── test_core_parser_cpp
│       ├── coverage.json
│       └── *.gcda / *.gcno
└── ...
```

## 脚本清单

所有辅助脚本的详细接口契约见 `scripts/README.md`。本 skill 用到的脚本一览:

| 步骤 | 脚本 | 作用 |
|---|---|---|
| 1 | `check_env.py` | 环境总检测,输出 tool_status |
| 2 | (bash 一行) | 仓库缺 CMakeLists 时生成最小化版本 |
| 3 | `cmake` 命令 | 生成 compile_commands.json |
| 4 | `build_build_context.py` | 生成 build_context.json(含 cxx_standard 抽取) |
| 5 | `list_top_dirs.py` | 列出顶层目录供用户勾选 |
| 6+7 | `pack_batches.py` | 筛选 + LPT 贪心打包 |
| 8 | `build_agent_input.py` | 为指定 batch/agent 生成 input JSON |
| 10a | `collect_results.py` | 聚合所有 run_result |
| 10c | `writeback_baseline.py` | 回写 test_cases.json + tool_status |
| 10d | (Claude 亲自) | 生成 summary.json / summary.md |

## 故障排查

### 某批 sub-agent 全部 status=env_bug

查任一 `run_result.json` 的 `error` 字段(通常指向具体缺失组件)。补装后重跑 —— 步骤 1 的 `check_env.py` 会再次检测。

### 覆盖率回写后某些函数 cases 仍为空

可能原因:
1. 对应 sub-agent status 是 `source_bug` / `test_gen_failed` / `coverage_not_met` —— 查 `summary.md`
2. 函数在 sub-agent 的 `skipped_functions` 里 —— 查该文件的 `run_result.json`
3. 函数不在本次处理范围(文件被 `skip_dirs` 排除)

### compile_commands.json 生成失败

步骤 3 的 `cmake` configure 失败通常源于仓库自身 CMake 有问题。本 skill 不修复 —— 请先让 `cmake -S . -B build` 在仓库里手动跑通再重跑。

### sub-agent 卡在某个文件

长时间不返回(> 10 分钟无新输出)可能是某函数让 LLM 陷入生成死循环。临时方案:把该文件所在目录加入 `skip_dirs` 重跑;根本方案:检查该文件是否异常(超大模板、宏爆炸)。

## 默认行为速查

| 项 | 默认值 | 备注 |
|---|---|---|
| 每批并发 sub-agent 数 | 3 | 不暴露参数 |
| 单 sub-agent 最大文件数 | 10 | 硬上限 |
| 打包算法 | LPT 贪心(按函数数) | 不按文件夹 |
| 覆盖率迭代上限 | 2(由 sub-agent 实施) | 见 SUBAGENT.md |
| 处理范围 | 基线中所有 C++ 文件 - skip_dirs | 全量重跑 |
| 基线回写字段 | `files[*].functions[*].cases` + `tool_status` | 其余不动 |
| compile_commands.json 缓存 | mtime < 24h 视为新鲜 | 过期重生成 |
