"""全局常量：版本号、合规声明与提示词常量。

本模块中的 ``COMPLIANCE_STATEMENT`` 与 ``SCOPE_GUARD_PROMPT`` 会被写入 README、
系统提示词与代码常量，构成项目的合规硬约束，任何重构都不得移除。
"""

from __future__ import annotations

__version__ = "1.0.2"

APP_NAME = "ctf-agent"
#: 对外产品名（UI 标题 / FastAPI 文档 / CLI 描述）
PRODUCT_NAME = "湖中剑"
PRODUCT_SUB = "未被见证的黎明"
PRODUCT_FULL = "湖中剑 · 未被见证的黎明"

# ---------------------------------------------------------------------------
# 合规硬约束（写入 README、系统提示词与代码常量）
# ---------------------------------------------------------------------------
COMPLIANCE_STATEMENT = """\
合规硬约束：
- 仅用于 CTF 竞赛环境、自建靶场（本地 Docker/VM）或获得明确书面授权的目标。
- 系统提示词必须包含"目标范围校验"：未提供合法题目目标时拒绝行动并提示补充。
- 不得包含针对真实第三方目标的攻击编排；不实现 C2、WebShell 管理等持久化攻防模块。
"""

SCOPE_GUARD_PROMPT = """\
【范围与合规】
- 你只能在用户提供的题目目标（题目平台地址、靶机 IP:Port、附件文件）范围内行动。
- 目标不明确时：先用 challenge_info 查看题目元数据；仍缺失则列出"缺失信息清单"要求用户补充，禁止臆造目标。
- 不对清单之外的任何主机/域名发起扫描或利用。
- 本代理仅用于 CTF 竞赛、自建靶场与获得明确书面授权的环境；对未授权目标的任何请求一律拒绝执行。
"""

# ---------------------------------------------------------------------------
# 软失败 / 治理常量
# ---------------------------------------------------------------------------
TOOL_ERROR_PREFIX = "[TOOL_ERROR]"
HITL_REJECTED_PREFIX = "[HITL_REJECTED]"
PERSISTED_OUTPUT_TAG = "persisted-output"

#: 内置工具中永远对模型可见的核心工具（tool_search 动态解锁模式下的常驻工具）
CORE_TOOL_NAMES: tuple[str, ...] = (
    "challenge_info",
    "blackboard_upsert",
    "blackboard_get",
    "blackboard_list",
    "blackboard_search",
    "flag_submit",
    "read_file",
    "write_file",
    "list_dir",
    "glob",
    "grep",
    "bash",
    "bash_readonly",
    "python_exec",
    "report_generate",
    "write_todos",
    "tool_search",
    "get_tool_execution",
    "wait_tool_execution",
    "cancel_tool_execution",
)

#: 默认 flag 格式（可在 challenge 记录中覆盖）
DEFAULT_FLAG_PATTERN = r"(?i)\b[a-z0-9_]{2,32}\{[^{}\n]{1,256}\}"

#: bash 工具的高危模式（命中后强制走 HITL 确认，即使处于白名单模式）
DANGEROUS_COMMAND_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rf]", "递归/强制删除文件"),
    (r"\brm\s+-rf\s+/(\s|$)", "删除根目录"),
    (r"\bmkfs(\.\w+)?\b", "格式化文件系统"),
    (r"\bdd\b.*\bof=/dev/", "向块设备写入数据"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "关机/重启"),
    (r":\(\)\s*\{.*\};:", "fork 炸弹"),
    (r"\bchmod\s+-R\s+777\s+/(\s|$)", "对根目录递归放开权限"),
    (r"\b>\s*/dev/sd[a-z]", "覆写磁盘设备"),
    (r"\b(iptables|nft)\s+.*-F", "清空防火墙规则"),
    (r"\bhistory\s+-c\b", "清空命令历史"),
    (r"\bmv\s+.*\s+/dev/null\b", "移动文件到 /dev/null"),
    (r"\bcurl\b[^|]*\|\s*(ba)?sh", "管道执行远程脚本"),
    (r"\bwget\b[^|]*\|\s*(ba)?sh", "管道执行远程脚本"),
    (r"\bgit\s+push\s+--force\b", "强制推送"),
)

#: bash_readonly 允许的命令前缀（只读/低危）。
#: 带参数形态的条目（如 "unzip -l"）按前缀匹配——列表/测试类子命令只读，
#: 解压类子命令（unzip 不带 -l、7z x、tar xf）刻意不在清单内。
READONLY_COMMAND_ALLOWLIST: tuple[str, ...] = (
    "cd",
    "ls",
    "cat",
    "head",
    "tail",
    "wc",
    "file",
    "stat",
    "find",
    "grep",
    "egrep",
    "fgrep",
    "strings",
    "xxd",
    "hexdump",
    "od",
    "base64",
    "md5sum",
    "sha1sum",
    "sha256sum",
    "sha512sum",
    "which",
    "whoami",
    "id",
    "uname",
    "pwd",
    "echo",
    "printf",
    "env",
    "date",
    "sort",
    "uniq",
    "cut",
    "tr",
    "sed",
    "awk",
    "jq",
    "diff",
    "cmp",
    "tree",
    "readlink",
    "realpath",
    "du",
    "df",
    "ps",
    "iconv",
    "python3 -c",
    "python3 -m json.tool",
    "binwalk",
    "exiftool",
    "zsteg",
    "steghide info",
    "tshark -r",
    "openssl x509",
    "openssl rsa -pubin",
    "checksec",
    "objdump",
    "readelf",
    "nm",
    "ldd",
    "command -v",
    "test",
    "ROPgadget",
    "ropper",
    "one_gadget",
    "pdfinfo",
    # ---- 归档**列表/完整性测试**（解压类子命令不在此清单）----
    "unzip -l",
    "unzip -v",
    "unzip -t",
    "unzip -Z",
    "zipinfo",
    "unrar l",
    "unrar t",
    "rar l",
    "rar t",
    "7z l",
    "7z t",
    "7zz l",
    "7zz t",
    "tar -t",
    "tar tf",
    "tar tvf",
    "tar tzf",
    "tar tJf",
    "tar --list",
    "tar -tf",
    "tar -tvf",
    "tar -tzf",
    "tar -tjf",
    "tar -tJf",
    "gzip -l",
    "xz -l",
    "zstd -l",
    "zcat",
    "bzcat",
    "xzcat",
)

#: 任何只读命令里出现这些 flag 前缀即判非只读——它们能执行外部程序（tar --to-command、
#: binwalk --dd 等），静态前缀白名单无法证明其安全。
READONLY_EXEC_FLAG_PREFIXES: tuple[str, ...] = (
    "--to-command",
    "--checkpoint-action",
    "--checkpoint=1",
    "--checkpoint",
    "--use-compress-program",
    "--dd",
)
