"""知识库索引：Markdown 语料切块 + BM25 检索（M5 增强项，零外部依赖）。

设计取舍：CTF writeup/技巧库规模通常在几百个文档量级，词法检索（BM25）已足够，
且不依赖 embedding 服务即可离线工作。若配置了 embedding 通道，可在此扩展向量召回
（:class:`KnowledgeIndex` 预留了 ``embeddings`` 字段与 :meth:`search_hybrid` 接口）。
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ASCII_WORD = re.compile(r"[A-Za-z0-9_]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    """分词：ASCII 词 + 中文单字与二元组（无需 jieba 也能有基本召回）。"""
    text = (text or "").lower()
    tokens: list[str] = [w for w in _ASCII_WORD.findall(text) if len(w) > 1 or w.isdigit()]
    cjk_chars = _CJK.findall(text)
    tokens.extend(cjk_chars)
    tokens.extend(cjk_chars[i] + cjk_chars[i + 1] for i in range(len(cjk_chars) - 1))
    return tokens


@dataclass
class Chunk:
    """一个知识块。"""

    id: str
    title: str
    path: str
    text: str
    tags: list[str] = field(default_factory=list)
    embeddings: list[float] | None = None


def chunk_text(text: str, *, size: int = 800, overlap: int = 120) -> list[str]:
    """按段落边界切块，块间保留 overlap 个字符的上下文。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        # 尽量在换行处断开
        if end < len(text):
            window = text.rfind("\n", start + int(size * 0.6), end)
            if window > 0:
                end = window
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


class KnowledgeIndex:
    """BM25 词法索引（可落盘为 JSONL）。"""

    def __init__(self, *, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.chunks: list[Chunk] = []
        self._doc_tokens: list[Counter[str]] = []
        self._doc_len: list[int] = []
        self._df: Counter[str] = Counter()
        self._avg_len: float = 0.0

    # ------------------------------------------------------------------
    def add_document(
        self,
        *,
        title: str,
        path: str,
        text: str,
        tags: list[str] | None = None,
        chunk_size: int = 800,
        chunk_overlap: int = 120,
    ) -> int:
        added = 0
        for index, piece in enumerate(chunk_text(text, size=chunk_size, overlap=chunk_overlap)):
            self.chunks.append(
                Chunk(
                    id=f"{path}#{index}",
                    title=title,
                    path=path,
                    text=piece,
                    tags=list(tags or []),
                )
            )
            added += 1
        return added

    def build(self, corpus_dir: str | Path, *, chunk_size: int = 800, chunk_overlap: int = 120) -> int:
        """从目录递归加载 ``*.md`` 语料并建索引。"""
        corpus_dir = Path(corpus_dir)
        if not corpus_dir.is_dir():
            logger.warning("知识库语料目录不存在：%s", corpus_dir)
            return 0
        self.chunks.clear()
        for path in sorted(corpus_dir.rglob("*.md")):
            if any(part.startswith(".") for part in path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            title = path.stem
            tags = [p.name for p in path.parents[:2] if p.name and p.name != corpus_dir.name]
            self.add_document(
                title=title,
                path=str(path.relative_to(corpus_dir.parent)),
                text=text,
                tags=tags,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )
        self._reindex()
        return len(self.chunks)

    def _reindex(self) -> None:
        self._doc_tokens = [Counter(tokenize(c.text)) for c in self.chunks]
        self._doc_len = [sum(counter.values()) for counter in self._doc_tokens]
        self._df = Counter()
        for counter in self._doc_tokens:
            self._df.update(counter.keys())
        self._avg_len = (sum(self._doc_len) / len(self._doc_len)) if self._doc_len else 0.0

    # ------------------------------------------------------------------
    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """BM25 检索，返回 ``[{title, path, text, score, tags}]``。"""
        if not self.chunks:
            return []
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        n = len(self.chunks)
        scores: list[float] = [0.0] * n
        for token in set(q_tokens):
            df = self._df.get(token, 0)
            if df == 0:
                continue
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for index, counter in enumerate(self._doc_tokens):
                tf = counter.get(token, 0)
                if tf == 0:
                    continue
                length = self._doc_len[index] or 1
                denom = tf + self.k1 * (1 - self.b + self.b * length / (self._avg_len or 1))
                scores[index] += idf * tf * (self.k1 + 1) / denom
        ranked = sorted(range(n), key=lambda i: -scores[i])
        results: list[dict[str, Any]] = []
        for index in ranked[: max(1, top_k)]:
            if scores[index] <= 0:
                continue
            chunk = self.chunks[index]
            results.append(
                {
                    "title": chunk.title,
                    "path": chunk.path,
                    "text": chunk.text,
                    "score": round(scores[index], 4),
                    "tags": chunk.tags,
                }
            )
        return results

    def search_hybrid(
        self, query: str, top_k: int = 5, *, embedding: list[float] | None = None
    ) -> list[dict[str, Any]]:
        """预留接口：有 embedding 时可做词法+向量混合召回（当前退化为纯词法）。"""
        return self.search(query, top_k=top_k)

    # ------------------------------------------------------------------
    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            for chunk in self.chunks:
                fh.write(json.dumps(asdict(chunk), ensure_ascii=False) + "\n")
        return path

    def load(self, path: str | Path) -> int:
        path = Path(path)
        if not path.is_file():
            return 0
        self.chunks.clear()
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self.chunks.append(
                    Chunk(
                        id=data.get("id", ""),
                        title=data.get("title", ""),
                        path=data.get("path", ""),
                        text=data.get("text", ""),
                        tags=data.get("tags") or [],
                        embeddings=data.get("embeddings"),
                    )
                )
        self._reindex()
        return len(self.chunks)

    def __len__(self) -> int:
        return len(self.chunks)


__all__ = ["Chunk", "KnowledgeIndex", "chunk_text", "tokenize"]
