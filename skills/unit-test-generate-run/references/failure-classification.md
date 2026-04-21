# 失败分类规则

当测试失败时，LLM 必须判断是 **测试代码 bug** 还是 **源代码 bug**。这决定了
"改测试文件" vs "登记 source_bugs.json"。

规则分两层：**优先级阈值**（看 traceback 位置 + 异常类型）和 **语义兜底**（结合源码 + case 描述人工判断）。

## 三选一输出

| 分类 | 含义 | 下一步 |
|---|---|---|
| `test_code_bug` | 测试代码本身写错了：import 错、fixture 用错、assertion 值算错、mock 没打对、参数化 ID 重复等 | LLM 用 `Edit` 工具改测试文件，`status` → `fixed_pending_rerun` |
| `source_code_bug` | 源代码真的有问题：算法错、边界漏处理、异常没抛、状态污染、并发竞态等 | 调用 `analyze.py record-bug`，`status` → `source_bug`，不再重跑 |
| `ambiguous` | 看不清楚：环境依赖、浮点精度边界、外部资源缺失 | 先按 `test_code_bug` 处理一次；如果二次失败再归为 `source_code_bug` |

## 优先级规则（traceback 位置）

| traceback 最深帧文件 | 异常类型 | 默认分类 |
|---|---|---|
| `test/generated_unit/...` | `ImportError` / `ModuleNotFoundError` | `test_code_bug`（导入路径或 `conftest.py` 问题） |
| `test/generated_unit/...` | `NameError` / `AttributeError` | `test_code_bug`（符号写错、mock 对象没有对应属性） |
| `test/generated_unit/...` | `TypeError` 且调用栈顶在测试文件 | `test_code_bug`（参数传错） |
| `test/generated_unit/...` | `AssertionError` 且源文件完全未出现在栈中 | `test_code_bug`（期望值算错） |
| 源码目录 | `AssertionError` 或显式 `raise` | `source_code_bug` |
| 源码目录 | `IndexError` / `KeyError` / `TypeError` / `ZeroDivisionError` 等运行时异常，测试输入本身合法（在函数 contract 允许范围内） | `source_code_bug` |
| 源码目录 | 运行时异常，但测试用例本身就是用边界/非法输入来触发（维度 = `boundary`/`exception`）且 case 期望异常 | `test_code_bug`（断言方式错，例如没用 `pytest.raises`） |
| `unittest.mock` 内部 | 任意 | `test_code_bug`（mock 用法错） |

## 语义兜底

当优先级规则不足以定论时（比如 `AssertionError` 同时出现在测试和源码栈），LLM 应**读源码和测试代码对比**：

1. **反推源码语义**：读 `line_range` 内的源码，和 case description 对比，源码实际行为是否符合用户预期？
2. **检查测试输入**：case 的 inputs 是否在函数契约允许的输入空间内？
3. **检查期望值**：case 的 expected 是否正确？（有时 LLM 第一轮把期望值算错了）
4. **检查 mock 设置**：`mocks_needed` 里建议过的 mock 是否都打了、返回值是否合理？

## 二次失败规则

- 同一 case 第一次被判 `test_code_bug` 并修复后，如果**第二次仍失败**：
  - 如果 traceback 位置和异常类型变了 → 继续按新证据分类
  - 如果 traceback 和上次几乎一致 → 升级为 `source_code_bug`（修不动了）
- 修复计数 `fix_attempts` ≥ 3 → 强制归为 `failed_persistent` 并按 source_bug 登记

## 典型反模式（LLM 容易误判的场景）

1. **浮点精度**：`assert x == 0.1 + 0.2` 永远失败。这是 `test_code_bug`（应该用 `pytest.approx`）
2. **dict 顺序**：旧 Python / 平台差异；若测试代码断言字典排序，是 `test_code_bug`
3. **路径分隔符**：Windows vs Unix；测试硬编码 `/` 是 `test_code_bug`
4. **时区**：`datetime.now()` 相关；测试没 freeze 时间是 `test_code_bug`
5. **类方法调用时漏了 `self`**：`test_code_bug`

## 调用 `analyze.py record-bug` 的提示

**并行场景下** `--bugs-file` 必须指到子 agent 的 `paths.bug_shard`（如
`.test/bug_shards/<slug>.json`），不要写全局 `.test/source_bugs.json`；主 agent
会在所有 sub-agent 结束后用 `analyze.py merge-bugs` 合并 shards。

`--reason` 字段给出一句话判断，结构建议 "<触发条件>导致<错误表现>"，例如：

- `"当 data 长度 < 4 时未校验，直接索引 data[3] 抛 IndexError"`
- `"negative input 时返回 None 但函数签名声明 -> int"`
- `"缓存未清理导致第二次调用返回陈旧值"`

这些判断会进 `.test/source_bugs.json`，工程师 review 时直接读。
