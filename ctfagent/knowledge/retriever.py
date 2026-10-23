"""知识库检索工具：``search_knowledge_base``（M5 增强项）。

语料默认取自 ``skills/``（writeup 方法论、可复用技巧），索引落盘 ``data/knowledge.jsonl``。
未启用或语料为空时返回软失败并提示如何准备语料。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..models.types import ToolOutcome
from ..tools.context import ToolContext
from ..tools.guard import soft_failure
from ..tools.registry import ToolDefinition, ToolRegistry
from .indexer import KnowledgeIndex

logger = logging.getLogger(__name__)

_INDEX_CACHE: dict[str, KnowledgeIndex] = {}


def get_index(config: AppConfig, *, rebuild: bool = False) -> KnowledgeIndex:
    """获取（并按需构建）知识库索引，进程内缓存。"""
    key = f"{config.knowledge.corpus_dir}::{config.knowledge.index_path}"
    if not rebuild and key in _INDEX_CACHE:
        return _INDEX_CACHE[key]

    index = KnowledgeIndex()
    index_path = Path(config.knowledge.index_path)
    if not index_path.is_absolute():
        index_path = config.project_root / index_path
    corpus_dir = Path(config.knowledge.corpus_dir)
    if not corpus_dir.is_absolute():
        corpus_dir = config.project_root / corpus_dir

    loaded = index.load(index_path)
    corpus_files = list(corpus_dir.rglob("*.md")) if corpus_dir.is_dir() else []
    if rebuild or loaded == 0 or len(corpus_files) > loaded:
        count = index.build(
            corpus_dir,
            chunk_size=config.knowledge.chunk_size,
            chunk_overlap=config.knowledge.chunk_overlap,
        )
        try:
            index.save(index_path)
        except OSError as exc:  # pragma: no cover
            logger.warning("知识库索引落盘失败：%s", exc)
        logger.info("知识库索引构建完成：%d 块（语料 %s）", count, corpus_dir)
    _INDEX_CACHE[key] = index
    return index


def register_knowledge_tools(registry: ToolRegistry, config: AppConfig) -> list[ToolDefinition]:
    """注册 ``search_knowledge_base`` 工具。"""

    async def _search(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        if not ctx.config.knowledge.enabled:
            return ToolOutcome.error(
                soft_failure(
                    "知识库未启用。",
                    tool="search_knowledge_base",
                    hint="在 config.yaml 中设置 knowledge.enabled=true，并把 writeup 语料放到 skills/ 目录。",
                )
            )
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolOutcome.error(soft_failure("缺少 query 参数", tool="search_knowledge_base"))
        top_k = int(args.get("top_k") or ctx.config.knowledge.top_k)
        rebuild = bool(args.get("rebuild", False))
        index = get_index(ctx.config, rebuild=rebuild)
        if len(index) == 0:
            return ToolOutcome.error(
                soft_failure(
                    "知识库为空（没有可检索的语料块）。",
                    tool="search_knowledge_base",
                    hint="把 writeup/技巧 Markdown 放到 skills/ 下，或用 rebuild=true 重建索引。",
                )
            )
        results = index.search(query, top_k=top_k)
        if not results:
            return ToolOutcome.ok(
                f"知识库中没有与「{query}」相关的内容（索引共 {len(index)} 块）。"
                "可换更具体的关键词（如 'padding oracle'、'fastbin dup'、'JWT alg none'）。"
            )
        blocks = [
            f"### {r['title']}（score={r['score']}）\n来源：{r['path']}\n\n{r['text'][:1200]}"
            for r in results
        ]
        return ToolOutcome.ok(
            f"知识库命中 {len(results)} 条（索引 {len(index)} 块）：\n\n"
            + "\n\n---\n\n".join(blocks)
            + "\n\n注意：知识库内容为历史经验，必须结合当前题目实际验证后再使用。"
        )

    tool = ToolDefinition(
        name="search_knowledge_base",
        short_description="检索本地 writeup/技巧知识库（BM25 词法召回）",
        description=(
            "在你的经验不足以判断下一步时检索本地知识库：payload 套路、漏洞利用要点、"
            "工具用法、writeup 方法论。命中内容需结合当前题目验证。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词，如 'padding oracle'"},
                "top_k": {"type": "integer", "description": "返回条数，默认取配置"},
                "rebuild": {"type": "boolean", "description": "是否强制重建索引（语料更新后用）"},
            },
            "required": ["query"],
        },
        handler=_search,
        category="knowledge",
        always_expose=True,
    )
    registry.register(tool)
    return [tool]


__all__ = ["get_index", "register_knowledge_tools"]
