"""``python -m ctfagent`` → 启动 Web 服务。"""

from __future__ import annotations

from .server import main

if __name__ == "__main__":
    raise SystemExit(main())
