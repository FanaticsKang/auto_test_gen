# auto_test_gen

Claude Code 技能安装工具集 — 将本仓库 `skills/` 目录下的技能一键安装到任意目标项目。

## 快速开始

```bash
# 交互式安装（推荐）
./install.sh

# 指定目标项目路径
./install.sh --target /path/to/your/project

# 指定源技能目录（默认为脚本同级的 skills/）
./install.sh --source /path/to/skills

# 只安装指定技能
./install.sh --skills unit-test-gen-init

# 强制覆盖已存在的技能
./install.sh --force

# 跳过依赖检查
./install.sh --skip-deps
```

## 选项说明

| 选项 | 说明 |
|------|------|
| `--target PATH` | 目标项目路径，省略则交互式输入 |
| `--source PATH` | 源技能目录（默认 `./skills`） |
| `--skills NAMES` | 逗号分隔的技能名，省略则交互式选择 |
| `--force` | 已存在的技能直接覆盖，不提示 |
| `--skip-deps` | 跳过 Python 依赖检查 |
| `-h, --help` | 显示帮助信息 |

## 交互式流程

不带参数运行 `./install.sh` 时，脚本会依次引导：

1. **输入目标路径** — 最多 3 次重试
2. **选择技能** — 列出所有可用技能，输入编号或 `all`
3. **冲突处理** — 如果目标已存在同名技能，可选择跳过 / 覆盖 / 备份后覆盖

## 安装原理

脚本会将 `skills/` 下每个技能目录复制到目标项目的 `.claude/skills/` 下：

```
目标项目/
└── .claude/
    └── skills/
        └── unit-test-gen-init/
            ├── SKILL.md
            ├── references/
            └── scripts/
```

安装完成后，在目标项目中重启 Claude Code 即可激活技能。

## 可用技能

| 技能名 | 说明 |
|--------|------|
| `unit-test-gen-init` | 单测生成流水线初始化 — 扫描 Python / C++ 仓库，生成 `test_cases.json` 基线文件 |

## 新增技能

在 `skills/` 下创建目录，添加 `SKILL.md`（含 YAML frontmatter）即可：

```
skills/
└── your-skill/
    ├── SKILL.md          # 必需，含 name 和 description
    ├── references/       # 可选，参考文档
    └── scripts/          # 可选，辅助脚本
```

`SKILL.md` 格式：

```markdown
---
name: your-skill
description: 一句话描述技能功能
---

# your-skill

技能的详细说明...
```
