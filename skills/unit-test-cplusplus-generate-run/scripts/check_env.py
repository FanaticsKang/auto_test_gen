#!/usr/bin/env python3
from __future__ import annotations
"""
check_env.py — 步骤 1 的环境总检测。

检查 cmake / g++ / gtest 头文件 / gmock 头文件 / gcovr / ninja 是否可用,
输出 tool_status JSON 到 stdout。

用法:
    python3 scripts/check_env.py [--auto-install-gcovr]

退出码:
    0 — 所有必需工具齐(ninja 可选,缺失不算失败)
    3 — 至少一个必需工具缺失
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


REQUIRED_TOOLS = ("cmake", "gxx", "gtest", "gmock", "gcovr")
OPTIONAL_TOOLS = ("ninja",)


def _run_version(cmd: list[str]) -> str | None:
    """运行 `<cmd> --version`,返回首行,失败返回 None。"""
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10, check=False
        )
        if out.returncode != 0:
            return None
        text = (out.stdout or out.stderr).strip()
        return text.splitlines()[0] if text else None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _extract_version(s: str | None) -> str | None:
    """从 `cmake version 3.28.3` 之类的字符串里抽出 `3.28.3`。"""
    if not s:
        return None
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", s)
    return m.group(1) if m else None


def check_cmake() -> dict:
    path = shutil.which("cmake")
    ver = _extract_version(_run_version(["cmake", "--version"])) if path else None
    return {"ok": bool(path and ver), "version": ver, "path": path}


def check_cxx() -> dict:
    """优先 g++,其次 clang++。"""
    for exe in ("g++", "clang++"):
        path = shutil.which(exe)
        if path:
            ver = _extract_version(_run_version([exe, "--version"]))
            if ver:
                return {"ok": True, "version": ver, "path": path, "name": exe}
    return {"ok": False, "version": None, "path": None, "name": None}


def check_header(header_relpath: str) -> dict:
    """在常见系统 include 路径中查找头文件。"""
    candidates = [
        "/usr/include", "/usr/local/include",
        "/opt/homebrew/include", "/opt/local/include",
    ]
    for root in candidates:
        p = Path(root) / header_relpath
        if p.is_file():
            return {"ok": True, "headers": str(p)}
    return {"ok": False, "headers": None}


def check_gcovr() -> dict:
    path = shutil.which("gcovr")
    ver = _extract_version(_run_version(["gcovr", "--version"])) if path else None
    return {"ok": bool(path and ver), "version": ver, "path": path}


def check_ninja() -> dict:
    path = shutil.which("ninja")
    ver = _extract_version(_run_version(["ninja", "--version"])) if path else None
    return {"ok": bool(path and ver), "version": ver, "path": path}


def try_install_gcovr() -> bool:
    """尝试用 pip 安装 gcovr,成功返回 True。"""
    for cmd in (
        ["pip3", "install", "--user", "--break-system-packages", "gcovr"],
        ["pip3", "install", "--user", "gcovr"],
    ):
        try:
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=300, check=False)
            if res.returncode == 0:
                # pip --user 装的可执行文件在 ~/.local/bin,加到 PATH
                user_bin = Path.home() / ".local" / "bin"
                if str(user_bin) not in os.environ.get("PATH", ""):
                    os.environ["PATH"] = f"{user_bin}{os.pathsep}{os.environ.get('PATH', '')}"
                return shutil.which("gcovr") is not None
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    return False


INSTALL_HINTS = {
    "cmake":  "sudo apt install cmake  # or: brew install cmake",
    "gxx":    "sudo apt install g++  # or: brew install gcc",
    "gtest":  "sudo apt install libgtest-dev  # 可能需手动 build /usr/src/googletest",
    "gmock":  "sudo apt install libgmock-dev",
    "gcovr":  "pip3 install --user gcovr",
    "ninja":  "sudo apt install ninja-build  # (可选)",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="环境总检测")
    parser.add_argument("--auto-install-gcovr", action="store_true",
                        help="gcovr 缺失时尝试 pip 自动安装")
    args = parser.parse_args()

    tools: dict[str, dict] = {}
    tools["cmake"] = check_cmake()
    tools["gxx"] = check_cxx()
    tools["gtest"] = check_header("gtest/gtest.h")
    tools["gmock"] = check_header("gmock/gmock.h")
    tools["gcovr"] = check_gcovr()

    if not tools["gcovr"]["ok"] and args.auto_install_gcovr:
        print("gcovr 缺失,尝试 pip install...", file=sys.stderr)
        if try_install_gcovr():
            tools["gcovr"] = check_gcovr()
            print("gcovr 安装成功", file=sys.stderr)
        else:
            print("gcovr 自动安装失败", file=sys.stderr)

    tools["ninja"] = check_ninja()

    missing = [name for name in REQUIRED_TOOLS if not tools[name]["ok"]]
    install_hints = {name: INSTALL_HINTS[name] for name in missing}

    result = {
        "all_ok": len(missing) == 0,
        "tools": tools,
        "missing": missing,
        "install_hints": install_hints,
    }

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["all_ok"] else 3


if __name__ == "__main__":
    sys.exit(main())
