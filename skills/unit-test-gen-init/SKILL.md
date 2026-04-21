---
name: unit-test-gen-init
description: 单测生成流水线的初始化阶段:为 Python / C++ 项目扫描代码仓库并生成或增量更新 `test_cases.json` 基线文件。
---

# unit-test-gen-init

扫描代码仓库(Python 和/或 C++),生成 `test_cases.json` 基线文件 —— 记录每个可测试函数的 MD5、源码位置、签名、适用的测试维度和建议的 mock。同时统计文件和函数的扫描完整性覆盖率。

## 扫描器的工作原理

初始化分为两个阶段,由两个独立脚本完成:

1. **`scan_repo.py`**(纯扫描器):遍历仓库源码,用 AST 提取函数信息,输出原始扫描结果(含 `features` 字段)到 `.test/scan_result.json`
2. **`list_dirs.py`**(目录提取器):从扫描结果中提取去重的顶层目录列表,供用户勾选排除范围
3. **`build_baseline.py`**(基线生成器):读取原始扫描结果,移除 `features`,与已有基线 merge,统计覆盖率,写入 `test_cases.json`

`scan_repo.py` 只负责扫描,`build_baseline.py` 只负责基线生成和 merge —— 职责清晰,原始扫描结果始终保留在 `.test/` 下供审计。

## 标准工作流

**这是 99% 的情况下应该走的流程**。不要预先询问用户任何参数 —— 按下面的决策树直接跑。

### 步骤 1:扫描仓库

```bash
python scripts/scan_repo.py <repo_root> --output .test/scan_result.json
```

### 步骤 2:选择扫描范围

扫描完成后，运行 `list_dirs.py` 提取顶层目录列表，然后用 `AskUserQuestion` 让用户勾选排除范围。

```bash
python scripts/list_dirs.py --scan .test/scan_result.json
```

该脚本输出去重后的顶层目录（已排除默认跳过的目录如 `__pycache__`、`.git`、`test`、`scripts` 等）。

拿到目录列表后，调用 `AskUserQuestion`，设置 `multiSelect: true`：

- **question**: `"以下目录包含扫描到的源文件，请选择【不需要】生成单测的目录（排除后的目录将计入 coverage_config.exclude_dirs）："`
- **options**: 每个顶层目录作为一个选项
- **header**: `"排除目录"`

将用户选中的目录列表记为 `exclude_dirs`，传递给后续步骤。

### 步骤 3:生成基线

#### 3a:首次生成(基线不存在)

```bash
python scripts/build_baseline.py --scan .test/scan_result.json --output test/generated_unit/test_cases.json --mode full --exclude-dirs <exclude_dirs>
```

#### 3b:增量生成(基线已存在)

```bash
python scripts/build_baseline.py --scan .test/scan_result.json --output test/generated_unit/test_cases.json --exclude-dirs <exclude_dirs>
```

其中 `<exclude_dirs>` 为步骤 2 中用户选中的排除目录，以空格分隔传入（如 `--exclude-dirs tools/config`）。

`build_baseline.py` 会自动与已有 `test_cases.json` merge,保留用户编辑的 `coverage_config`、`tool_status`、未变函数的 `cases`。`--exclude-dirs` 传入的目录会写入 `coverage_config.exclude_dirs`，并且这些目录下的文件不会出现在基线中。

### 步骤 4:报告结果

读取 `test_cases.json` 和 `.test/scan_result.json`,按以下顺序向用户报告。所有统计均以基线范围（排除 `coverage_config.exclude_dirs` 之后的文件）为界,不包含排除目录下的任何数据。

1. **排除目录**:读取 `test_cases.json` 的 `coverage_config.exclude_dirs`,列在报告最前面
2. **范围内统计**:
   - 读取 `test_cases.json["files"]` 得到基线文件数和基线函数数
   - 从 `.test/scan_result.json` 的 `skipped_files` 中筛除排除目录下的条目,得到范围内跳过文件列表
   - 从 `.test/scan_result.json` 的 `skipped_functions` 中筛除排除目录下的条目,得到范围内跳过函数列表
   - 范围内总源文件数 = 基线文件数 + 范围内跳过文件数
   - 范围内总函数数 = 基线函数数 + 范围内跳过函数数
3. **覆盖率**:
   - 文件扫描率 = 基线文件数 / 范围内总源文件数 × 100%
   - 函数提取率 = 基线函数数 / 范围内总函数数 × 100%
   - 可直接使用 `test_cases.json["summary"]` 中的 `file_scan_rate` 和 `function_scan_rate`（`build_baseline.py` 已基于基线范围计算）
4. **跳过原因汇总**:仅统计范围内（非排除目录）的 `skipped_files` 和 `skipped_functions`,按 reason 分组统计精确数量（不得使用"约"等近似表述）并描述含义,同时给出跳过文件总数和跳过函数总数。如果存在 `reason: "unknown"` 的条目,需要逐一读取对应源文件,用 LLM 分析跳过原因并给出诊断建议
5. **语言和维度分布**:Python/C++,functional/boundary/exception 等
6. **增量诊断与重点**(如有):
   - 变更文件数、新增/删除函数
   - 如果函数因 MD5 改变导致 `cases` 被清空,明确列出这些函数名

## 故障排查出口:检视模式

如果扫描结果看起来异常,可以检查原始扫描结果:

```bash
cat .test/scan_result.json | python3 -m json.tool | head -50
```

原始扫描结果包含比基线更多的信息(`features`、`decorators`、`has_float_type`、`total_funcs_found` 等),供诊断使用。

## 调试产物

所有调试产物保存在 `.test/` 目录下:

- `.test/scan_result.json`:原始扫描结果(含 `features` 等完整 AST 特征)

## 依赖

- Python 3.9+(使用了 `ast.unparse`、`|` 类型联合)
- C++ 扫描需要:`pip install tree-sitter tree-sitter-cpp`
  - 如果仓库里有 C++ 文件但这个依赖没装,扫描器会在 stderr 打印警告并跳过 C++ 文件

## 输出格式

基线 `test_cases.json` 长这样:

```json
{
  "version": "1.0",
  "generated_at": "2026-04-20T12:34:56+09:00",
  "languages": ["python"],
  "test_frameworks": {"python": "pytest"},
  "source_dirs": ["."],
  "mode_last_run": "full",
  "summary": {
    "total_files": "...",
    "total_functions": "...",
    "total_cases": 0,
    "total_source_files": "... (基线范围内总源文件数)",
    "scanned_files": "... (基线文件数)",
    "file_scan_rate": "... (scanned_files / total_source_files × 100)",
    "total_functions_found": "... (基线范围内总函数数)",
    "extracted_functions": "... (基线函数数)",
    "function_scan_rate": "... (extracted_functions / total_functions_found × 100)"
  },
  "coverage_config": { "...": "用户可编辑,扫描时保留" },
  "files": {
    "core/parser.py": {
      "file_md5": "...",
      "test_path": "test/generated_unit/core/test_parser.py",
      "functions": {
        "parse_header": {
          "func_md5": "...",
          "line_range": [12, 45],
          "signature": "parse_header(data: bytes, strict: bool = False) -> Header",
          "is_async": false,
          "class_name": null,
          "dimensions": ["functional", "boundary", "exception"],
          "mocks_needed": [],
          "cases": []
        }
      }
    }
  }
}
```

`features` 字段仅存在于 `.test/scan_result.json`(原始扫描结果),不进入基线。

完整字段说明:见 `references/baseline-schema.md`。
维度判定规则:见 `references/dimensions.md`。

## 字段保留规则

基线生成时 `coverage_config`、`tool_status`、未变函数的 `cases` 都会保留;`func_md5` 变化会清空该函数的 `cases`,这是下游需要重新生成测试的信号。

## 默认跳过的内容

- **目录**:`__pycache__`、`.git`、`.venv`、`node_modules`、`test`/`tests`、  `docs`、`scripts`、`third_party`、`vendor`,以及任何以 `.` 开头或 `.egg-info`结尾的目录
- **Python 文件**:以 `_` 开头(含 `__init__.py`)、以 `_generated.py` 结尾
- **函数**:桩函数（函数体仅含文档字符串（可选）+ `pass` 或 `...`，即无实际实现的占位函数）、property setter、`@overload`、C++ `main`、析构、纯虚、`= default`、`= delete`

若用户报告"我的函数被漏了",优先怀疑是否匹配上述任一规则;其次检查解析错误(Python 版本过新、文件截断、编码异常)。
