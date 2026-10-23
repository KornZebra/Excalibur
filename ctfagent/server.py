"""uvicorn 入口：``python -m ctfagent.server`` 或 ``ctfagent-server``。"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import ConfigError, describe_secret, load_config, secret_problem
from .constants import APP_NAME, COMPLIANCE_STATEMENT, __version__


def _configure_logging(level: str, *, json_logs: bool = False) -> None:
    if json_logs:
        import json
        import time

        class _JsonFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                return json.dumps(
                    {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
                        "level": record.levelname,
                        "logger": record.name,
                        "message": record.getMessage(),
                    },
                    ensure_ascii=False,
                )

        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_JsonFormatter())
        root = logging.getLogger()
        root.handlers.clear()
        root.addHandler(handler)
        root.setLevel(level.upper())
    else:
        logging.basicConfig(
            level=level.upper(),
            format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ctf-agent", description="CTF-Agent Web 服务")
    parser.add_argument("--config", "-c", default=None, help="配置文件路径（默认自动探测 config.yaml）")
    parser.add_argument("--host", default=None, help="监听地址（覆盖配置）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（覆盖配置）")
    parser.add_argument("--log-level", default="info", help="日志级别：debug/info/warning/error")
    parser.add_argument("--json-logs", action="store_true", help="输出结构化 JSON 日志")
    parser.add_argument("--reload", action="store_true", help="开发模式自动重载（不含 Runtime 热重载）")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.log_level, json_logs=args.json_logs)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    host = args.host or config.server.host
    port = args.port or config.server.port

    print(f"{APP_NAME} v{__version__}", flush=True)
    print(COMPLIANCE_STATEMENT.strip(), flush=True)
    print(f"project_root={config.project_root}  workspace={config.workspace_root}", flush=True)
    if not config.ai.has_credentials():
        print(
            "⚠ 未检测到任何已配置的 API Key：请在 Web 界面「配置」页填写密钥并点「保存并写入本地」"
            f"（配置文件：{config.config_path or (config.project_root / 'config.yaml')}）；"
            "密钥不再从环境变量读取（本地 vLLM/Ollama 可忽略此提示）。",
            file=sys.stderr,
            flush=True,
        )
    else:
        # 每个通道打印 base_url / model / 密钥摘要：密钥只给长度+尾 4 位，且明确标出占位符
        print("LLM 通道：", flush=True)
        for name, ch in config.ai.channels.items():
            problem = secret_problem(ch.api_key, ch.base_url)
            flag = f"  ⚠ api_key {problem}" if problem else ""
            print(
                f"  - {name}: model={ch.model}  base_url={ch.base_url}\n"
                f"      api_key {describe_secret(ch.api_key)}{flag}",
                flush=True,
            )
        print(
            "  提示：可 `POST /api/config/verify`（或 Web 界面「测试通道」）真实探测每个通道；"
            "base_url 与 model 必须属于同一家服务商。",
            flush=True,
        )
    print(f"\n→ 打开 http://{host}:{port}/ 使用 Web 界面（API 文档：/docs）\n", flush=True)

    try:
        import uvicorn
    except ImportError:  # pragma: no cover
        print("缺少 uvicorn：请先执行 `pip install -e .`", file=sys.stderr)
        return 2

    from .app import create_app

    if args.reload:
        uvicorn.run(
            "ctfagent.app:create_app",
            factory=True,
            host=host,
            port=port,
            reload=True,
            log_level=args.log_level,
        )
    else:
        uvicorn.run(create_app(config), host=host, port=port, log_level=args.log_level)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
