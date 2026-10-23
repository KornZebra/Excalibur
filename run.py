#!/usr/bin/env python3
"""一键启动：建 venv → 装依赖 → 起服务。

用法：
    python3 run.py                 # 建/复用 .venv，安装依赖并启动 Web 服务
    python3 run.py --skip-install  # 跳过依赖安装直接启动
    python3 run.py --cli "题目描述"  # 转交给 CLI 模式

仅依赖 Python 标准库实现引导逻辑；真正的依赖安装在 .venv 里完成。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import venv
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
VENV_DIR = PROJECT_ROOT / ".venv"


def venv_python() -> Path:
    if os.name == "nt":  # pragma: no cover - Windows
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def ensure_venv() -> Path:
    """确保虚拟环境存在，返回其中的 python 可执行文件路径。"""
    python = venv_python()
    if python.is_file():
        return python
    print(f"[run.py] 创建虚拟环境：{VENV_DIR}")
    venv.EnvBuilder(with_pip=True, clear=False, upgrade_deps=False).create(str(VENV_DIR))
    if not python.is_file():  # pragma: no cover
        print("[run.py] 虚拟环境创建失败，回退到系统 Python", file=sys.stderr)
        return Path(sys.executable)
    return python


def install_deps(python: Path, *, extras: str = "") -> int:
    spec = f".[{extras}]" if extras else "."
    print(f"[run.py] 安装依赖：pip install -e {spec}")
    code = subprocess.call(
        [str(python), "-m", "pip", "install", "--upgrade", "pip", "--quiet"],
        cwd=str(PROJECT_ROOT),
    )
    if code != 0:  # pragma: no cover
        print("[run.py] pip 升级失败（继续尝试安装依赖）", file=sys.stderr)
    return subprocess.call([str(python), "-m", "pip", "install", "-e", spec], cwd=str(PROJECT_ROOT))


def ensure_config() -> None:
    config = PROJECT_ROOT / "config.yaml"
    example = PROJECT_ROOT / "config.example.yaml"
    if not config.exists() and example.exists():
        config.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[run.py] 已从 config.example.yaml 生成 {config}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CTF-Agent 一键启动")
    parser.add_argument("--skip-install", action="store_true", help="跳过依赖安装")
    parser.add_argument("--host", default=None, help="监听地址")
    parser.add_argument("--port", type=int, default=None, help="监听端口")
    parser.add_argument("--extras", default="", help='可选依赖组，如 "dev,mcp"')
    parser.add_argument("--cli", nargs=argparse.REMAINDER, help="改为运行 CLI 模式（其后参数原样透传）")
    args = parser.parse_args(argv)

    ensure_config()
    python = ensure_venv()
    if not args.skip_install:
        code = install_deps(python, extras=args.extras)
        if code != 0:
            print("[run.py] 依赖安装失败，请检查网络或手动执行 pip install -e .", file=sys.stderr)
            return code

    if args.cli:
        cmd = [str(python), "-m", "ctfagent.cli", *args.cli]
    else:
        cmd = [str(python), "-m", "ctfagent.server"]
        if args.host:
            cmd += ["--host", args.host]
        if args.port:
            cmd += ["--port", str(args.port)]

    print(f"[run.py] 启动：{' '.join(cmd)}")
    try:
        return subprocess.call(cmd, cwd=str(PROJECT_ROOT))
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
