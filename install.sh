#!/usr/bin/env bash
# Claude Code 技能安装脚本 — 将 skills/ 目录下的技能安装到目标项目的 .claude/skills/ 下
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILLS_DIR="${SCRIPT_DIR}/skills"
SKILL_MANIFEST="SKILL.md"

# ── 工具函数 ──

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Install Claude Code skills to a target project.

Options:
  --target PATH    Target project path (interactive prompt if omitted)
  --source PATH    Source skills directory (default: skills/ next to this script)
  --skills NAMES   Comma-separated skill names to install (default: all)
  --force          Overwrite existing skills without prompt
  --skip-deps      Skip dependency check
  -h, --help       Show this help message
EOF
    exit 0
}

die() { echo "Error: $*" >&2; exit 1; }
warn() { echo "Warning: $*" >&2; }

# 解析 SKILL.md 的 YAML frontmatter，输出 "name||description"
parse_skill_manifest() {
    local dir="$1" manifest="${1}/${SKILL_MANIFEST}"
    [[ -f "$manifest" ]] || return 1

    local content in_fm=0 name="" desc=""
    while IFS= read -r line; do
        if [[ "$in_fm" -eq 0 && "$line" == "---" ]]; then
            in_fm=1; continue
        fi
        if [[ "$in_fm" -eq 1 && "$line" == "---" ]]; then
            break
        fi
        if [[ "$in_fm" -eq 1 && "$line" == *":"* ]]; then
            local key="${line%%:*}" val="${line#*:}"
            key="$(echo "$key" | xargs)" val="$(echo "$val" | xargs)"
            case "$key" in
                name) name="$val" ;;
                description) desc="$val" ;;
            esac
        fi
    done < "$manifest"

    [[ -z "$name" ]] && name="$(basename "$dir")"
    echo "${name}||${desc}"
}

# 检查目标位置已存在的技能状态: absent / identical / different
check_existing_skill() {
    local target_skills_dir="$1" skill_name="$2" source_path="$3"
    local target_manifest="${target_skills_dir}/${skill_name}/${SKILL_MANIFEST}"
    local source_manifest="${source_path}/${SKILL_MANIFEST}"

    [[ -f "$target_manifest" ]] || { echo "absent"; return; }

    if diff -q "$source_manifest" "$target_manifest" &>/dev/null; then
        echo "identical"
    else
        echo "different"
    fi
}

# 安装单个技能
install_skill() {
    local source_dir="$1" target_dir="$2" mode="$3"

    if [[ "$mode" == "backup_and_overwrite" && -d "$target_dir" ]]; then
        local ts backup_path
        ts="$(date +%Y%m%d_%H%M%S)"
        backup_path="${target_dir}.bak.${ts}"
        echo "  Backing up to: ${backup_path}"
        mv "$target_dir" "$backup_path"
    fi

    if [[ "$mode" == "overwrite" && -d "$target_dir" ]]; then
        rm -rf "$target_dir"
    fi

    cp -R "$source_dir" "$target_dir"
}

# 验证安装结果
verify_installation() {
    local target_dir="$1"
    [[ -f "${target_dir}/${SKILL_MANIFEST}" ]] || return 1

    local refs=0 scripts=0
    [[ -d "${target_dir}/references" ]] && refs=$(find "${target_dir}/references" -maxdepth 1 -type f | wc -l | tr -d ' ')
    [[ -d "${target_dir}/scripts" ]] && scripts=$(find "${target_dir}/scripts" -maxdepth 1 -type f | wc -l | tr -d ' ')
    echo "  Verified: ${SKILL_MANIFEST} OK, ${refs} reference(s), ${scripts} script(s)"
}

# 检查 SKILL.md 中提到的依赖，输出缺失的包名（每行一个）
check_dependencies() {
    local skill_dir="$1" manifest="${1}/${SKILL_MANIFEST}"
    [[ -f "$manifest" ]] || return

    local mentioned_deps
    mentioned_deps=$(grep -oE 'pip install [a-zA-Z0-9_\-]+( [a-zA-Z0-9_\-]+)*' "$manifest" 2>/dev/null \
        | sed 's/^pip install //' | tr ' ' '\n' || true)
    mentioned_deps+=$'\n'
    mentioned_deps+=$(grep -oiE 'requires?:?[[:space:]]*[a-zA-Z0-9_\-]+(,[[:space:]]*[a-zA-Z0-9_\-]+)*' "$manifest" 2>/dev/null \
        | sed 's/^requires\?:[[:space:]]*//' | tr ',' '\n' | xargs -I{} echo "{}" || true)

    [[ -z "${mentioned_deps// /}" ]] && return

    local dep
    while IFS= read -r dep; do
        dep="$(echo "$dep" | xargs)"
        [[ -z "$dep" ]] && continue
        local module="${dep//-/_}"
        python3 -c "import ${module}" &>/dev/null || echo "$dep"
    done <<< "$mentioned_deps"
}

# ── 交互式函数 ──

prompt_target() {
    local attempt=0 max=3 raw path
    while (( attempt < max )); do
        echo ""
        read -rp "Enter target project path: " raw
        raw="$(echo "$raw" | xargs)"
        [[ -z "$raw" ]] && { echo "Cancelled." >&2; return 1; }
        path="$(cd "$raw" 2>/dev/null && pwd)" || path=""
        if [[ -n "$path" && -d "$path" ]]; then
            echo "$path"
            return 0
        fi
        echo "Error: '$raw' is not a valid directory" >&2
        (( attempt++ ))
        local remaining=$(( max - attempt ))
        (( remaining > 0 )) && echo "Please try again (${remaining} attempt(s) remaining)"
    done
    echo "Too many invalid attempts. Exiting." >&2
    return 1
}

prompt_skill_selection() {
    # $@: "name||desc" 条目列表
    local items=("$@") i=1
    echo ""
    echo "Available skills:"
    for entry in "${items[@]}"; do
        local name="${entry%%||*}" desc="${entry#*||}"
        echo "  [${i}] ${name} — ${desc}"
        (( i++ ))
    done

    echo ""
    read -rp "Select skills to install (comma-separated numbers, or 'all'): " raw
    raw="$(echo "$raw" | xargs)"

    if [[ -z "$raw" || "${raw,,}" == "all" ]]; then
        for entry in "${items[@]}"; do echo "${entry%%||*}"; done
        return
    fi

    local selected=()
    IFS=',' read -ra parts <<< "$raw"
    for part in "${parts[@]}"; do
        part="$(echo "$part" | xargs)"
        [[ -z "$part" ]] && continue
        if [[ "$part" =~ ^[0-9]+$ ]]; then
            local idx=$(( part - 1 ))
            if (( idx >= 0 && idx < ${#items[@]} )); then
                selected+=("${items[idx]%%||*}")
            else
                echo "  Out of range: ${part}" >&2
            fi
        else
            echo "  Invalid input: '${part}'" >&2
        fi
    done

    if [[ ${#selected[@]} -eq 0 ]]; then
        echo "No valid skills selected." >&2
        return 1
    fi
    printf '%s\n' "${selected[@]}"
}

prompt_conflict_action() {
    local skill_name="$1" choice
    while true; do
        read -rp "  Skill '${skill_name}' already exists. [s]kip / [o]verwrite / [b]ackup and overwrite: " choice
        choice="${choice,,}"
        case "$choice" in
            s|skip) echo "skip"; return ;;
            o|overwrite) echo "overwrite"; return ;;
            b|backup) echo "backup_and_overwrite"; return ;;
            *) echo "  Invalid choice. Use 's', 'o', or 'b'." ;;
        esac
    done
}

install_dependencies() {
    echo "  Running: pip install $*"
    if python3 -m pip install "$@"; then
        echo "  Dependencies installed successfully."
    else
        echo "  Error: pip install failed" >&2
        return 1
    fi
}

# ── 主逻辑 ──

main() {
    local target="" source="" skills_arg="" force=0 skip_deps=0

    # 参数解析
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --target) target="$2"; shift 2 ;;
            --source) source="$2"; shift 2 ;;
            --skills) skills_arg="$2"; shift 2 ;;
            --force) force=1; shift ;;
            --skip-deps) skip_deps=1; shift ;;
            -h|--help) usage ;;
            *) die "Unknown option: $1" ;;
        esac
    done

    echo ""
    echo "=== Claude Code Skills Installer ==="
    echo ""

    # 1. 扫描可用技能
    local src_dir="${source:-${SKILLS_DIR}}"
    src_dir="$(cd "$src_dir" 2>/dev/null && pwd)" || die "Source directory not found: ${source:-${SKILLS_DIR}}"

    # 关联数组: skill_name -> "source_path||description"
    declare -A SKILL_PATHS SKILL_DESCS
    local entries=()

    for item in "$src_dir"/*/; do
        [[ -d "$item" ]] || continue
        local parsed
        parsed="$(parse_skill_manifest "$item")" || {
            warn "skipping '$(basename "$item")' — no valid ${SKILL_MANIFEST} found"
            continue
        }
        local sname="${parsed%%||*}" sdesc="${parsed#*||}"
        SKILL_PATHS["$sname"]="${item%/}"
        SKILL_DESCS["$sname"]="$sdesc"
        entries+=("${sname}||${sdesc}")
    done

    if [[ ${#SKILL_PATHS[@]} -eq 0 ]]; then
        die "No skills found."
    fi

    # 2. 获取目标路径
    local target_path
    if [[ -n "$target" ]]; then
        target_path="$(cd "$target" 2>/dev/null && pwd)" || die "Target path does not exist or is not a directory: $target"
    else
        target_path="$(prompt_target)" || exit 1
    fi

    # 3. 选择技能
    local selected_names=()
    if [[ -n "$skills_arg" ]]; then
        IFS=',' read -ra parts <<< "$skills_arg"
        for part in "${parts[@]}"; do
            local sname="$(echo "$part" | xargs)"
            [[ -z "$sname" ]] && continue
            [[ -n "${SKILL_PATHS[$sname]+_}" ]] || die "Unknown skill: $sname"
            selected_names+=("$sname")
        done
    else
        while IFS= read -r sname; do
            selected_names+=("$sname")
        done < <(prompt_skill_selection "${entries[@]}") || exit 1
    fi

    [[ ${#selected_names[@]} -eq 0 ]] && die "No skills selected."

    # 4. 执行安装
    local target_skills_dir="${target_path}/.claude/skills"
    mkdir -p "$target_skills_dir"

    local installed=0 sname source_skill_dir target_skill_dir existing mode
    for sname in "${selected_names[@]}"; do
        source_skill_dir="${SKILL_PATHS[$sname]}"
        target_skill_dir="${target_skills_dir}/${sname}"

        echo ""
        echo "Installing: ${sname}"

        existing="$(check_existing_skill "$target_skills_dir" "$sname" "$source_skill_dir")"

        if [[ "$existing" == "identical" && $force -eq 0 ]]; then
            echo "  Already installed and up-to-date, skipping."
            continue
        fi

        if [[ "$existing" == "identical" && $force -eq 1 ]]; then
            mode="overwrite"
        elif [[ "$existing" == "different" ]]; then
            if [[ $force -eq 1 ]]; then
                mode="overwrite"
            else
                local action
                action="$(prompt_conflict_action "$sname")"
                if [[ "$action" == "skip" ]]; then
                    echo "  Skipped."
                    continue
                fi
                mode="$action"
            fi
        else
            mode="copy"
        fi

        if install_skill "$source_skill_dir" "$target_skill_dir" "$mode"; then
            verify_installation "$target_skill_dir"
            (( installed++ )) || true
        fi
    done

    # 5. 依赖检查
    if [[ $skip_deps -eq 0 && $installed -gt 0 ]]; then
        local all_missing=()
        for sname in "${selected_names[@]}"; do
            [[ -n "${SKILL_PATHS[$sname]+_}" ]] || continue
            target_skill_dir="${target_skills_dir}/${sname}"
            [[ -d "$target_skill_dir" ]] || continue

            local missing
            missing="$(check_dependencies "${SKILL_PATHS[$sname]}")" || true
            if [[ -n "$missing" ]]; then
                while IFS= read -r dep; do
                    [[ -n "$dep" ]] && echo "  [WARNING] ${dep} is required by ${sname} but not installed."
                done <<< "$missing"
                all_missing+=($missing)
            fi
        done

        if [[ ${#all_missing[@]} -gt 0 ]]; then
            # 去重并排序
            local unique_deps
            unique_deps="$(printf '%s\n' "${all_missing[@]}" | sort -u | xargs)"
            echo ""
            read -rp "  Install missing dependencies? (pip install ${unique_deps}) [y/N]: " choice
            choice="${choice,,}"
            if [[ "$choice" == "y" || "$choice" == "yes" ]]; then
                install_dependencies $unique_deps
            fi
        fi
    fi

    # 6. 汇总
    echo ""
    echo "Installation complete. ${installed} skill(s) installed to ${target_skills_dir}"
    if [[ $installed -gt 0 ]]; then
        echo "Restart Claude Code in the target project to activate the skills."
    fi
    echo ""
}

main "$@"
