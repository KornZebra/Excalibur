"""FastAPI 组装与生命周期（lifespan）。

- 启动：加载 Runtime（数据库/工具平面/模型/子代理/MCP），挂载静态前端与全部路由；
- 关闭：取消运行中的 Agent、断开 MCP、关闭模型与数据库连接。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api import (
    routes_admin,
    routes_agent,
    routes_attachments,
    routes_challenge,
    routes_competition,
    routes_config,
    routes_models,
    routes_data,
    routes_projects,
    routes_stats,
    routes_steer,
)
from .config import AppConfig, get_config
from .constants import APP_NAME, COMPLIANCE_STATEMENT, PRODUCT_FULL, __version__
from .runtime import Runtime

logger = logging.getLogger(__name__)


def _web_dir(config: AppConfig) -> Path:
    return config.project_root / "web"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config: AppConfig = app.state.config
    approver = getattr(app.state, "approver", None)
    logger.info("正在初始化 %s v%s（项目根 %s）", APP_NAME, __version__, config.project_root)
    runtime = await Runtime.create(config, approver=approver)
    app.state.runtime = runtime
    try:
        yield
    finally:
        logger.info("正在关闭 Runtime…")
        await runtime.close()
        app.state.runtime = None


def create_app(config: AppConfig | None = None, *, approver: Any = None) -> FastAPI:
    """创建 FastAPI 应用。``approver`` 非空时用其做 HITL 审批（CLI/测试）。"""
    config = config or get_config()
    app = FastAPI(
        title=PRODUCT_FULL,
        version=__version__,
        description=(
            "以 LLM 为大脑、MCP 风格工具为手脚的 CTF 自主解题智能体平台。\n\n" + COMPLIANCE_STATEMENT
        ),
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.approver = approver

    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _no_cache_for_shell(request: Request, call_next: Any) -> Any:
        """前端壳与脚本强制协商缓存（no-cache）。

        StaticFiles 只带 ETag/Last-Modified，浏览器会启发式缓存 JS——发版后页面
        经常还在跑旧脚本（表现为"改了但没反应"）。这里对静态壳强制每次回源校验：
        未变更 304 代价极小，变更必定生效。
        """
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/js/") or path.startswith("/css/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.include_router(routes_agent.router)
    app.include_router(routes_admin.router)
    app.include_router(routes_attachments.router)
    app.include_router(routes_challenge.router)
    app.include_router(routes_competition.router)
    app.include_router(routes_config.router)
    # 模型目录 / 会话级模型选择 / 模型通道管理（DSH ModelSelect 对应能力）
    app.include_router(routes_models.router)
    app.include_router(routes_data.router)
    app.include_router(routes_projects.router)
    # U14：数据看板统计（一次性聚合）
    app.include_router(routes_stats.router)
    # 运行中引导（Steering）：统一消息入口 / 会话事件流 / 状态 / 取消
    app.include_router(routes_steer.router)

    web_dir = _web_dir(config)
    if web_dir.is_dir():
        # 挂在最后：API 路由优先匹配，其余落到静态前端（html=True 使 / 命中 index.html）
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
        logger.info("已挂载静态前端：%s", web_dir)
    else:  # pragma: no cover - 前端缺失时的提示

        @app.get("/")
        async def index_missing() -> dict[str, str]:
            return {
                "message": "未找到 web/ 静态前端目录，请检查项目结构。",
                "api": "/docs",
            }

    return app


__all__ = ["create_app", "lifespan"]
