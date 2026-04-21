#!/usr/bin/env python3
"""Claude Code 技能安装脚本 — 将 skills/ 目录下的技能安装到目标项目的 .claude/skills/ 下。"""

import argparse
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# 源技能目录（脚本同级）
SKILLS_DIR = Path(__file__).parent / "skills"
# 技能清单文件名
SKILL_MANIFEST = "SKILL.md"
# 最大重试次数
MAX_RETRIES = 3


def parse_skill_manifest(skill_dir: Path) -> "dict | None":
    """解析 SKILL.md 的 YAML frontmatter，返回 {name, description}。"""
    manifest_path = skill_dir / SKILL_MANIFEST
    if not manifest_path.is_file():
        return None

    content = manifest_path.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return None

    # 提取 frontmatter
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None

    meta = {}
    for line in parts[1].strip().splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()

    name = meta.get("name", skill_dir.name)
    description = meta.get("description", "")
    return {"name": name, "description": description}


def discover_skills(skills_dir: Path) -> dict:
    """扫描技能目录，返回 {name: {path, description}}。"""
    if not skills_dir.is_dir():
        print(f"Error: skills directory not found: {skills_dir}", file=sys.stderr)
        return {}

    skills = {}
    for item in sorted(skills_dir.iterdir()):
        if not item.is_dir():
            continue
        manifest = parse_skill_manifest(item)
        if manifest is None:
            print(f"Warning: skipping '{item.name}' — no valid SKILL.md found", file=sys.stderr)
            continue
        skills[manifest["name"]] = {
            "path": item,
            "description": manifest["description"],
        }
    return skills


def validate_target(path: Path) -> bool:
    """验证目标路径是否为有效目录。"""
    if not path.exists():
        print(f"Error: path does not exist: {path}", file=sys.stderr)
        return False
    if not path.is_dir():
        print(f"Error: path is not a directory: {path}", file=sys.stderr)
        return False
    return True


def check_existing_skill(target_skills_dir: Path, skill_name: str, source_path: Path) -> str:
    """检查目标位置是否已存在该技能。返回 'absent' / 'identical' / 'different'。"""
    target_manifest = target_skills_dir / skill_name / SKILL_MANIFEST
    if not target_manifest.is_file():
        return "absent"

    source_manifest = source_path / SKILL_MANIFEST
    if source_manifest.read_text(encoding="utf-8") == target_manifest.read_text(encoding="utf-8"):
        return "identical"
    return "different"


def install_skill(source_dir: Path, target_dir: Path, mode: str = "copy") -> bool:
    """安装单个技能。mode: 'copy' / 'overwrite' / 'backup_and_overwrite'。"""
    try:
        if mode == "backup_and_overwrite" and target_dir.exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = target_dir.parent / f"{target_dir.name}.bak.{timestamp}"
            print(f"  Backing up to: {backup_path}")
            target_dir.rename(backup_path)

        if mode == "overwrite" and target_dir.exists():
            shutil.rmtree(target_dir)

        shutil.copytree(source_dir, target_dir, dirs_exist_ok=True)
        return True
    except OSError as e:
        print(f"  Error installing skill: {e}", file=sys.stderr)
        return False


def verify_installation(target_dir: Path) -> bool:
    """验证安装结果。"""
    manifest = target_dir / SKILL_MANIFEST
    if not manifest.is_file():
        return False
    # 统计文件数
    refs = list((target_dir / "references").glob("*")) if (target_dir / "references").is_dir() else []
    scripts = list((target_dir / "scripts").glob("*")) if (target_dir / "scripts").is_dir() else []
    print(f"  Verified: {SKILL_MANIFEST} OK, {len(refs)} reference(s), {len(scripts)} script(s)")
    return True


def check_dependencies(skill_dir: Path) -> list[str]:
    """扫描 SKILL.md 中提到的 pip 依赖，返回缺失的包名列表。"""
    manifest_path = skill_dir / SKILL_MANIFEST
    if not manifest_path.is_file():
        return []

    content = manifest_path.read_text(encoding="utf-8")
    # 在 SKILL.md 中查找类似 pip install xxx 或 import xxx 的依赖提示
    import re
    # 匹配常见的依赖声明模式
    dep_patterns = [
        r"pip install ([\w\-]+(?:\s+[\w\-]+)*)",
        r"requires?:?\s*([\w\-]+(?:\s*,\s*[\w\-]+)*)",
    ]
    mentioned_deps = set()
    for pattern in dep_patterns:
        for match in re.finditer(pattern, content):
            for dep in re.split(r"[\s,]+", match.group(1)):
                if dep:
                    mentioned_deps.add(dep)

    if not mentioned_deps:
        return []

    # 检查哪些依赖未安装
    missing = []
    for dep in sorted(mentioned_deps):
        # 将包名转换为可 import 的模块名
        module = dep.replace("-", "_")
        try:
            __import__(module)
        except ImportError:
            missing.append(dep)
    return missing


def prompt_target() -> "Path | None":
    """交互式获取目标项目路径。"""
    for attempt in range(MAX_RETRIES):
        raw = input("\nEnter target project path: ").strip()
        if not raw:
            print("Cancelled.", file=sys.stderr)
            return None
        path = Path(raw).resolve()
        if validate_target(path):
            return path
        remaining = MAX_RETRIES - attempt - 1
        if remaining > 0:
            print(f"Please try again ({remaining} attempt(s) remaining)")
    print("Too many invalid attempts. Exiting.", file=sys.stderr)
    return None


def prompt_skill_selection(skills: dict) -> "list[str] | None":
    """交互式选择要安装的技能。返回技能名列表或 None（取消）。"""
    items = list(skills.items())
    print("\nAvailable skills:")
    for i, (name, info) in enumerate(items, 1):
        print(f"  [{i}] {name} — {info['description']}")

    raw = input("\nSelect skills to install (comma-separated numbers, or 'all'): ").strip()
    if not raw or raw.lower() == "all":
        return [name for name, _ in items]

    selected = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            idx = int(part) - 1
        except ValueError:
            print(f"  Invalid input: '{part}'", file=sys.stderr)
            continue
        if 0 <= idx < len(items):
            selected.append(items[idx][0])
        else:
            print(f"  Out of range: {part}", file=sys.stderr)

    if not selected:
        print("No valid skills selected.", file=sys.stderr)
        return None
    return selected


def prompt_conflict_action(skill_name: str) -> str:
    """交互式处理已存在的技能。返回 'skip' / 'overwrite' / 'backup_and_overwrite'。"""
    while True:
        choice = input(
            f"  Skill '{skill_name}' already exists. "
            "[s]kip / [o]verwrite / [b]ackup and overwrite: "
        ).strip().lower()
        if choice in ("s", "skip"):
            return "skip"
        if choice in ("o", "overwrite"):
            return "overwrite"
        if choice in ("b", "backup"):
            return "backup_and_overwrite"
        print("  Invalid choice. Use 's', 'o', or 'b'.")


def install_dependencies(deps: list[str]) -> bool:
    """通过 pip 安装缺失的依赖。"""
    cmd = [sys.executable, "-m", "pip", "install"] + deps
    print(f"  Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  Error: {result.stderr}", file=sys.stderr)
        return False
    print("  Dependencies installed successfully.")
    return True


def main():
    parser = argparse.ArgumentParser(description="Install Claude Code skills to a target project")
    parser.add_argument("--target", help="Target project path (interactive prompt if omitted)")
    parser.add_argument("--source", default=str(SKILLS_DIR), help="Source skills directory")
    parser.add_argument("--skills", help="Comma-separated skill names to install (default: all)")
    parser.add_argument("--force", action="store_true", help="Overwrite existing skills without prompt")
    parser.add_argument("--skip-deps", action="store_true", help="Skip dependency check")
    args = parser.parse_args()

    print("\n=== Claude Code Skills Installer ===\n")

    # 1. 扫描可用技能
    source_dir = Path(args.source).resolve()
    skills = discover_skills(source_dir)
    if not skills:
        print("No skills found. Exiting.", file=sys.stderr)
        sys.exit(1)

    # 2. 获取目标路径
    if args.target:
        target_path = Path(args.target).resolve()
        if not validate_target(target_path):
            sys.exit(1)
    else:
        target_path = prompt_target()
        if target_path is None:
            sys.exit(1)

    # 3. 选择技能
    if args.skills:
        selected_names = [s.strip() for s in args.skills.split(",")]
        # 验证
        invalid = [n for n in selected_names if n not in skills]
        if invalid:
            print(f"Error: unknown skill(s): {', '.join(invalid)}", file=sys.stderr)
            sys.exit(1)
    else:
        selected_names = prompt_skill_selection(skills)
        if selected_names is None:
            sys.exit(1)

    # 4. 执行安装
    target_skills_dir = target_path / ".claude" / "skills"
    target_skills_dir.mkdir(parents=True, exist_ok=True)

    installed = 0
    for name in selected_names:
        info = skills[name]
        source_skill_dir = info["path"]
        target_skill_dir = target_skills_dir / name

        print(f"\nInstalling: {name}")

        # 检查已存在的技能
        existing = check_existing_skill(target_skills_dir, name, source_skill_dir)
        if existing == "identical" and not args.force:
            print(f"  Already installed and up-to-date, skipping.")
            continue

        if existing == "identical" and args.force:
            mode = "overwrite"
        elif existing == "different":
            if args.force:
                mode = "overwrite"
            else:
                action = prompt_conflict_action(name)
                if action == "skip":
                    print(f"  Skipped.")
                    continue
                mode = action
        else:
            mode = "copy"

        success = install_skill(source_skill_dir, target_skill_dir, mode)
        if success:
            verify_installation(target_skill_dir)
            installed += 1

    # 5. 依赖检查
    if not args.skip_deps and installed > 0:
        all_missing = []
        for name in selected_names:
            if name not in skills:
                continue
            target_skill_dir = target_skills_dir / name
            if not target_skill_dir.is_dir():
                continue
            missing = check_dependencies(skills[name]["path"])
            for dep in missing:
                print(f"  [WARNING] {dep} is required by {name} but not installed.")
                all_missing.append(dep)

        if all_missing:
            # 去重
            unique_deps = sorted(set(all_missing))
            choice = input(f"\n  Install missing dependencies? (pip install {' '.join(unique_deps)}) [y/N]: ").strip().lower()
            if choice in ("y", "yes"):
                install_dependencies(unique_deps)

    # 6. 汇总
    print(f"\nInstallation complete. {installed} skill(s) installed to {target_skills_dir}")
    if installed > 0:
        print(f"Restart Claude Code in the target project to activate the skills.\n")


if __name__ == "__main__":
    main()
