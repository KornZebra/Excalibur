"""内置文件系统工具：read_file / write_file / list_dir / glob / grep / download。"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any

from ...models.types import ToolOutcome
from ..context import ToolContext
from ..guard import soft_failure, wrap_exception
from .paths import PathScopeError, display_path, resolve_path

#: 单次读取上限（防止把整个大文件灌进上下文）
MAX_READ_BYTES = 512 * 1024


def _numbered(lines: list[str], start: int) -> str:
    width = len(str(start + len(lines)))
    return "\n".join(f"{str(start + i).rjust(width)}| {line}" for i, line in enumerate(lines))


def _undecodable_name(name: str) -> bool:
    """文件名是否含无法编码为 UTF-8 的字节（即 surrogateescape 留下的代理字符）。

    CTF 题目压缩包内的文件名常是原始/加密字节，解压后 ``os.listdir`` 会以
    surrogateescape 解码带出 U+D800–U+DFFF。这类名称无法用路径字符串访问，
    需要在输出里显式提示模型改用 bash/python 按字节处理。
    """
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------
async def read_file(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    try:
        path = resolve_path(str(args.get("file") or args.get("path") or ""), ctx=ctx, must_exist=True)
    except (PathScopeError, FileNotFoundError, ValueError) as exc:
        return ToolOutcome.error(
            soft_failure(str(exc), tool="read_file", hint="先用 list_dir 确认文件路径。")
        )
    offset = max(1, int(args.get("offset") or 1))
    limit = int(args.get("limit") or 400)
    limit = max(1, min(limit, 5_000))
    try:
        size = path.stat().st_size
        if size > MAX_READ_BYTES and size > 0:
            with open(path, "rb") as fh:
                head = fh.read(MAX_READ_BYTES)
            text = head.decode("utf-8", errors="replace")
            note = f"\n\n[read_file] 文件共 {size} 字节，仅读取前 {MAX_READ_BYTES} 字节；如需完整内容请用 offset/limit 分段读取或改用 grep。"
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
            note = ""
    except OSError as exc:
        return ToolOutcome.error(wrap_exception(exc, tool="read_file"))
    lines = text.splitlines()
    total = len(lines)
    window = lines[offset - 1 : offset - 1 + limit]
    if not window:
        return ToolOutcome.ok(f"{display_path(path, ctx)} 共 {total} 行，offset={offset} 处没有内容。")
    body = _numbered(window, offset)
    header = f"# {display_path(path, ctx)}（共 {total} 行，显示第 {offset}-{offset + len(window) - 1} 行）"
    return ToolOutcome.ok(f"{header}\n{body}{note}")


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------
async def write_file(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    try:
        path = resolve_path(str(args.get("file") or args.get("path") or ""), ctx=ctx, for_write=True)
    except (PathScopeError, ValueError) as exc:
        return ToolOutcome.error(
            soft_failure(str(exc), tool="write_file", hint="写入只允许在 workspace 内。")
        )
    content = args.get("content")
    if content is None:
        return ToolOutcome.error(soft_failure("缺少 content 参数", tool="write_file"))
    mode = str(args.get("mode", "overwrite")).lower()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append":
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(str(content))
        elif mode == "create_new":
            if path.exists():
                return ToolOutcome.error(
                    soft_failure(f"文件已存在（mode=create_new）：{path}", tool="write_file")
                )
            path.write_text(str(content), encoding="utf-8")
        else:
            path.write_text(str(content), encoding="utf-8")
    except OSError as exc:
        return ToolOutcome.error(wrap_exception(exc, tool="write_file"))
    size = path.stat().st_size
    return ToolOutcome.ok(f"已写入 {display_path(path, ctx)}（{size} 字节，mode={mode}）。")


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------
async def list_dir(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    raw = str(args.get("path") or ".")
    try:
        path = resolve_path(raw, ctx=ctx, must_exist=True)
    except (PathScopeError, FileNotFoundError, ValueError) as exc:
        return ToolOutcome.error(soft_failure(str(exc), tool="list_dir"))
    if path.is_file():
        return ToolOutcome.ok(f"{display_path(path, ctx)} 是文件，不是目录。")
    recursive = bool(args.get("recursive", False))
    max_entries = int(args.get("max_entries") or 500)
    entries: list[str] = []
    undecodable = 0
    try:
        if recursive:
            for root, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", ".venv"}]
                for name in sorted(files):
                    if _undecodable_name(name):
                        undecodable += 1
                    full = Path(root) / name
                    entries.append(
                        f"{display_path(full, ctx)} ({full.stat().st_size} B)"
                        if full.exists()
                        else display_path(full, ctx)
                    )
                    if len(entries) >= max_entries:
                        break
                if len(entries) >= max_entries:
                    break
        else:
            for item in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name)):
                if _undecodable_name(item.name):
                    undecodable += 1
                if item.is_dir():
                    entries.append(f"{display_path(item, ctx)}/")
                else:
                    try:
                        entries.append(f"{display_path(item, ctx)} ({item.stat().st_size} B)")
                    except OSError:
                        entries.append(display_path(item, ctx))
                if len(entries) >= max_entries:
                    break
    except OSError as exc:
        return ToolOutcome.error(wrap_exception(exc, tool="list_dir"))
    if not entries:
        return ToolOutcome.ok(f"{display_path(path, ctx)} 为空目录。")
    suffix = f"\n...（已截断，最多 {max_entries} 条）" if len(entries) >= max_entries else ""
    if undecodable:
        suffix += (
            f"\n[注意] 有 {undecodable} 个文件名包含非 UTF-8 字节（显示为 \ufffd），"
            "无法用路径字符串直接访问。请改用 bash 通配（如 `for f in *; do ...; done`）"
            "或 python（`os.listdir(os.fsencode(dir))` 后按字节 open）处理。"
        )
    return ToolOutcome.ok(f"# {display_path(path, ctx)} 内容：\n" + "\n".join(entries) + suffix)


# ---------------------------------------------------------------------------
# glob
# ---------------------------------------------------------------------------
async def glob_files(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    pattern = str(args.get("pattern") or "*")
    raw_path = str(args.get("path") or ".")
    try:
        base = resolve_path(raw_path, ctx=ctx, must_exist=True)
    except (PathScopeError, FileNotFoundError, ValueError) as exc:
        return ToolOutcome.error(soft_failure(str(exc), tool="glob"))
    if base.is_file():
        base = base.parent
    max_results = int(args.get("max_results") or 200)
    matches: list[str] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(base))
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(path.name, pattern):
            try:
                matches.append(f"{display_path(path, ctx)} ({path.stat().st_size} B)")
            except OSError:
                matches.append(display_path(path, ctx))
            if len(matches) >= max_results:
                break
    if not matches:
        return ToolOutcome.ok(f"在 {display_path(base, ctx)} 下没有匹配 {pattern} 的文件。")
    return ToolOutcome.ok(f"匹配 {pattern} 的文件：\n" + "\n".join(matches))


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------
async def grep_files(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    pattern = str(args.get("pattern") or "")
    if not pattern:
        return ToolOutcome.error(soft_failure("缺少 pattern 参数", tool="grep"))
    raw_path = str(args.get("path") or ".")
    try:
        base = resolve_path(raw_path, ctx=ctx, must_exist=True)
    except (PathScopeError, FileNotFoundError, ValueError) as exc:
        return ToolOutcome.error(soft_failure(str(exc), tool="grep"))
    include = str(args.get("include") or "").strip()
    ignore_case = bool(args.get("ignore_case", False))
    max_results = int(args.get("max_results") or 120)
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        return ToolOutcome.error(
            soft_failure(f"正则表达式非法：{exc}", tool="grep", hint="检查括号/转义；或改用固定字符串。")
        )

    files: list[Path] = []
    if base.is_file():
        files = [base]
    else:
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                continue
            if any(part in {".git", "__pycache__", ".venv", "node_modules"} for part in path.parts):
                continue
            if include and not (fnmatch.fnmatch(path.name, include) or fnmatch.fnmatch(str(path), include)):
                continue
            files.append(path)

    hits: list[str] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                hits.append(f"{display_path(path, ctx)}:{lineno}: {line.strip()[:240]}")
                if len(hits) >= max_results:
                    break
        if len(hits) >= max_results:
            break
    if not hits:
        return ToolOutcome.ok(f"在 {display_path(base, ctx)} 下没有匹配 /{pattern}/ 的内容。")
    return ToolOutcome.ok(f"匹配 /{pattern}/ 的结果：\n" + "\n".join(hits))


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------
async def download_file(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    url = str(args.get("url") or "").strip()
    if not url:
        return ToolOutcome.error(soft_failure("缺少 url 参数", tool="download"))
    if not url.lower().startswith(("http://", "https://")):
        return ToolOutcome.error(
            soft_failure("只支持 http/https 下载", tool="download", hint="本地文件请直接用 read_file。")
        )
    filename = str(args.get("filename") or "").strip() or url.split("/")[-1].split("?")[0] or "download.bin"
    try:
        path = resolve_path(filename, ctx=ctx, for_write=True)
    except (PathScopeError, ValueError) as exc:
        return ToolOutcome.error(soft_failure(str(exc), tool="download"))
    max_bytes = int(args.get("max_bytes") or 200 * 1024 * 1024)
    timeout = float(args.get("timeout_seconds") or 120)
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                path.parent.mkdir(parents=True, exist_ok=True)
                total = 0
                with open(path, "wb") as fh:
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            return ToolOutcome.error(
                                soft_failure(f"下载超过上限 {max_bytes} 字节，已中止。", tool="download")
                            )
                        fh.write(chunk)
        return ToolOutcome.ok(
            f"已下载 {url} → {display_path(path, ctx)}（{total} 字节）。"
            "下一步建议用 file / exiftool 鉴别文件类型。"
        )
    except Exception as exc:  # noqa: BLE001 - 网络错误统一软失败
        return ToolOutcome.error(
            wrap_exception(exc, tool="download", hint="检查 URL 可达性与是否需要代理；题目附件优先本地路径。")
        )


__all__ = [
    "download_file",
    "glob_files",
    "grep_files",
    "list_dir",
    "read_file",
    "write_file",
]
