"""对话附件（U2a）：文件名消毒、白名单校验、落盘、元数据与 Agent 附件清单块。

设计要点：
- 上传校验顺序：开关 → 单条消息数量上限 → 单文件体积 → 扩展名白名单 → 文件名消毒；
- 扩展名白名单是**权威判据**（CTF 附件大量是 ``application/octet-stream``，只认 MIME 会误杀），
  MIME 会被记录并在明显矛盾时给出 ``mimeWarning``；下载一律 ``application/octet-stream`` +
  ``Content-Disposition: attachment`` + ``nosniff``，避免上传的 HTML/SVG 在本站 origin 内被渲染；
- 落盘路径 ``workspace/conversations/{conversation_id}/attachments/{filename}``，重名自动追加 ``-1``/``-2``；
- :func:`build_attachment_manifest` 生成注入给 Agent 的清单块（**只在有附件时才追加**，
  无附件时 Agent 输入与改造前逐字节一致）。
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any

from .config import AttachmentConfig

logger = logging.getLogger(__name__)

#: 扩展名 → 类型猜测（用于前端图标与清单块里的 kind 字段）
_KIND_BY_EXT: dict[str, str] = {
    **{e: "traffic" for e in ("pcap", "pcapng", "cap")},
    **{e: "archive" for e in ("zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "zst")},
    **{e: "binary" for e in ("exe", "dll", "elf", "bin", "so", "dex", "apk", "jar", "class", "wasm")},
    **{e: "disk" for e in ("img", "iso", "vhd", "vmdk", "ova", "dd", "raw")},
    **{e: "memory" for e in ("mem", "vmem", "dmp", "core")},
    **{e: "image" for e in ("jpg", "jpeg", "png", "bmp", "gif", "webp", "tif", "tiff", "ico", "svg")},
    **{e: "audio" for e in ("mp3", "wav")},
    **{e: "video" for e in ("mp4", "webm", "avi", "mkv")},
    **{e: "text" for e in ("txt", "md", "log", "csv", "tsv", "xml", "yaml", "yml")},
    **{
        e: "code"
        for e in (
            "py",
            "c",
            "h",
            "cpp",
            "hpp",
            "go",
            "rs",
            "java",
            "js",
            "ts",
            "php",
            "rb",
            "sh",
            "ps1",
            "bat",
            "sql",
            "html",
            "htm",
        )
    },
    **{e: "doc" for e in ("pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx")},
    **{e: "database" for e in ("db", "sqlite", "sqlite3", "sav")},
    **{e: "key" for e in ("pem", "crt", "key", "pub", "gpg")},
}

#: 扩展名 → 期望的 MIME 前缀；声明 MIME 与之矛盾时给出警告（仅提示，不阻断）
_EXPECTED_MIME: dict[str, tuple[str, ...]] = {
    "png": ("image/png",),
    "jpg": ("image/jpeg",),
    "jpeg": ("image/jpeg",),
    "gif": ("image/gif",),
    "webp": ("image/webp",),
    "pdf": ("application/pdf",),
    "zip": ("application/zip", "application/x-zip"),
    "gz": ("application/gzip", "application/x-gzip"),
    "exe": (
        "application/x-dosexec",
        "application/vnd.microsoft.portable-executable",
        "application/x-msdownload",
        "application/octet-stream",
    ),
}

_UNSAFE_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff ]+")


class AttachmentError(ValueError):
    """附件校验失败（路由层转 4xx）。"""

    def __init__(self, message: str, *, status: int = 400, filename: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.filename = filename


def extension_of(filename: str) -> str:
    """取小写扩展名（无点）。``a.tar.gz`` → ``gz``。"""
    name = (filename or "").strip()
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[1].lower()


def kind_of(filename: str) -> str:
    """按扩展名给出类型猜测。"""
    return _KIND_BY_EXT.get(extension_of(filename), "unknown")


def sanitize_filename(filename: str) -> str:
    """消毒文件名：去路径分隔符与 ``..``、去控制字符、限长；空则回退 ``attachment``。

    例：``../../etc/passwd`` → ``passwd``，``..\\..\\win.ini`` → ``win.ini``。
    """
    raw = (filename or "").replace("\\", "/")
    raw = raw.split("/")[-1]  # 去目录部分（含 ../ 与绝对路径）
    raw = raw.replace("..", "_")
    raw = _UNSAFE_CHARS.sub("", raw)
    raw = _UNSAFE_NAME.sub("_", raw).strip(" .")
    if not raw or not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", raw):
        return "attachment"
    if len(raw) > 180:  # 保留扩展名
        stem, _, ext = raw.rpartition(".")
        keep = 180 - (len(ext) + 1) if ext else 180
        raw = (stem[:keep] + "." + ext) if ext else raw[:180]
    return raw


def mime_warning(filename: str, mime: str) -> str:
    """声明 MIME 与扩展名明显矛盾时返回提示文本，否则空串。"""
    ext = extension_of(filename)
    expected = _EXPECTED_MIME.get(ext)
    if not expected or not mime:
        return ""
    if any(mime.startswith(prefix) for prefix in expected):
        return ""
    return f"扩展名 .{ext} 与声明的 MIME（{mime}）不一致"


def check_extension(filename: str, cfg: AttachmentConfig) -> None:
    """扩展名白名单校验（权威判据）。"""
    ext = extension_of(filename)
    allowed = {e.lower().lstrip(".") for e in cfg.allowed_extensions}
    if not ext:
        raise AttachmentError(
            f"文件 {filename} 没有扩展名，无法识别类型；请重命名后上传，或在配置 "
            "attachment.allowed_extensions 中加入所需类型。",
            status=400,
            filename=filename,
        )
    if ext not in allowed:
        raise AttachmentError(
            f"文件类型 .{ext} 不在白名单内（允许 {len(allowed)} 种）。"
            "如确需上传，请在配置 attachment.allowed_extensions 中追加该扩展名。",
            status=400,
            filename=filename,
        )


def check_size(size: int, cfg: AttachmentConfig) -> None:
    """单文件体积校验。"""
    limit = int(cfg.max_file_mb) * 1024 * 1024
    if size > limit:
        raise AttachmentError(
            f"文件超过单文件上限 {cfg.max_file_mb}MB（实际 {size / 1024 / 1024:.1f}MB）；"
            "可调整配置 attachment.max_file_mb。",
            status=413,
        )


def check_count(existing: int, incoming: int, cfg: AttachmentConfig) -> None:
    """单条消息附件数量上限校验。"""
    if existing + incoming > int(cfg.max_count):
        raise AttachmentError(
            f"附件数量超限：本条消息最多 {cfg.max_count} 个（已有 {existing}，本次 {incoming}）。",
            status=400,
        )


def unique_path(directory: Path, filename: str) -> Path:
    """重名自动追加 ``-1``/``-2``…（不覆盖已存在文件）。"""
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, _, ext = filename.rpartition(".")
    base, suffix = (stem, "." + ext) if ext else (filename, "")
    for index in range(1, 1000):
        candidate = directory / f"{base}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise AttachmentError(f"同名附件过多：{filename}", status=409, filename=filename)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def build_attachment_manifest(rows: list[dict[str, Any]]) -> str:
    """生成注入 Agent 的附件清单块（无附件时返回空串，保证既有输入不变）。

    每行：``[附件] 文件名 | 绝对路径 | 类型 | 大小 | sha256``；并附一条"先鉴别再分析"的纪律。
    """
    if not rows:
        return ""
    lines = ["[附件清单] 本轮用户上传了以下文件，已落盘，可直接用 read_file/glob/grep/bash 操作："]
    for row in rows:
        path = row.get("storedPath") or row.get("stored_path") or ""
        lines.append(
            "  [附件] {name} | {path} | {kind} | {size} | sha256:{sha}".format(
                name=row.get("filename", ""),
                path=path,
                kind=row.get("kind") or "unknown",
                size=human_size(int(row.get("size") or 0)),
                sha=(row.get("sha256") or "")[:16],
            )
        )
        warning = row.get("mimeWarning") or ""
        if warning:
            lines.append(f"    ⚠ {warning}")
    lines.append(
        "附件处理纪律：面对未知类型附件，第一步必须先用 `file`（必要时 `exiftool`/`binwalk`）"
        "鉴别真实类型再决定分析路径；图片可直接用 analyze_image 走视觉旁路；"
        "解压/反编译前先确认文件类型与大小，不要盲目爆破。"
    )
    return "\n".join(lines)
