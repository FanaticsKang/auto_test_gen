# scripts/ 契约清单

本文档列出 `unit-test-gen-generate-run` skill 的所有辅助脚本的**输入输出契约**。脚本实现者按此契约编码,SKILL.md 按此契约引用 —— 两边通过契约解耦。

## 通用约定

### 退出码

所有脚本统一使用:

| 退出码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 参数错误(缺必选参数、类型不对等) |
| 2 | 前置条件不满足(输入文件缺失、依赖工具不在 PATH 等) |
| 3 | 业务逻辑失败(解析失败、数据异常等) |

### 标准流约定

- **stdout**:只输出主产物(JSON、路径、目录列表等)。Claude 用 bash 捕获 stdout 作为机器可读结果
- **stderr**:人类可读的进度/警告/错误信息。出错时简要说明原因
- 不要往 stdout 混写日志

### 前置条件断言

除 `check_env.py` 外,其他脚本只做"自己需要"的 precondition 检查,不做全局环境扫描。检查失败时 `sys.exit(2)` 并在 stderr 打印原因。

### JSON I/O

- JSON 文件读写都用 UTF-8,`ensure_ascii=False`
- 写 JSON 文件一律 atomic write(先写 `.tmp` 再 `os.replace`)
- 只使用 Python 标准库(`json`、`pathlib`、`argparse`、`subprocess`、`hashlib`、`re` 等)

---

## 1. `check_env.py`

**职责**:步骤 1 的环境总检测。检查 cmake / g++ / gtest 头文件 / gcovr / ninja 是否可用,输出 tool_status JSON。

**用法**:

```bash
python3 scripts/check_env.py [--auto-install-gcovr]
```

**参数**:

| 参数 | 说明 |
|---|---|
| `--auto-install-gcovr` | 若 gcovr 缺失,尝试 `pip3 install --user --break-system-packages gcovr` 后重测 |

**输出**(stdout 一个 JSON):

```json
{
  "all_ok": false,
  "tools": {
    "cmake":  {"ok": true,  "version": "3.28.3", "path": "/usr/bin/cmake"},
    "gxx":    {"ok": true,  "version": "13.2.0", "path": "/usr/bin/g++"},
    "gtest":  {"ok": true,  "headers": "/usr/include/gtest/gtest.h"},
    "gmock":  {"ok": true,  "headers": "/usr/include/gmock/gmock.h"},
    "gcovr":  {"ok": false, "version": null, "path": null},
    "ninja":  {"ok": true,  "version": "1.11.1", "path": "/usr/bin/ninja"}
  },
  "missing": ["gcovr"],
  "install_hints": {
    "gcovr": "pip3 install --user gcovr"
  }
}
```

**退出码**:

- 0:所有必需工具齐(ninja 是可选,缺失不算失败)
- 3:至少一个必需工具缺失(Claude 读取 `missing` 字段决定后续)

**前置条件**:无。

---

## 2. `build_build_context.py`

**职责**:步骤 4 的合并版。从 `<repo_root>/CMakeLists.txt` 抽取 `cxx_standard`,从 `compile_commands.json` 统计 top-N include 路径,生成 `.test/build_context.json`。

**用法**:

```bash
python3 scripts/build_build_context.py \
    --repo-root <repo_root> \
    --compile-commands <path-relative-to-repo-root> \
    --output .test/build_context.json \
    [--cxx-standard 17] \
    [--top-n-includes 10]
```

**参数**:

| 参数 | 必选 | 说明 |
|---|---|---|
| `--repo-root` | 是 | 仓库根绝对路径 |
| `--compile-commands` | 是 | compile_commands.json 相对 repo_root 的路径(一般是 `build/compile_commands.json`) |
| `--output` | 是 | 输出 build_context.json 的路径 |
| `--cxx-standard` | 否 | 强制指定 C++ 标准(如 17);未传则从 CMakeLists 自动抽取,抽不到就以退出码 2 失败(由 Claude 调用 `AskUserQuestion` 问用户后再带此参数重跑) |
| `--top-n-includes` | 否 | 统计 compile_commands 中最常出现的 include 路径作为 `common_include_dirs`,默认 10 |

**输出 stdout**:成功时打印一行 `build_context written to <path>`;实际产物写到 `--output` 路径。

**输出文件**(`.test/build_context.json`):

```json
{
  "build_system": "cmake",
  "compile_commands_path": "build/compile_commands.json",
  "common_include_dirs": ["include", "src", "third_party/eigen/include"],
  "common_link_libraries": [],
  "cxx_standard": 17,
  "extra_cxxflags": []
}
```

**退出码**:

- 0:成功
- 1:参数错
- 2:CMakeLists.txt 不存在 / compile_commands.json 不存在 / `cmake` 不在 PATH
- 3:CMakeLists 抽不到 `cxx_standard` 且未传 `--cxx-standard`(Claude 应 ask user)

**前置条件断言**:

- `<repo_root>/CMakeLists.txt` 存在
- `<repo_root>/<compile_commands>` 存在且是合法 JSON
- `shutil.which("cmake")` 非空

---

## 3. `list_top_dirs.py`

**职责**:步骤 5,从 `test_cases.json` 的 `files` 里提取顶层目录(去重、排除已默认跳过的目录),供 Claude 调用 `AskUserQuestion` 让用户勾选排除。

**用法**:

```bash
python3 scripts/list_top_dirs.py \
    --baseline test/generated_unit/test_cases.json \
    [--language cpp]
```

**参数**:

| 参数 | 必选 | 说明 |
|---|---|---|
| `--baseline` | 是 | test_cases.json 路径 |
| `--language` | 否 | 只返回包含指定语言源文件的目录(默认 `cpp`),支持 `cpp` / `python` / `all` |

**输出**(stdout,每行一个目录):

```
core
planning
perception
sensor_fusion
```

**退出码**:

- 0:成功
- 2:baseline 文件不存在

**前置条件断言**:`--baseline` 指向的文件存在且是合法 JSON。

---

## 4. `pack_batches.py`

**职责**:步骤 6+7,从 `test_cases.json` 筛选待处理 C++ 文件(剔除 skip_dirs),用 LPT 贪心把文件打包成 sub-agent 批次,输出 `batches.json`。

**用法**:

```bash
python3 scripts/pack_batches.py \
    --baseline test/generated_unit/test_cases.json \
    --skip-dirs dir1 dir2 \
    --output .test/batches.json \
    [--k-max 10] \
    [--batch-size 3]
```

**参数**:

| 参数 | 必选 | 说明 |
|---|---|---|
| `--baseline` | 是 | test_cases.json 路径 |
| `--skip-dirs` | 否 | 要跳过的顶层目录列表(空格分隔);会与 baseline 里 `coverage_config.exclude_dirs` 取并集 |
| `--output` | 是 | 输出 batches.json 的路径 |
| `--k-max` | 否 | 单 sub-agent 最大文件数(默认 10) |
| `--batch-size` | 否 | 每批并发 sub-agent 数(默认 3) |

**输出文件**(`.test/batches.json`):

```json
{
  "generated_at": "2026-04-23T14:30:00+09:00",
  "total_files": 32,
  "total_functions": 412,
  "skip_dirs": ["tools/config", "deprecated"],
  "k_max": 10,
  "batch_size": 3,
  "agent_count": 11,
  "batch_count": 4,
  "batches": [
    {
      "batch_id": 0,
      "agents": [
        {
          "slug_prefix": "cpp_gen_batch_0_agent_0",
          "files": ["core/parser.cpp", "core/lexer.cpp"],
          "total_functions": 38
        },
        {
          "slug_prefix": "cpp_gen_batch_0_agent_1",
          "files": ["planning/astar.cpp"],
          "total_functions": 41
        },
        {
          "slug_prefix": "cpp_gen_batch_0_agent_2",
          "files": ["...": "..."],
          "total_functions": 39
        }
      ]
    }
  ]
}
```

**输出 stdout**:打印 `batches written to <path>, agent_count=N, batch_count=M`。

**退出码**:

- 0:成功
- 2:baseline 不存在
- 3:筛选后没有可处理的 C++ 文件(Claude 应终止并告知用户)

**前置条件断言**:`--baseline` 文件存在;baseline 的 `languages` 包含 `cpp`。

**算法**:LPT(Longest Processing Time)贪心。文件按函数数降序排序,逐个放入当前函数数最少的桶;若桶已满 k_max 个文件则跳过该桶。`agent_count = ceil(total_files / batch_size)`,必要时向上调整以满足 k_max 约束。

---

## 5. `build_agent_input.py`

**职责**:步骤 8,为 `batches.json` 中**指定**的一个 sub-agent 生成 input JSON。一次调用生成一个 sub-agent 的输入,便于并行或按需生成。

**用法**:

```bash
python3 scripts/build_agent_input.py \
    --baseline test/generated_unit/test_cases.json \
    --batches .test/batches.json \
    --batch-id 0 \
    --agent-id 1 \
    --repo-root /abs/path/to/repo \
    --build-context-path .test/build_context.json \
    --scripts-dir /abs/path/to/skills/unit-test-gen-generate-run/scripts \
    --output-dir .test/
```

**参数**:

| 参数 | 必选 | 说明 |
|---|---|---|
| `--baseline` | 是 | test_cases.json 路径(脚本会从中取每个文件的 functions、file_md5、test_path) |
| `--batches` | 是 | pack_batches.py 生成的 batches.json |
| `--batch-id` | 是 | 要生成的 batch 索引(0-based) |
| `--agent-id` | 是 | 该 batch 内的 agent 索引(0-based) |
| `--repo-root` | 是 | 仓库根绝对路径 |
| `--build-context-path` | 是 | build_context.json 相对或绝对路径 |
| `--scripts-dir` | 是 | 本 skill 的 scripts 目录绝对路径 |
| `--output-dir` | 是 | sub-agent 工作目录的父目录(一般是 `.test/`),脚本会在此创建每个文件的 `<slug>/` 子目录 |

**输出 stdout**:打印生成的 input JSON **绝对路径**,一行。Claude 用这个路径把内容读给 Task 工具。

**输出文件**:`<output-dir>/cpp_gen_batch_<batch_id>_agent_<agent_id>/agent_input.json`

内容符合 SUBAGENT.md 定义的 schema:

```json
{
  "files": [
    {
      "source_path": "...",
      "test_path": "...",
      "file_md5": "...",
      "functions": { "...": "..." },
      "paths": {
        "slug": "core_parser_cpp",
        "work_dir": ".test/core_parser_cpp/",
        "run_result": ".test/core_parser_cpp/run_result.json",
        "log": ".test/core_parser_cpp/process.log",
        "input_backup": ".test/core_parser_cpp/input.json",
        "build_dir": ".test/core_parser_cpp/build/"
      }
    }
  ],
  "coverage_config": { "...": "..." },
  "repo_root": "/abs/path/to/repo",
  "build_context_path": ".test/build_context.json",
  "scripts_dir": "/abs/path/to/.../scripts"
}
```

**slug 生成规则**:`source_path.replace("/", "_").replace(".", "_")`,例如 `core/parser.cpp` → `core_parser_cpp`。

**退出码**:

- 0:成功
- 1:参数错
- 2:baseline / batches 文件不存在,或 batch_id / agent_id 越界

**前置条件断言**:baseline 和 batches 两个 JSON 文件都存在且可解析。

---

## 6. `collect_results.py`

**职责**:步骤 10 辅助,扫描 `.test/<slug>/run_result.json` 把所有结果聚合成一个 JSON 供下游使用。

**用法**:

```bash
python3 scripts/collect_results.py \
    --batches .test/batches.json \
    --output .test/all_results.json
```

**参数**:

| 参数 | 必选 | 说明 |
|---|---|---|
| `--batches` | 是 | pack_batches.py 生成的 batches.json,用于定位所有 slug |
| `--output` | 是 | 输出聚合 JSON 的路径 |

**输出文件**(`.test/all_results.json`):

```json
{
  "collected_at": "2026-04-23T14:30:00+09:00",
  "total_files_expected": 32,
  "total_files_found": 31,
  "missing_run_results": ["some_file.cpp"],
  "results": [
    {
      "source_path": "core/parser.cpp",
      "slug": "core_parser_cpp",
      "status": "success",
      "run_result": { "...": "sub-agent 写入的完整 run_result.json 内容" }
    }
  ]
}
```

**输出 stdout**:`collected N/M run_results (missing: K)`。

**退出码**:

- 0:成功(即使部分 run_result 缺失也算成功,由下游判断是否可接受)
- 2:batches.json 不存在

---

## 7. `writeback_baseline.py`

**职责**:步骤 10A,把 `all_results.json` 中的 cases 回写到 `test_cases.json`,同时更新 `tool_status`。

**用法**:

```bash
python3 scripts/writeback_baseline.py \
    --baseline test/generated_unit/test_cases.json \
    --results .test/all_results.json \
    --tool-status .test/tool_status.json
```

**参数**:

| 参数 | 必选 | 说明 |
|---|---|---|
| `--baseline` | 是 | test_cases.json 路径,原地更新 |
| `--results` | 是 | collect_results.py 产出的 all_results.json |
| `--tool-status` | 否 | check_env.py 输出(或其子集 `tools`),用于更新 `tool_status` 字段 |

**输出 stdout**:简要统计 `updated N files, M functions, cases written K`。

**回写规则**(严格):

- 遍历 `all_results.results[*]`,定位 `baseline["files"][source_path]["functions"][func_key]`
- 用 `run_result.cases[func_key]` 替换 `cases` 字段;找不到对应函数时跳过
- 用 `tool-status.tools` 更新 `baseline["tool_status"]`(布尔化:`tool_status[name] = tools[name].ok`);名字映射:`gxx` → `gpp` 或保持不变(实现时定好)
- 其他字段一律不动(`func_md5`、`dimensions`、`mocks_needed`、`coverage_config`、`summary`、`source_dirs` 等)
- atomic write 保护

**退出码**:

- 0:成功
- 2:baseline / results 文件不存在
- 3:baseline 结构异常无法回写

---

## 脚本依赖图

```
check_env.py              (独立)
    └─ 产出 tool_status,供 writeback_baseline 使用

build_build_context.py    依赖:<repo_root>/CMakeLists.txt, build/compile_commands.json
    └─ 产出 .test/build_context.json,供 build_agent_input 使用

list_top_dirs.py          依赖:test_cases.json
    └─ 供 Claude 做 AskUserQuestion

pack_batches.py           依赖:test_cases.json + skip_dirs
    └─ 产出 .test/batches.json,供 build_agent_input / collect_results 使用

build_agent_input.py      依赖:test_cases.json + batches.json + build_context.json
    └─ 产出每个 sub-agent 的 agent_input.json

collect_results.py        依赖:batches.json + 各 sub-agent 产出的 run_result.json
    └─ 产出 .test/all_results.json,供 writeback_baseline 使用

writeback_baseline.py     依赖:test_cases.json + all_results.json + tool_status
    └─ 原地更新 test_cases.json
```

## Claude 端消化这些脚本的要点

- 所有脚本都是**一次性执行**,不常驻、不交互
- Claude 用 `bash` 工具调用,用退出码判断成败,用 stdout 拿机器可读结果
- 交互(问用户)全在 Claude 侧,脚本失败 2(前置不满足)后 Claude 重试需**带补充参数**
- 不要在 Claude 里重新解析 baseline 去手工拼 JSON —— 交给 `build_agent_input.py`
