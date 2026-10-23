"""pydantic 配置模型与加载逻辑。

设计要点：
- ``config.yaml`` 通过 pydantic-settings / pydantic 校验，支持 ``${VAR}`` 与 ``${VAR:-默认值}`` 环境变量展开。
- 支持 Web API 热更新部分字段（见 :meth:`AppConfig.apply_update`）。
- api_key 对 Web API **明文往返**（本机单用户工具）；但"星号占位符"绝不允许被当成密钥保存。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from .constants import APP_NAME, COMPLIANCE_STATEMENT

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 环境变量展开
# ---------------------------------------------------------------------------
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_str(value: str) -> str:
    """展开字符串中的 ``${VAR}`` / ``${VAR:-default}``。"""

    def _repl(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        current = os.environ.get(name)
        if current is None or current == "":
            return default if default is not None else ""
        return current

    return _ENV_PATTERN.sub(_repl, value)


def expand_env(obj: Any) -> Any:
    """递归展开 dict/list/str 中的环境变量占位符。"""
    if isinstance(obj, str):
        return _expand_str(obj)
    if isinstance(obj, dict):
        return {k: expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# 占位符识别与密钥描述（只用于"拒绝写坏配置 / 诊断"，不用于对外脱敏）
# ---------------------------------------------------------------------------
#: 密钥过短（<=4 字符）无法展示尾部时使用的占位符（历史遗留，见到即视为非法密钥）
MASKED_PLACEHOLDER = "已配置"
#: 任何星号都说明这是占位符而不是密钥（真实密钥不含星号，含半角/全角几种写法）
_STAR_CHARS = "*＊✱⁎∗"


def looks_masked_secret(value: Any) -> bool:
    """判断一个值是否为"星号占位符"。

    这类值**绝不能**当密钥使用或落盘（模型请求必然 401），配置加载、热更新与模型调用都会拦截。
    """
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    if text == MASKED_PLACEHOLDER:
        return True
    return any(ch in text for ch in _STAR_CHARS)


def describe_secret(value: str) -> str:
    """安全描述密钥：长度 + 尾 4 位（绝不输出完整密钥）。"""
    key = value or ""
    if not key:
        return "未配置"
    if len(key) <= 8:
        return f"长度 {len(key)}"
    return f"长度 {len(key)}，尾 4 位 …{key[-4:]}"


def is_local_endpoint(base_url: str | None) -> bool:
    """判断 base_url 是否指向本机/本地推理服务（这类端点通常不需要 api_key）。"""
    if not base_url:
        return False
    try:
        host = (urlparse(base_url).hostname or "").lower()
    except ValueError:  # pragma: no cover - 非法 URL
        return False
    if not host:
        return False
    if host in {"localhost", "0.0.0.0", "::1", "host.docker.internal"}:
        return True
    if host.startswith("127.") or host.endswith(".local") or host.endswith(".internal"):
        return True
    return False


def secret_problem(value: str, base_url: str | None = None) -> str | None:
    """返回密钥的问题描述；``None`` 表示这一环看起来没问题。

    - 空值：本地端点（vLLM/Ollama）免鉴权，正常；**远端端点**则必须填 key——
      否则 openai SDK 会拿内部占位串 ``not-needed`` 去请求，服务商只回一个
      看不懂的 401（DeepSeek 会把它缩写成 ``****eded``，正是 ``not-needed`` 的尾 4 位）；
    - 星号占位符：一律非法；
    - 过短：几乎不可能是有效密钥。
    """
    key = (value or "").strip()
    if not key:
        if base_url and not is_local_endpoint(base_url):
            return "未配置（远端端点必须填写真实 api_key）"
        return None
    if looks_masked_secret(key):
        return f"是星号占位符（{describe_secret(key)}），不是真实密钥"
    if len(key) < 8:
        return f"长度仅 {len(key)}，很可能不是有效密钥"
    return None


def api_key_placeholder_channels(config: AppConfig | None = None, *, path: Path | None = None) -> list[str]:
    """磁盘配置里仍写成 ``${VAR}`` 的 api_key 通道名（这些密钥不再从环境变量读取）。"""
    target = path if path is not None else (config.config_path if config is not None else None)
    if not target or not Path(target).is_file():
        return []
    try:
        raw = yaml.safe_load(Path(target).read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - 读不到就当作没有占位符
        logger.debug("读取配置文件检查密钥占位符失败", exc_info=True)
        return []
    if not isinstance(raw, dict):
        return []
    channels = (raw.get("ai") or {}).get("channels") or {}
    if not isinstance(channels, dict):
        return []
    names: list[str] = []
    for name, chan in channels.items():
        if isinstance(chan, dict) and isinstance(chan.get("api_key"), str) and "${" in chan["api_key"]:
            names.append(str(name))
    return names


def _blank_api_key_placeholders(raw: dict[str, Any]) -> list[str]:
    """把 ``ai.channels.*.api_key`` 里的 ``${VAR}`` 占位符清空并提示（不再读环境变量）。"""
    channels = (raw.get("ai") or {}).get("channels") or {}
    if not isinstance(channels, dict):
        return []
    names: list[str] = []
    for name, chan in channels.items():
        if not isinstance(chan, dict):
            continue
        value = chan.get("api_key")
        if isinstance(value, str) and "${" in value:
            chan["api_key"] = ""
            names.append(str(name))
    return names


def _warn_if_masked_secrets(data: dict[str, Any]) -> None:
    """落盘前自检：配置里出现星号占位符说明上游有 bug（或配置已被写坏），必须显式告警。"""
    for name, chan in (data.get("ai", {}).get("channels", {}) or {}).items():
        if isinstance(chan, dict) and looks_masked_secret(chan.get("api_key")):
            logger.error(
                "配置中 ai.channels.%s.api_key 是星号占位符：它不会被当作密钥使用，"
                "请填写真实密钥（保存后明文写入本地配置文件）",
                name,
            )


# ---------------------------------------------------------------------------
# 配置模型
# ---------------------------------------------------------------------------
class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080


class ChannelConfig(BaseModel):
    """单条 LLM 通道配置。"""

    name: str = "Main"
    provider: str = "openai_compatible"
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-chat"
    max_total_tokens: int = 120_000
    max_completion_tokens: int = 16_384
    temperature: float | None = 0.3
    timeout_seconds: float = 600.0
    max_retries: int = 3
    #: reasoning 参数适配：透传给 SDK 的 ``reasoning_effort``（如 low/medium/high）
    reasoning_effort: str | None = None
    #: 额外请求头 / 额外 body 字段（适配 GLM thinking、Qwen enable_thinking 等非标准参数）
    extra_headers: dict[str, str] = Field(default_factory=dict)
    extra_body: dict[str, Any] = Field(default_factory=dict)
    supports_tools: bool = True
    supports_vision: bool = False


class AIConfig(BaseModel):
    default_channel: str = "main"
    #: failover 顺序；缺省时按 channels 的声明顺序
    channel_order: list[str] = Field(default_factory=list)
    #: 至少保留一个通道，否则运行期只能抛出「至少需要一个 LLM 通道」这类晦涩错误
    channels: dict[str, ChannelConfig] = Field(default_factory=lambda: {"main": ChannelConfig()})

    @field_validator("channels")
    @classmethod
    def _ensure_default_channel(cls, value: dict[str, ChannelConfig]) -> dict[str, ChannelConfig]:
        if not value:
            value = {"main": ChannelConfig()}
        return value

    def has_credentials(self) -> bool:
        """是否存在任意一个填了 api_key 的通道（本地 vLLM/Ollama 可以不需要 key）。"""
        return any(ch.api_key for ch in self.channels.values())

    def ordered_channels(self) -> list[ChannelConfig]:
        """返回按 failover 顺序排列的通道列表（default_channel 优先）。"""
        names: list[str] = []
        if self.default_channel and self.default_channel in self.channels:
            names.append(self.default_channel)
        for name in self.channel_order:
            if name in self.channels and name not in names:
                names.append(name)
        for name in self.channels:
            if name not in names:
                names.append(name)
        return [self.channels[n] for n in names]


class AgentConfig(BaseModel):
    max_iterations: int = 500
    sub_agent_max_iterations: int = 150
    tool_timeout_seconds: int = 900
    tool_wait_timeout_seconds: int = 120
    no_output_timeout_seconds: int = 300
    max_concurrent_tools: int = 8
    max_tool_result_bytes: int = 12_000
    workspace_root: str = "workspace"
    max_history_messages: int = 120
    tool_search_enabled: bool = False
    finalizer_enabled: bool = True
    max_finalizer_retries: int = 2
    system_prompt_path: str = ""
    #: 用 config 覆盖/新增 Markdown 子代理定义（同 id 覆盖 agents/*.md）
    custom_agents: dict[str, Any] = Field(default_factory=dict)


class AuditAgentConfig(BaseModel):
    """U6b：Agent 快速审查器（审计专用小模型，独立于主 AI 通道）。"""

    #: 审计专用 AI 通道名；留空复用 ai.default_channel
    model_channel: str = ""
    #: 单次审计调用上限（秒）
    timeout_seconds: float = 30.0
    max_tokens: int = 512
    #: 解析失败/超时的兜底策略：reject（保守拒绝，默认）| fallback_human（转人工队列）
    on_failure: Literal["reject", "fallback_human"] = "reject"
    #: true 时启用 review_edit（审计可改参后放行；只收窄不扩大）
    allow_edit: bool = False
    #: 留空使用内置 CTF 审计提示词（ctfagent.agent.audit_agent.AUDIT_PROMPT）
    prompt: str = ""


class ReadonlyGateConfig(BaseModel):
    """``bash_readonly`` / 白名单免审的只读命令校验开关与自定义白名单。

    - ``enabled=false``：跳过只读校验——``bash_readonly`` 等价于 ``bash``（仍受
      硬超时/无输出终止/workspace 治理约束），HITL 白名单路径也不再因"参数疑似写操作"升级审查；
    - ``extra_allowlist``：**追加**到内置只读白名单（前缀匹配，如
      ``"sqlite3 -readonly"``、``"rabin2 -I"``），不覆盖内置清单。
    """

    enabled: bool = True
    extra_allowlist: list[str] = Field(default_factory=list)


class HITLConfig(BaseModel):
    enabled: bool = True
    #: **deprecated（v1）**：off | all | whitelist —— 保留读取但不再生效；
    #: 启动加载时按 off→none / all→human / whitelist→human 幂等映射到 default_reviewer。
    mode: Literal["whitelist", "off", "all"] = "whitelist"
    #: U6a 审查模式：human（人工逐条审）| audit_agent（Agent 快速审）| none（完全访问）
    default_reviewer: Literal["human", "audit_agent", "none"] = "none"
    timeout_seconds: int = 120
    cli_auto_approve: bool = False
    tool_whitelist: list[str] = Field(default_factory=list)
    audit_agent: AuditAgentConfig = Field(default_factory=AuditAgentConfig)
    #: bash_readonly / 白名单免审共用的只读命令校验（全局开关 + 自定义追加白名单）
    readonly_gate: ReadonlyGateConfig = Field(default_factory=ReadonlyGateConfig)

    @field_validator("mode", mode="before")
    @classmethod
    def _coerce_legacy_mode_bool(cls, value: Any) -> Any:
        """YAML 1.1 把未加引号的 ``off``/``on`` 解析成布尔值；旧配置按语义纠正。"""
        if value is False:
            return "off"
        if value is True:
            return "all"
        return value


class MultiAgentConfig(BaseModel):
    enabled: bool = True
    default_orchestration: Literal["deep", "plan_execute"] = "deep"
    max_concurrent_sub_agents: int = 4
    #: roles=v1 deep 编排（交互对话）；blackboard=v2 自主解题（无编排者 LLM，调度外置）
    mode: Literal["roles", "blackboard"] = "roles"


class BlackboardConfig(BaseModel):
    enabled: bool = True
    inject_index_limit: int = 60
    search_limit: int = 20


# ---------------------------------------------------------------------------
# v2 增量配置（全部带向后兼容默认值；关闭后 v1 行为不变）
# ---------------------------------------------------------------------------
class RendererV2Config(BaseModel):
    """渲染式上下文（挑战/Solver 上下文生效；普通会话不受影响）。"""

    enabled: bool = True
    budget_tokens: int = 3_000
    idea_top: int = 6
    memory_top: int = 6
    handoff_max_chars: int = 900


class StateV2Config(BaseModel):
    """状态账本 v2（Idea 状态机 + Memory 类型学 + 渲染式上下文）。"""

    enabled: bool = True
    renderer: RendererV2Config = Field(default_factory=RendererV2Config)


class SolverConfig(BaseModel):
    """Solver 生命周期与双阶段 conclude。"""

    soft_timeout_minutes: int = 20
    hard_timeout_minutes: int = 60
    conclude_budget_tokens: int = 2_000
    two_phase_conclude: bool = True
    max_conclude_iterations: int = 2
    #: 无新 Memory 的宽限期：软超时后先提醒，再宽限一轮才停止
    grace_minutes: int = 5

    # ---- M1 租约（0 = 关闭，退回原有 soft_timeout 启发式，可秒级回滚）----
    #: 租约续约周期（秒）；应 ≤ lease_ttl_seconds / 3
    lease_renew_seconds: int = 30
    #: 租约 TTL（秒）；过期即视为 Solver 死亡（长工具调用需在工具前后续约）
    lease_ttl_seconds: int = 180

    # ---- M3 运行中黑板增量刷新（0 = 关闭）----
    #: 每 N 次工具调用检查一次他人产出
    blackboard_refresh_steps: int = 8
    #: 最小刷新间隔（秒）
    blackboard_refresh_seconds: int = 90


class DispatcherConfig(BaseModel):
    """外置控制回路（规则驱动，非 LLM）。"""

    interval_seconds: float = 10.0
    stability_window_seconds: float = 60.0
    max_concurrent_solvers: int = 4
    #: M4：reason 触发改用图指纹（memories 数 / hints 数 / 是否有 open ideas）；
    #: False 时退回旧的 last_task_type 启发式（可秒级回滚）
    reason_by_fingerprint: bool = True


class TerminationConfig(BaseModel):
    default_time_budget_minutes: int = 120
    #: 无 pending/testing Idea 且无运行中 Solver、且已完成过 explore 时判定 EXHAUSTED
    min_advances_before_exhausted: int = 1  # 至少完成过一次正向推进（explore/reason）才可能判定"图静止"


class ObserverJudgeConfig(BaseModel):
    enabled: bool = False
    model_channel: str = ""


class ObserverConfig(BaseModel):
    """旁路纠偏（只提醒不决策）。"""

    enabled: bool = True
    judge: ObserverJudgeConfig = Field(default_factory=ObserverJudgeConfig)
    cooldown_per_solver_minutes: int = 30
    max_reminders_per_solver: int = 2
    repeat_tool_threshold: int = 3
    no_progress_minutes: int = 8
    failure_similarity_threshold: float = 0.6


class ChallengeConfig(BaseModel):
    """题目（挑战）模块：多题并发的控制面。

    [DEPRECATED] 多题批量并发已移除（并发改由多会话承担）；字段保留仅为旧配置兼容。
    其余题目排队，待有题目结束后自动补位。
    """

    max_concurrent_runs: int = Field(
        default=3,
        ge=1,
        le=32,
        description="同时自主解题的题目数量上限（默认 3）",
    )


class DockerWorkerConfig(BaseModel):
    """可选 M5：容器化 Worker（默认关闭）。"""

    enabled: bool = False
    image: str = "ctfagent-worker:latest"


class KnowledgeConfig(BaseModel):
    enabled: bool = False
    corpus_dir: str = "skills"
    index_path: str = "data/knowledge.jsonl"
    top_k: int = 5
    chunk_size: int = 800
    chunk_overlap: int = 120


class VisionConfig(BaseModel):
    enabled: bool = False
    channel: str = ""
    max_image_bytes: int = 8_000_000


class MCPServerConfig(BaseModel):
    name: str
    transport: Literal["stdio", "sse", "http"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    tool_prefix: str = ""

    def resolved_prefix(self) -> str:
        return self.tool_prefix or f"mcp_{self.name}_"


class MCPConfig(BaseModel):
    enabled: bool = False
    servers: list[MCPServerConfig] = Field(default_factory=list)


class AttachmentConfig(BaseModel):
    """对话附件上传（U2a）。"""

    enabled: bool = True
    max_file_mb: int = Field(default=50, ge=1, le=2048)
    max_count: int = Field(default=10, ge=1, le=100)
    #: 允许的扩展名白名单（不带点，大小写不敏感）；按 CTF 场景预置，可在配置里覆盖
    allowed_extensions: list[str] = Field(
        default_factory=lambda: [
            # 流量与取证
            "pcap",
            "pcapng",
            "cap",
            "evtx",
            "raw",
            "dd",
            "mem",
            "vmem",
            "dmp",
            "core",
            # 压缩包
            "zip",
            "rar",
            "7z",
            "tar",
            "gz",
            "tgz",
            "bz2",
            "xz",
            "zst",
            # 可执行 / 固件 / 移动
            "exe",
            "dll",
            "elf",
            "bin",
            "so",
            "dex",
            "apk",
            "jar",
            "class",
            "wasm",
            "img",
            "iso",
            "vhd",
            "fw",
            "hex",
            "mot",
            "srec",
            # 图片 / 音视频
            "jpg",
            "jpeg",
            "png",
            "bmp",
            "gif",
            "webp",
            "tif",
            "tiff",
            "ico",
            "svg",
            "mp3",
            "wav",
            "mp4",
            "webm",
            "avi",
            "mkv",
            # 文本 / 源码 / 数据
            "txt",
            "md",
            "json",
            "yaml",
            "yml",
            "csv",
            "tsv",
            "log",
            "xml",
            "html",
            "htm",
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
            "pem",
            "crt",
            "key",
            "pub",
            "gpg",
            # 文档 / 数据库 / 其他
            "pdf",
            "doc",
            "docx",
            "xls",
            "xlsx",
            "ppt",
            "pptx",
            "db",
            "sqlite",
            "sqlite3",
            "sav",
            "dat",
            "bak",
            "enc",
            "qco",
            "vmdk",
            "ova",
            "ova",
        ]
    )


class AppConfig(BaseModel):
    """应用总配置。"""

    server: ServerConfig = Field(default_factory=ServerConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    hitl: HITLConfig = Field(default_factory=HITLConfig)
    multi_agent: MultiAgentConfig = Field(default_factory=MultiAgentConfig)
    blackboard: BlackboardConfig = Field(default_factory=BlackboardConfig)
    # -- v2 增量段（默认值保证 v1 行为不变） --
    state_v2: StateV2Config = Field(default_factory=StateV2Config)
    solver: SolverConfig = Field(default_factory=SolverConfig)
    dispatcher: DispatcherConfig = Field(default_factory=DispatcherConfig)
    termination: TerminationConfig = Field(default_factory=TerminationConfig)
    observer: ObserverConfig = Field(default_factory=ObserverConfig)
    #: 题目（挑战）模块：多题并发上限
    challenge: ChallengeConfig = Field(default_factory=ChallengeConfig)
    docker_worker: DockerWorkerConfig = Field(default_factory=DockerWorkerConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    attachment: AttachmentConfig = Field(default_factory=AttachmentConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)

    #: 运行期元信息（不来自 YAML，由加载器注入）
    project_root: Path = Field(default_factory=lambda: Path.cwd())
    config_path: Path | None = None
    db_path: str = "data/ctfagent.db"

    model_config = {"arbitrary_types_allowed": True}

    # -- 路径辅助 ---------------------------------------------------------
    @property
    def workspace_root(self) -> Path:
        """会话工作区绝对路径（按题目隔离的子目录在 :meth:`challenge_workspace`）。"""
        root = Path(self.agent.workspace_root)
        if not root.is_absolute():
            root = self.project_root / root
        return root

    @property
    def database_path(self) -> Path:
        path = Path(self.db_path)
        if not path.is_absolute():
            path = self.project_root / path
        return path

    def challenge_workspace(self, challenge_id: str | None) -> Path:
        """返回某题目的隔离工作区（未指定题目时落到 ``_default``）。

        .. deprecated:: U17
           仅供历史数据兼容；运行期请用 :meth:`run_workspace`——未绑定题目的会话
           必须落到会话私有目录，避免读到其它题目的附件与旧 writeup（串题）。
        """
        cid = (challenge_id or "_default").strip() or "_default"
        # 防目录穿越：只保留安全字符
        cid = re.sub(r"[^A-Za-z0-9_.\-]", "_", cid)
        path = self.workspace_root / "challenge" / cid
        path.mkdir(parents=True, exist_ok=True)
        return path

    def run_workspace(self, challenge_id: str | None, conversation_id: str) -> Path:
        """运行期工作区（U17 会话隔离）。

        - 绑定题目：``workspace/challenge/<challenge_id>``（题目隔离区，跨会话共享同一题）；
        - 未绑定题目（普通对话上传附件解题）：``workspace/conversations/<cid>/work`` ——
          **会话私有**。

        旧逻辑把所有未绑定会话统一指向 ``workspace/challenge/_default``，该目录会积累
        其它题目的附件、解题脚本与 writeup（内含已解出的 flag）。新会话按提示词探索
        「自己的工作区」时读到的是旧题数据，于是直接给出旧题答案（串题）。
        """
        if (challenge_id or "").strip():
            return self.challenge_workspace(challenge_id)
        cid = (conversation_id or "_anon").strip() or "_anon"
        cid = re.sub(r"[^A-Za-z0-9_.\-]", "_", cid)  # 防目录穿越
        path = self.workspace_root / "conversations" / cid / "work"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def outputs_dir(self) -> Path:
        path = self.workspace_root / "_outputs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def conversation_workspace(self, conversation_id: str) -> Path:
        """会话附件目录（U2a）：``workspace/conversations/{conversation_id}/attachments``。"""
        cid = (conversation_id or "_anon").strip() or "_anon"
        cid = re.sub(r"[^A-Za-z0-9_.\-]", "_", cid)  # 防目录穿越
        path = self.workspace_root / "conversations" / cid / "attachments"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def conversation_dir(self, conversation_id: str) -> Path:
        """会话目录路径（**纯路径，不创建目录**）：删会话时整目录清理用。"""
        cid = (conversation_id or "_anon").strip() or "_anon"
        cid = re.sub(r"[^A-Za-z0-9_.\-]", "_", cid)
        return self.workspace_root / "conversations" / cid

    def uploads_dir(self) -> Path:
        path = self.workspace_root / "uploads"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def active_channel(self) -> ChannelConfig:
        chans = self.ai.channels
        return chans.get(self.ai.default_channel) or next(iter(chans.values()))

    # -- 对外输出 ---------------------------------------------------------
    def public_dict(self) -> dict[str, Any]:
        """输出给 Web 前端的配置。

        **api_key 明文返回**（不做 ``****`` 脱敏）：本机单用户工具，前端配置编辑器需要看到真实值，
        否则"读到的脱敏串被原样保存回去"曾导致密钥被写成 ``********eded``、请求全部 401。
        注意：明文意味着任何能访问该接口的人都能看到密钥，请勿把服务暴露到不受信任的网络。
        """
        return self.model_dump(mode="json", exclude={"project_root", "config_path"})

    # -- 热更新 -----------------------------------------------------------
    #: 允许通过 Web API 热更新的顶层配置段（敏感/危险字段除外）
    HOT_UPDATE_SECTIONS: tuple[str, ...] = (
        "server",
        "ai",
        "agent",
        "hitl",
        "multi_agent",
        "blackboard",
        "knowledge",
        "vision",
        "attachment",
        # v2 增量段（可在 Web 界面在线调参）
        "state_v2",
        "solver",
        "dispatcher",
        "termination",
        "observer",
        #: 题目（挑战）模块：并发上限可在线调参
        "challenge",
    )
    #: 永不允许热更新的字段路径
    HOT_UPDATE_DENY: tuple[str, ...] = ("agent.workspace_root", "db_path", "project_root")

    def apply_update(self, patch: dict[str, Any]) -> AppConfig:
        """把部分字段合并进当前配置并重新校验，返回新配置对象。

        仅允许 ``HOT_UPDATE_SECTIONS`` 中的顶层段。

        **密钥**：配置接口明文往返，``api_key`` 按填写值直接生效；但若传入星号占位符
        （``********eded`` / ``已配置``）则抛 :class:`ConfigError`（HTTP 422），
        避免"看起来保存成功、请求却 401"。空字符串是**显式清空**。
        """
        current = self.model_dump(mode="python", exclude={"project_root", "config_path"})
        for section, values in (patch or {}).items():
            if section not in self.HOT_UPDATE_SECTIONS:
                continue
            if section in current and isinstance(current[section], dict) and isinstance(values, dict):
                current[section] = _deep_merge(current[section], values)
            else:
                current[section] = values
        # 配置接口是明文往返（见 public_dict），因此这里只做一件事：
        # 拦住"星号占位符"被当成密钥写进来（历史 bug：****eded 会一直 401）。
        in_channels = current.get("ai", {}).get("channels", {}) or {}
        for name, chan in list(in_channels.items()):
            if not isinstance(chan, dict):
                continue
            key = chan.get("api_key", "")
            if isinstance(key, str) and looks_masked_secret(key):
                raise ConfigError(
                    f"ai.channels.{name}.api_key 是星号占位符（{key}），不是真实密钥。"
                    "请填写完整密钥（保存后会明文写入本地配置文件）；"
                    "若要清空请设为空字符串。"
                )
        current["project_root"] = self.project_root
        current["config_path"] = self.config_path
        updated = AppConfig.model_validate(current)
        for deny in self.HOT_UPDATE_DENY:
            obj: Any = updated
            for part in deny.split("."):
                obj = getattr(obj, part, None)
                if obj is None:
                    break
            if obj is None:
                continue
        return updated

    def to_yaml(self) -> str:
        """导出可落盘的 YAML（含真实 api_key，仅用于写回配置文件）。

        密钥一律按内存中的真实值写出：**不再把 api_key 写成 ``${VAR}`` 占位符**，
        也不再从环境变量读取它——用户在 Web「配置」页保存什么，文件里就是什么。
        """
        data = self.model_dump(mode="json", exclude={"project_root", "config_path"})
        _warn_if_masked_secrets(data)
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


class ConfigError(RuntimeError):
    """配置加载失败。"""


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# 加载与全局访问
# ---------------------------------------------------------------------------
_CONFIG: AppConfig | None = None


def find_config_file() -> Path | None:
    """按优先级定位配置文件。"""
    env_path = os.environ.get("CTF_AGENT_CONFIG")
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path))
    cwd = Path.cwd()
    root = Path(__file__).resolve().parent.parent
    candidates += [
        cwd / "config.yaml",
        cwd / "config.example.yaml",
        root / "config.yaml",
        root / "config.example.yaml",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def config_section_map(config: AppConfig) -> dict[str, Any]:
    """配置的顶层 section 映射（保留 ``${VAR}`` 占位符写法，剔除运行期字段）。"""
    data = yaml.safe_load(config.to_yaml())
    if not isinstance(data, dict):
        return {}
    for key in ("project_root", "config_path", "HOT_UPDATE_SECTIONS", "HOT_UPDATE_DENY"):
        data.pop(key, None)
    return data


def changed_config_sections(before: AppConfig, after: AppConfig) -> list[str]:
    """哪些顶层 section 真的变了（界面保存只写这些，避免把用户文件整份重写）。"""
    old = config_section_map(before)
    new = config_section_map(after)
    changed: list[str] = []
    for key in dict.fromkeys(list(old) + list(new)):
        if old.get(key) != new.get(key):
            changed.append(key)
    return changed


_TOP_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(?:\s|$)")


def _iter_top_sections(text: str) -> list[tuple[str, int, int]]:
    """把 YAML 文本切成顶层 section 的 (key, 起始行, 结束行) 区间（行号左闭右开）。"""
    lines = text.splitlines(keepends=True)
    spans: list[tuple[str, int, int]] = []
    for index, line in enumerate(lines):
        match = _TOP_KEY_RE.match(line)
        if match:
            spans.append((match.group(1), index, index))
    for position, (key, start, _) in enumerate(spans):
        end = spans[position + 1][1] if position + 1 < len(spans) else len(lines)
        spans[position] = (key, start, end)
    return spans


def merge_config_text(existing: str | None, sections: dict[str, Any]) -> str:
    """把若干顶层 section 合并进既有配置文件文本，**保留未改动部分的注释与顺序**。

    - 已存在的 section：整段替换为新内容（该段内的行内注释会由 ``.bak`` 兜底）；
    - 不存在的 section：追加到文件末尾；
    - 没有既有文件：直接输出这些 section。
    """
    dumped = yaml.safe_dump(sections, allow_unicode=True, sort_keys=False)
    if not existing or not existing.strip():
        return dumped
    lines = existing.splitlines(keepends=True)
    spans = _iter_top_sections(existing)
    by_key = {key: (start, end) for key, start, end in spans}
    replaced: set[str] = set()
    # 从后往前替换，避免行号漂移
    for key, (start, end) in sorted(by_key.items(), key=lambda item: item[1][0], reverse=True):
        if key not in sections:
            continue
        block = yaml.safe_dump({key: sections[key]}, allow_unicode=True, sort_keys=False)
        lines[start:end] = [block]
        replaced.add(key)
    text = "".join(lines)
    missing = [key for key in sections if key not in replaced]
    if missing:
        if text and not text.endswith("\n"):
            text += "\n"
        text += yaml.safe_dump({key: sections[key] for key in missing}, allow_unicode=True, sort_keys=False)
    return text


def writable_config_path(config: AppConfig) -> Path:
    """写回配置时的目标文件。

    - 当前 ``config_path`` 是普通配置文件 → 写回原处（保留用户的目录约定）；
    - 当前路径缺失，或指向 ``config.example.yaml`` 这类模板 → 不回写模板，
      改为 ``<project_root>/config.yaml``（首次保存即创建，下次启动自动被
      :func:`find_config_file` 读到）。
    """
    current = Path(config.config_path) if config.config_path else None
    if current is not None:
        name = current.name.lower()
        if "example" not in name and "template" not in name and name.endswith((".yaml", ".yml")):
            return current
    return Path(config.project_root) / "config.yaml"


def _looks_like_project(root: Path) -> bool:
    """判断目录是否像本项目的根（用于推导 project_root）。"""
    if not root.is_dir():
        return False
    markers = ("pyproject.toml", "agents", "tools", "web")
    return sum(1 for name in markers if (root / name).exists()) >= 2


def _guess_project_root(config_path: Path | None) -> Path:
    """推导项目根目录。

    优先级：配置文件所在目录（若确实像项目根）→ 包所在目录的父目录 → 当前工作目录。
    这样把配置放到 /etc 或 /tmp 时，仍能找到 ``tools/``、``agents/``、``web/`` 等资源。
    """
    if config_path is not None:
        parent = Path(config_path).resolve().parent
        if _looks_like_project(parent):
            return parent
    package_parent = Path(__file__).resolve().parent.parent
    if _looks_like_project(package_parent):
        return package_parent
    return Path.cwd()


#: v1 ``hitl.mode`` → U6 ``default_reviewer`` 的幂等迁移映射（旧字段保留读取但不再生效）
_HITL_MODE_MIGRATION: dict[str, str] = {"off": "none", "all": "human", "whitelist": "human"}


def migrate_hitl_v1(raw: dict[str, Any]) -> dict[str, Any]:
    """把 v1 的 ``hitl.mode`` 幂等映射到 ``hitl.default_reviewer``（U6a 向后兼容）。

    - 用户已显式写了 ``default_reviewer`` → 原样返回（不覆盖）；
    - ``mode: off`` → ``none``；``mode: all`` / ``mode: whitelist`` → ``human``；
    - 只改内存 raw dict，不回写配置文件；每次加载打一次 INFO 日志。
    """
    hitl = raw.get("hitl")
    if not isinstance(hitl, dict) or "default_reviewer" in hitl:
        return raw
    mode = hitl.get("mode")
    if mode is False:  # YAML 1.1 会把未加引号的 off 解析成布尔 False
        mode = "off"
    elif mode is True:
        mode = "all"
    mode = str(mode or "").lower()
    if mode in _HITL_MODE_MIGRATION:
        hitl["default_reviewer"] = _HITL_MODE_MIGRATION[mode]
        logger.info(
            "HITL 配置迁移（U6）：hitl.mode=%s → hitl.default_reviewer=%s"
            "（hitl.mode 已弃用，仅保留读取；hitl.enabled 仍是总开关）",
            mode,
            _HITL_MODE_MIGRATION[mode],
        )
    return raw


def load_config(path: str | Path | None = None, *, overrides: dict[str, Any] | None = None) -> AppConfig:
    """加载并校验配置；``path`` 为空时自动探测。"""
    explicit = Path(path) if path else None
    config_path = explicit or find_config_file()
    if explicit is not None and not explicit.is_file():
        raise ConfigError(
            f"配置文件不存在或不是文件：{explicit}\n"
            "提示：可执行 `cp config.example.yaml config.yaml` 生成一份，或去掉 --config 让其自动探测。"
        )
    raw: dict[str, Any] = {}
    if config_path is not None and Path(config_path).is_file():
        with open(config_path, encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"配置文件 {config_path} 顶层必须是映射（dict）")
        raw = loaded
    # api_key 不走环境变量：先记下占位符，展开其它字段后把密钥位清空并提示。
    api_key_placeholder_names = _blank_api_key_placeholders(raw)
    raw = expand_env(raw)
    for name in api_key_placeholder_names:
        logger.error(
            "ai.channels.%s.api_key 在配置文件里仍是 ${VAR} 占位符；本版本不再从环境变量读取密钥，"
            "已忽略该值。请在 Web 界面「配置」页填写真实密钥并点「保存并写入本地」。",
            name,
        )
    if overrides:
        raw = _deep_merge(raw, overrides)
    raw = migrate_hitl_v1(raw)
    raw.setdefault("project_root", str(_guess_project_root(config_path)))
    raw["config_path"] = str(config_path) if config_path else None
    try:
        config = AppConfig.model_validate(raw)
    except ValidationError as exc:  # pragma: no cover - 报错路径
        raise ConfigError(f"配置校验失败：{exc}") from exc
    config.project_root = Path(config.project_root).resolve()
    if config.config_path:
        config.config_path = Path(config.config_path).resolve()
    _repair_masked_secrets(config)
    # 确保工作区与数据目录存在
    config.workspace_root.mkdir(parents=True, exist_ok=True)
    config.database_path.parent.mkdir(parents=True, exist_ok=True)
    return config


def _repair_masked_secrets(config: AppConfig) -> None:
    """修复：配置文件里被误写入星号占位符的 api_key → 清空并显著告警。

    历史 bug：Web 界面「保存配置」把 ``********89ed`` 当成新密钥写回 YAML，之后所有
    模型请求都会 401。这里**不再从环境变量兜底**（密钥只认配置文件里用户填写的内容）：

    1. 该字段清空并打 ERROR 日志（宁可报「未配置密钥」，也不要拿星号去 401）；
    2. 用户到 Web「配置」页重新填写并保存即可（会明文写回本地配置文件）。
    """
    for name, channel in config.ai.channels.items():
        if not looks_masked_secret(channel.api_key):
            continue
        masked = channel.api_key
        channel.api_key = ""
        logger.error(
            "配置文件中 ai.channels.%s.api_key 是星号占位符（%s，疑似保存配置时误写入），已忽略该值。"
            "请在 Web 界面「配置」页重新填写真实密钥并保存（密钥不再从环境变量读取）。",
            name,
            masked,
        )


def get_config() -> AppConfig:
    """获取全局配置（未加载时惰性加载）。"""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG


def set_config(config: AppConfig) -> None:
    """替换全局配置（热更新用）。"""
    global _CONFIG
    _CONFIG = config


__all__ = [
    "APP_NAME",
    "COMPLIANCE_STATEMENT",
    "AIConfig",
    "AgentConfig",
    "AppConfig",
    "AuditAgentConfig",
    "BlackboardConfig",
    "ChannelConfig",
    "ConfigError",
    "HITLConfig",
    "KnowledgeConfig",
    "MCPConfig",
    "MCPServerConfig",
    "MultiAgentConfig",
    "ServerConfig",
    "VisionConfig",
    "expand_env",
    "find_config_file",
    "get_config",
    "load_config",
    "migrate_hitl_v1",
    "set_config",
]
