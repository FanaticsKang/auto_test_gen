# auto_test_gen

Claude Code 技能安装工具集 — 将本仓库 `skills/` 和 `agents/` 目录下的技能及 Agent 一键安装到任意目标项目。

## 用法

```bash
./install.sh <path/to/project>
```

只需一个必填参数：目标项目路径。脚本会自动完成以下工作：

1. **安装所有技能** — 将 `skills/` 下每个技能目录复制到目标项目 `.claude/skills/`
2. **安装所有 Agent** — 将 `agents/` 下每个 Agent 配置复制到目标项目 `.claude/agents/`
3. **生成权限配置** — 在目标项目 `.claude/settings.local.json` 中写入预授权规则
4. **自动安装依赖** — 扫描各技能 `SKILL.md` 中引用的 Python 包，自动 `pip install`

安装完成后，在目标项目中重启 Claude Code 即可激活。

## 示例

```bash
# 安装到当前项目
./install.sh .

# 安装到指定项目
./install.sh /path/to/your/project
```

## 安装后目录结构

```
目标项目/
└── .claude/
    ├── settings.local.json   # 预授权权限规则
    ├── skills/
    │   ├── unit-test-gen-init/
    │   ├── unit-test-python-generate-run/
    │   └── unit-test-cplusplus-generate-run/
    └── agents/
        ├── python-test-gen-agent.md
        └── cpp-test-gen-agent.md
```

## 预授权权限

安装时自动写入 `settings.local.json`，包含以下预授权规则：

- `Read` — 读取全仓库
- `Write(test/**)` — 写入测试目录
- `Write(.test/**)` — 写入临时测试目录
- `Bash(*)` — 执行任意命令

## 可用技能

| 技能名 | 说明 |
|--------|------|
| `unit-test-gen-init` | 单测生成流水线初始化 — 扫描 Python / C++ 仓库，生成 `test_cases.json` 基线文件 |
| `unit-test-python-generate-run` | Python 单测生成流水线的生成-执行阶段 |
| `unit-test-cplusplus-generate-run` | C++ 单测生成流水线的生成-运行阶段 — 派发子 Agent 并行生成测试、编译、采集覆盖率 |

## 可用 Agent

| Agent 配置文件 | 说明 |
|----------------|------|
| `python-test-gen-agent.md` | Python 单测生成子 Agent |
| `cpp-test-gen-agent.md` | C++ 单测生成子 Agent |
