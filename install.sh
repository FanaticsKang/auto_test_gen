#!/usr/bin/env bash
# Claude Code 技能安装脚本 — 将 skills/ 目录下的所有技能安装到目标项目
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILLS_DIR="${SCRIPT_DIR}/skills"
AGENTS_DIR="${SCRIPT_DIR}/agents"
SKILL_MANIFEST="SKILL.md"

die() { echo "Error: $*" >&2; exit 1; }

# 解析 SKILL.md 的 YAML frontmatter，输出技能名称
parse_skill_name() {
    local manifest="$1/${SKILL_MANIFEST}"
    [[ -f "$manifest" ]] || return 1

    local in_fm=0 name=""
    while IFS= read -r line; do
        [[ "$in_fm" -eq 0 && "$line" == "---" ]] && { in_fm=1; continue; }
        [[ "$in_fm" -eq 1 && "$line" == "---" ]] && break
        if [[ "$in_fm" -eq 1 && "$line" == name:* ]]; then
            name="${line#*:}"
            name="$(echo "$name" | xargs)"
        fi
    done < "$manifest"

    [[ -z "$name" ]] && name="$(basename "$1")"
    echo "$name"
}

# 检查 SKILL.md 中提到的 Python 依赖，输出缺失的包名
find_missing_deps() {
    local manifest="$1/${SKILL_MANIFEST}"
    [[ -f "$manifest" ]] || return

    local mentioned_deps
    mentioned_deps=$(grep -oE 'pip install [a-zA-Z0-9_\-]+( [a-zA-Z0-9_\-]+)*' "$manifest" 2>/dev/null \
        | sed 's/^pip install //' | tr ' ' '\n' || true)

    [[ -z "${mentioned_deps// /}" ]] && return

    local dep
    while IFS= read -r dep; do
        dep="$(echo "$dep" | xargs)"
        [[ -z "$dep" ]] && continue
        local module="${dep//-/_}"
        python3 -c "import ${module}" &>/dev/null || echo "$dep"
    done <<< "$mentioned_deps"
}

# ── 主逻辑 ──

[[ $# -lt 1 ]] && die "Usage: $(basename "$0") <path/to/project>"

target_path="$(cd "$1" 2>/dev/null && pwd)" || die "Invalid project path: $1"
[[ ! -d "${SKILLS_DIR}" ]] && die "Skills directory not found: ${SKILLS_DIR}"

target_skills_dir="${target_path}/.claude/skills"
mkdir -p "$target_skills_dir"

echo "Installing skills to ${target_path}"

# 1. 安装所有技能
installed=0
for item in "${SKILLS_DIR}"/*/; do
    [[ -d "$item" ]] || continue

    sname="$(parse_skill_name "$item")" || { echo "  Skipping $(basename "$item") — no valid ${SKILL_MANIFEST}"; continue; }
    target_dir="${target_skills_dir}/${sname}"

    rm -rf "$target_dir"
    cp -R "$item" "$target_dir"

    local_refs=0 local_scripts=0
    [[ -d "${target_dir}/references" ]] && local_refs=$(find "${target_dir}/references" -maxdepth 1 -type f | wc -l | tr -d ' ')
    [[ -d "${target_dir}/scripts" ]] && local_scripts=$(find "${target_dir}/scripts" -maxdepth 1 -type f | wc -l | tr -d ' ')
    echo "  [OK] ${sname} (${local_refs} ref, ${local_scripts} scripts)"
    ((installed++)) || true
done

[[ $installed -eq 0 ]] && die "No skills found in ${SKILLS_DIR}"

# 2. 安装所有 agents
agents_installed=0
if [[ -d "${AGENTS_DIR}" ]]; then
    target_agents_dir="${target_path}/.claude/agents"
    mkdir -p "$target_agents_dir"

    for agent_file in "${AGENTS_DIR}"/*.md; do
        [[ -f "$agent_file" ]] || continue
        agent_name="$(basename "$agent_file")"
        cp "$agent_file" "${target_agents_dir}/${agent_name}"
        echo "  [OK] agent: ${agent_name}"
        ((agents_installed++)) || true
    done
else
    echo "  No agents directory found, skipping."
fi

# 2. 生成 settings.local.json
settings_file="${target_path}/.claude/settings.local.json"
mkdir -p "$(dirname "$settings_file")"

if [[ -f "$settings_file" ]]; then
    # 已有文件：合并 permissions.allow 字段，确保 Bash(*) 存在
    updated=$(python3 -c "
import json, sys
with open('${settings_file}') as f:
    cfg = json.load(f)
perms = cfg.setdefault('permissions', {})
allows = perms.setdefault('allow', [])
if 'Bash(*)' not in allows:
    allows.insert(0, 'Bash(*)')
perms['allow'] = allows
json.dump(cfg, sys.stdout, indent=2)
" 2>/dev/null)
    if [[ -n "$updated" ]]; then
        echo "$updated" > "$settings_file"
    else
        echo '{"permissions": {"allow": ["Bash(*)"]}}' > "$settings_file"
    fi
else
    echo '{"permissions": {"allow": ["Bash(*)"]}}' > "$settings_file"
fi
echo "  [OK] .claude/settings.local.json (allow: Bash(*))"

# 3. 依赖检查与自动安装
all_missing=()
for item in "${SKILLS_DIR}"/*/; do
    [[ -d "$item" ]] || continue
    while IFS= read -r dep; do
        [[ -n "$dep" ]] && all_missing+=("$dep")
    done <<< "$(find_missing_deps "$item")"
done

if [[ ${#all_missing[@]} -gt 0 ]]; then
    unique_deps=$(printf '%s\n' "${all_missing[@]}" | sort -u | tr '\n' ' ')
    echo "  Installing dependencies: ${unique_deps}"
    python3 -m pip install $unique_deps
fi

echo ""
echo "Done. ${installed} skill(s), ${agents_installed} agent(s) installed."
echo "Restart Claude Code in the target project to activate."
