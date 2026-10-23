"""竞赛文档解析器（M6）——把官方 API 文档变成可信的能力规格。

流水线：
1. ``split_sections``：纯代码结构切分（标题树 / 表格 / fenced code）
2. ``extract_spec_heuristic``：确定性抽取（正则 + 表格），无 LLM、可单测、可离线
3. ``extract_spec_llm``：LLM 抽取（可选，用于启发式覆盖不到的自由文本），严格 JSON
4. ``validate_spec``：必需能力校验（list_exercises + submit_flag）
5. ``persist_spec``：落竞赛黑板（protocol/rule/constraint/credential）+ 注册 capabilities

设计原则：**启发式优先、LLM 兜底**。比赛文档结构高度规整（接口总览表 + 字段说明），
正则比 LLM 更稳、更快、零成本；LLM 只在"表格缺失、纯散文描述"时启用。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ParsedSpec",
    "CAPABILITY_KEYWORDS",
    "REQUIRED_CAPABILITIES",
    "extract_spec",
    "extract_spec_heuristic",
    "split_sections",
    "validate_spec",
]

#: 能力 → 文档关键词（按优先级匹配，先匹配到的胜出）
CAPABILITY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "get_rules": ("竞赛注意事项", "竞赛规则", "match-info", "获取规则", "rules"),
    "get_scoreboard": ("得分与排名", "排名", "overview", "scoreboard", "stageRank"),
    "submit_flag": ("提交答案", "提交 flag", "提交flag", "answer-panel/answer", "submit_flag", "提交 Flag"),
    "list_exercises": ("题目列表", "exercise-list", "list_challenges", "查询赛题"),
    "get_exercise": ("题目详情", "ctf/exercise", "查询题目详情"),
    "build_env": ("启动题目环境", "启动环境", "build-exercise-env", "start_challenge", "启动赛题"),
    "recover_env": ("回收题目环境", "回收环境", "recover-exercise-env", "stop_challenge", "停止赛题"),
    "get_notices": ("公告列表", "公告", "now-list", "notice/detail"),
    "view_hint": ("查看提示", "提示信息", "view_hint", "hint"),
}

#: 必需能力（缺任一 → 拒绝落库）
REQUIRED_CAPABILITIES = ("list_exercises", "submit_flag")

#: MCP 工具名 → 能力
MCP_TOOL_MAP = {
    "list_challenges": "list_exercises",
    "start_challenge": "build_env",
    "stop_challenge": "recover_env",
    "submit_flag": "submit_flag",
    "view_hint": "view_hint",
}


@dataclass
class ParsedSpec:
    """解析结果（落库前的人工可审对象）。"""

    platform_kind: str = "generic_rest"
    base_url: str = ""
    auth_kind: str = "header"
    auth_header: str = ""
    capabilities: list[dict[str, Any]] = field(default_factory=list)
    constraints: dict[str, Any] = field(default_factory=dict)
    rules_text: str = ""
    notes: list[str] = field(default_factory=list)
    confidence: str = "medium"

    def to_dict(self) -> dict[str, Any]:
        return {
            "platformKind": self.platform_kind,
            "baseUrl": self.base_url,
            "authKind": self.auth_kind,
            "authHeader": self.auth_header,
            "capabilities": self.capabilities,
            "constraints": self.constraints,
            "rulesText": self.rules_text,
            "notes": self.notes,
            "confidence": self.confidence,
        }

    def capability_names(self) -> list[str]:
        return [str(c.get("name")) for c in self.capabilities]


# ---------------------------------------------------------------------------
# 1) 结构切分
# ---------------------------------------------------------------------------
@dataclass
class Section:
    level: int
    title: str
    body: str


def split_sections(text: str) -> list[Section]:
    """按 Markdown 标题切段（保留标题层级与正文）。"""
    sections: list[Section] = []
    current = Section(0, "", [])
    for line in text.splitlines():
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if match:
            if current.title or current.body:
                sections.append(Section(current.level, current.title, "\n".join(current.body)))
            current = Section(len(match.group(1)), match.group(2).strip(), [])
        else:
            current.body.append(line)
    if current.title or current.body:
        sections.append(Section(current.level, current.title, "\n".join(current.body)))
    return sections


# ---------------------------------------------------------------------------
# 2) 确定性抽取
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 凭证脱敏：文档原文里的密钥（token/access_key/api_key）落黑板前一律打码
# ---------------------------------------------------------------------------
_SECRET_PATTERNS = [
    (re.compile(r"(nss_agent_)[A-Za-z0-9]{6,}", re.I), r"\1****"),
    (re.compile(r"(ak_live_)[A-Za-z0-9]{6,}", re.I), r"\1****"),
    (re.compile(r"(sk-[A-Za-z0-9_-]{8,})"), "sk-****"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9_.\-]{8,}"), r"\1****"),
    (re.compile(r"(token\s*(?:是|:|=)\s*)[A-Za-z0-9_.\-]{8,}", re.I), r"\1****"),
]


def _sanitize_secrets(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _extract_base_url(text: str) -> str:
    # 优先取 "Base URL" 段后的第一个代码块/裸 URL
    block = re.search(r"###?\s*Base URL[\s\S]{0,400}?```[a-z]*\n\s*(\S+)", text, re.I)
    if block:
        return block.group(1).strip().rstrip("/")
    raw = re.search(r"https?://[^\s`\"'）)]+", text)
    return raw.group(0).rstrip("/") if raw else ""


def _extract_auth(text: str) -> tuple[str, str]:
    """返回 (auth_kind, header_name)。

    文档里的 Header 常被反引号或全角标点包裹（如 `` `Authorization: Bearer xxx` ``），
    因此用宽松匹配，避免因标点差异漏判鉴权方式。
    """
    if re.search(r"Authorization[^\n]{0,24}Bearer", text, re.I) or re.search(
        r"Bearer\s+[<A-Za-z0-9_]", text
    ):
        return "bearer", "Authorization"
    table = re.search(r"\|\s*`?([A-Za-z0-9_-]*(?:AccessKey|Token|Key)[A-Za-z0-9_-]*)`?\s*\|", text)
    if table:
        return "header", table.group(1).strip("`")
    if re.search(r"X-Agent-AccessKey", text):
        return "header", "X-Agent-AccessKey"
    return "header", ""

def _table_rows(text: str) -> list[list[str]]:
    """抽取所有 Markdown 表格行（去掉分隔行）。"""
    rows: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if re.match(r"^\|[\s:|-]+\|$", stripped):
            continue
        cells = [c.strip().strip("`") for c in stripped.strip("|").split("|")]
        rows.append(cells)
    return rows


def _endpoints_from_overview(text: str) -> list[dict[str, str]]:
    """从「接口总览」类表格提取 (method, path, desc)。"""
    found: list[dict[str, str]] = []
    for cells in _table_rows(text):
        if len(cells) < 2:
            continue
        method = cells[0].upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            continue
        path = cells[1]
        if not path.startswith("/"):
            continue
        found.append({"method": method, "path": path, "desc": cells[2] if len(cells) > 2 else ""})
    return found


def _match_capability(desc_or_title: str, path: str) -> str | None:
    haystack = f"{desc_or_title} {path}".lower()
    for name, keywords in CAPABILITY_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in haystack:
                return name
    return None


def _response_field_hints(text: str, capability: str) -> dict[str, Any]:
    """从文档中提取该能力的响应字段映射（保守：只取明确出现的字段名）。"""
    if capability == "submit_flag":
        if re.search(r"isCorrect", text):
            return {"verdict": {"field": "isCorrect", "success": True}}
        if re.search(r"\bcorrect\b", text):
            return {"verdict": {"field": "correct", "success": True}}
    if capability == "list_exercises":
        if re.search(r"corpus", text):
            return {"response": {"path": "data", "item": {"key": "corpus[].id", "name": "corpus[].name",
                                                          "group": "name", "solved": "corpus[].hasSolved",
                                                          "locked": "!corpus[].isOpen"}}}
        if re.search(r"current_level", text):
            return {"response": {"path": "data", "item": {"key": "code", "name": "title",
                                                          "solved": "flag_got_count>=flag_count",
                                                          "group": "current_level"}}}
    if capability == "build_env":
        if re.search(r"isNeedCheck", text):
            return {"poll": {"detail_field": "isNeedCheck", "ready_when": False,
                             "endpoints_field": "endpoints"}}
        if re.search(r"entrypoint", text):
            return {"returns": {"entrypoint": "entrypoint"}}
    return {}


def _extract_constraints(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    rate = re.search(r"每秒最多调用\s*\**\s*(\d+)\s*\**\s*次", text)
    if rate:
        out["rate_limit_per_sec"] = int(rate.group(1))
    inst = re.search(r"最多同时运行\s*\**\s*(\d+)\s*\**\s*个实例", text)
    if inst:
        out["max_instances"] = int(inst.group(1))
    penalty = re.search(r"扣除[^。\n]{0,20}?(\d+(?:\.\d+)?)\s*%", text)
    if penalty:
        out["hint_penalty_ratio"] = float(penalty.group(1)) / 100.0
    if re.search(r"实例未运行|requires? .*running|必须先启动", text, re.I):
        out["requires_env_running"] = True
    if re.search(r"尚未解锁|关卡", text):
        out["level_lock"] = True
    if re.search(r"expireTime", text):
        out["env_expire_field"] = "expireTime"
    return out


def _extract_mcp_tools(text: str) -> list[dict[str, Any]]:
    """识别 MCP 工具表（工具名 + 参数）。"""
    caps: list[dict[str, Any]] = []
    for cells in _table_rows(text):
        if not cells:
            continue
        tool = cells[0].strip("`")
        if tool not in MCP_TOOL_MAP:
            continue
        name = MCP_TOOL_MAP[tool]
        args_raw = cells[2] if len(cells) > 2 else ""
        args = [a.strip("` ") for a in re.split(r"[、,，]", args_raw) if a.strip("` ")]
        caps.append({
            "name": name,
            "transport": "mcp",
            "spec": {"tool": tool, "args": {a: a for a in args if a in {"code", "flag"}}},
            "confidence": "high",
        })
    return caps


def extract_spec_heuristic(text: str) -> ParsedSpec:
    """确定性抽取（不依赖 LLM）。"""
    spec = ParsedSpec()
    spec.base_url = _extract_base_url(text)
    spec.auth_kind, spec.auth_header = _extract_auth(text)
    spec.constraints = _extract_constraints(text)

    is_mcp = bool(re.search(r"mcp|streamable[-_]http", text, re.I)) and "list_challenges" in text
    spec.platform_kind = "tencent_mcp" if is_mcp else (
        "dasctf" if "dasctf" in text.lower() else "generic_rest"
    )

    if is_mcp:
        spec.capabilities = _extract_mcp_tools(text)
    else:
        # T2：代码块/正文调用行 + 接口总览表 + curl，三者合并去重
        endpoints = merge_calls(_endpoints_from_overview(text), _calls_from_text(text), _curl_calls(text))
        seen: set[str] = set()
        seen_paths: set[str] = set()
        for ep in endpoints:
            # S2：不按关键词过滤——文档里有什么接口就记什么（name 仅作标识）
            name = _match_capability(ep["desc"], ep["path"]) or (
                "call_" + str(len(spec.capabilities) + 1))
            if name in seen:
                continue
            seen.add(name)
            seen_paths.add(ep["path"])
            payload: dict[str, Any] = {"method": ep["method"], "path": ep["path"]}
            payload.update(_response_field_hints(text, name))
            if name == "submit_flag":
                payload.setdefault("body", {"exercise_key": "exerciseId", "flag": "flag"})
            if name in {"build_env", "recover_env", "get_exercise"}:
                payload.setdefault("params", {"exercise_key": "exerciseId"})
            spec.capabilities.append({
                "name": name,
                "transport": "rest",
                "spec": payload,
                "confidence": "high" if ep["desc"] else "medium",
            })
        # 规则/公告类若只在总览出现但无 desc，仍按路径补
        for ep in endpoints:
            if ep["path"] in seen_paths:
                continue
            seen_paths.add(ep["path"])
            spec.capabilities.append({
                "name": "call_" + str(len(spec.capabilities) + 1),
                "transport": "rest",
                "spec": {"method": ep["method"], "path": ep["path"]},
                "confidence": "medium",
            })

    # 规则原文（有则沉淀）
    m = re.search(r"竞赛规则[\s\S]{0,2000}", text)
    if m:
        spec.rules_text = m.group(0)[:2000]
    if not spec.base_url:
        spec.notes.append("未识别到 Base URL：请在创建竞赛时手动填写")
    if not spec.auth_header:
        spec.notes.append("未识别到鉴权 Header 名：请手动确认")
    spec.confidence = "high" if len(spec.capabilities) >= 4 else ("medium" if spec.capabilities else "low")
    return spec


# ---------------------------------------------------------------------------
# 3) LLM 抽取（兜底）
# ---------------------------------------------------------------------------
LLM_PROMPT = """你是接口文档解析器。从下面的官方文档中抽取平台能力规格，只输出一个 JSON 对象。

要求：
- 只抽取文档中**明确存在**的信息；不确定的字段留空，不要编造路径。
- capabilities[].name 必须取自：get_rules, get_scoreboard, submit_flag, list_exercises,
  get_exercise, build_env, recover_env, get_notices, view_hint
- transport 为 "rest"（HTTP 接口）或 "mcp"（MCP 工具）
- 凭证只记录 header 名，不要输出任何密钥值

输出结构：
{"platformKind":"dasctf|tencent_mcp|generic_rest","baseUrl":"","authKind":"header|bearer",
 "authHeader":"","capabilities":[{"name":"","transport":"rest","spec":{"method":"","path":"","params":{},
 "body":{},"tool":"","args":{},"response":{},"verdict":{},"poll":{}},"confidence":"high|medium|low"}],
 "constraints":{"rate_limit_per_sec":null,"max_instances":null,"hint_penalty_ratio":0,
 "requires_env_running":false,"level_lock":false},
 "rulesText":"","notes":[]}

文档：
---
{document}
---"""


async def extract_spec_llm(text: str, model: Any, *, max_chars: int = 24000) -> ParsedSpec | None:
    """用现有模型通道抽取；失败返回 None（由调用方回退到启发式）。"""
    if model is None:
        return None
    try:
        from ..models.types import ChatMessage

        resp = await model.chat(
            [ChatMessage(role="user", content=LLM_PROMPT.replace("{document}", text[:max_chars]))]
        )
        raw = (resp.content or "").strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        data = json.loads(raw[start:end + 1])
    except Exception:  # noqa: BLE001 - 解析失败即回退
        return None
    spec = ParsedSpec(
        platform_kind=str(data.get("platformKind") or "generic_rest"),
        base_url=str(data.get("baseUrl") or "").rstrip("/"),
        auth_kind=str(data.get("authKind") or "header"),
        auth_header=str(data.get("authHeader") or ""),
        capabilities=list(data.get("capabilities") or []),
        constraints=dict(data.get("constraints") or {}),
        rules_text=str(data.get("rulesText") or ""),
        notes=list(data.get("notes") or []),
        confidence="medium",
    )
    return spec if spec.capabilities else None


#: LLM 兜底抽取的硬超时（秒）——超过就回退启发式，绝不把请求挂到分钟级
LLM_FALLBACK_TIMEOUT = 45


async def extract_spec(text: str, model: Any = None) -> ParsedSpec:
    """启发式优先；识别不足时才尝试 LLM（硬超时 45s），失败即回退启发式。

    阈值取 2：只要启发式认出 ≥2 个能力就直接返回，避免为了"更全"去等模型。
    """
    heuristic = extract_spec_heuristic(text)
    if len(heuristic.capabilities) >= 2:
        return heuristic
    try:
        llm_spec = await asyncio.wait_for(extract_spec_llm(text, model), timeout=LLM_FALLBACK_TIMEOUT)
    except (asyncio.TimeoutError, TimeoutError):
        heuristic.notes.append(
            f"AI 抽取超时（>{LLM_FALLBACK_TIMEOUT}s），已返回规则解析结果；"
            "可补充文档中的接口总览表后重试"
        )
        return heuristic
    if llm_spec and len(llm_spec.capabilities) > len(heuristic.capabilities):
        llm_spec.notes.append("由 LLM 兜底抽取，请人工核对路径与字段")
        return llm_spec
    if not heuristic.capabilities:
        heuristic.notes.append(
            "未能识别任何接口：请确认文档包含「接口总览」表格或 MCP 工具表；"
            "也可在能力表中手动编辑 spec"
        )
    return heuristic


# ---------------------------------------------------------------------------
# 4) 校验
# ---------------------------------------------------------------------------
def validate_spec(spec: ParsedSpec) -> list[str]:
    """返回错误列表（空 = 通过）。"""
    errors: list[str] = []
    names = set(spec.capability_names())
    for required in REQUIRED_CAPABILITIES:
        if required not in names:
            errors.append(f"缺少必需能力：{required}")
    if not spec.base_url:
        errors.append("缺少 base_url")
    if spec.auth_kind == "header" and not spec.auth_header:
        errors.append("缺少鉴权 Header 名（auth_header）")
    for cap in spec.capabilities:
        if not cap.get("name"):
            errors.append("存在无名能力")
        if cap.get("transport") == "rest" and not (cap.get("spec") or {}).get("path"):
            errors.append(f"能力 {cap.get('name')} 缺少 path")
        if cap.get("transport") == "mcp" and not (cap.get("spec") or {}).get("tool"):
            errors.append(f"能力 {cap.get('name')} 缺少 tool")
    return errors


# ---------------------------------------------------------------------------
# 5) 落库
# ---------------------------------------------------------------------------
async def persist_spec(repo: Any, competition_id: str, spec: ParsedSpec) -> dict[str, Any]:
    """把解析结果写入竞赛黑板 + 能力注册表（能力默认关闭）。"""
    for cap in spec.capabilities:
        await repo.upsert_capability(
            competition_id,
            name=str(cap["name"]),
            transport=str(cap.get("transport") or "rest"),
            spec=dict(cap.get("spec") or {}),
            confidence=str(cap.get("confidence") or "medium"),
        )
        await repo.add_fact(
            competition_id,
            kind="protocol",
            summary=f"能力 {cap['name']}（{cap.get('transport')}，{cap.get('confidence')}）",
            body=json.dumps(cap.get("spec") or {}, ensure_ascii=False, indent=2),
            source="doc",
            ref=f"capability:{cap['name']}",
        )
    if spec.rules_text:
        await repo.add_fact(
            competition_id, kind="rule", summary="竞赛规则（文档摘录）",
            body=spec.rules_text[:4000], source="doc", ref="doc:rules",
        )
    if spec.constraints:
        await repo.add_fact(
            competition_id, kind="constraint", summary="平台约束（限流/实例/扣分）",
            body=json.dumps(spec.constraints, ensure_ascii=False, indent=2),
            source="doc", ref="doc:constraints",
        )
    await repo.add_fact(
        competition_id, kind="credential", summary="凭证边界（只记 Header 名与引用）",
        body=f"auth_kind={spec.auth_kind}\\nauth_header={spec.auth_header or '-'}\\n"
             "（密钥值不入黑板，请在竞赛配置的 auth_ref 指向的配置项中维护）",
        source="doc", ref="doc:credential",
    )
    return {
        "capabilities": spec.capability_names(),
        "constraints": spec.constraints,
        "notes": spec.notes,
        "confidence": spec.confidence,
    }


# ---------------------------------------------------------------------------
# S1：文档 → 黑板事实（不预设能力槽位，文档有什么就记什么）
# ---------------------------------------------------------------------------
#: AI 分析用的提示词：产出「事实列表」，不要求匹配固定接口名
FACT_PROMPT = """你是比赛平台文档分析助手。请阅读文档，把它拆成若干条**事实**存入知识库。
每条事实包含：
- kind: protocol（接口）/ rule（规则）/ constraint（限制：限流、并发、扣分等）/ notice（注意事项）/ credential（鉴权方式）
- summary: 一句话说明（接口用途或约束内容）
- body: 原文关键片段（务必保留请求方法、路径、参数、返回字段、示例、错误码）
- ref: 可选，原文小节标题

要求：
1. 不要预设固定的接口清单——文档里有什么就记什么，接口名原样保留；
2. 不要臆造文档中不存在的信息；不确定的宁可保留原文；
3. 一个接口一条事实；同一章节的规则可合并为一条；
4. 只输出一个 JSON 对象：{"facts":[{"kind":"...","summary":"...","body":"...","ref":"..."}]}

文档：
---
{document}
---"""




# ---------------------------------------------------------------------------
# T2：代码块里的接口调用识别（几乎所有平台文档都用代码块展示调用）
# ---------------------------------------------------------------------------
_CALL_LINE = re.compile(
    r"(?:^|\n)[^\S\n]*\**\s*(GET|POST|PUT|PATCH|DELETE)\s+\**\s*"
    r"((?:https?://[^\s`\"']+)|/[^\s`\"']+)",
    re.I,
)


def _calls_from_text(text: str) -> list[dict[str, str]]:
    """扫描全文中的 HTTP 调用行（代码块内外都认）。

    形式：``GET /path``、``POST https://host/path``、curl 命令里的 ``-X POST url``。
    说明取路径行之前最近的非空行（通常是编号 + 用途）。
    """
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        match = _CALL_LINE.search(line)
        if not match:
            continue
        method = match.group(1).upper()
        path = match.group(2).strip().strip("`\"'").rstrip("）；)")
        # curl -X POST 形式：行里可能没有直接的 METHOD+path 相邻，单独处理 curl
        if path.startswith("http"):
            # 完整 URL：截取 host 之后保留 path（base_url 由竞赛配置拼接）
            parsed_path = "/" + path.split("/", 3)[3] if path.count("/") >= 3 else path
            if path in seen:
                continue
            seen.add(path)
        else:
            if path in seen:
                continue
            seen.add(path)
            parsed_path = path
        # 说明：往前找最近的非空行
        desc = ""
        for back in range(idx - 1, max(-1, idx - 6), -1):
            prev = lines[back].strip()
            if not prev or prev.startswith(("```", "|", "#")):
                continue
            if _CALL_LINE.search(prev):
                continue
            desc = re.sub(r"^\d+[\.、]\s*", "", prev).strip("`\"' *")[:120]
            break
        found.append({"method": method, "path": parsed_path, "desc": desc})
    return found


def _curl_calls(text: str) -> list[dict[str, str]]:
    """识别 curl -X POST 'https://host/path' 形式。"""
    found: list[dict[str, str]] = []
    for m in re.finditer(r"curl\s+[^\\n]*?-X\s*(GET|POST|PUT|PATCH|DELETE)\s+['\"]?(https?://[^\s'\"\\]+)", text, re.I):
        method, url = m.group(1).upper(), m.group(2)
        path = "/" + url.split("/", 3)[3] if url.count("/") >= 3 else "/"
        if path in seen_path_set(found):
            continue
        found.append({"method": method, "path": path, "desc": ""})
    return found


def seen_path_set(items: list[dict[str, str]]) -> set:
    return {i.get("path") for i in items}


def merge_calls(*lists: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for lst in lists:
        for item in lst:
            if item["path"] in seen:
                continue
            seen.add(item["path"])
            out.append(item)
    return out


def heuristic_facts(text: str) -> list[dict[str, Any]]:
    """规则抽取（始终执行，保证信息不丢）。

    - 接口总览表行 → protocol 事实（保留整行原文）
    - 鉴权 Header → credential 事实（只记名字）
    - 含限制语义的句子 → constraint 事实
    - 其余章节正文 → rule/notice 事实（按小节整段保存）
    """
    facts: list[dict[str, Any]] = []
    # ⓪ 代码块/正文里的接口调用行（先于接口表：几乎全部平台文档都有）
    code_calls = merge_calls(_calls_from_text(text), _curl_calls(text))
    for ep in code_calls:
        facts.append({
            "kind": "protocol",
            "summary": f"{ep['method']} {ep['path']}" + (f" — {ep['desc']}" if ep["desc"] else ""),
            "body": f"方法: {ep['method']}\n路径: {ep['path']}\n说明: {ep['desc'] or '（原文未注明，请参考文档上下文）'}",
            "ref": "调用示例",
        })
    # ① 接口表
    for ep in _endpoints_from_overview(text):
        desc = ep.get("desc") or ""
        facts.append({
            "kind": "protocol",
            "summary": f"{ep['method']} {ep['path']}" + (f" — {desc}" if desc else ""),
            "body": f"方法: {ep['method']}\n路径: {ep['path']}\n说明: {desc or '（原文未注明）'}",
            "ref": "接口总览",
        })
    # ② 鉴权
    auth_kind, auth_header = _extract_auth(text)
    if auth_header:
        facts.append({
            "kind": "credential",
            "summary": f"鉴权方式：{auth_kind} / Header「{auth_header}」（值需另行提供）",
            "body": f"auth_kind={auth_kind}\nauth_header={auth_header}\n"
                    "（凭证值不写入黑板，请在指挥台「提供凭证」中录入，仅存进程内存）",
            "ref": "认证",
        })
    # ③ 约束句
    constraints = _extract_constraints(text)
    if constraints:
        facts.append({
            "kind": "constraint",
            "summary": "平台限制（限流/实例/扣分等）",
            "body": json.dumps(constraints, ensure_ascii=False, indent=2),
            "ref": "约束",
        })
    # ④ 章节正文（保证不丢信息）
    for section in split_sections(text):
        body = section.body.strip()
        if len(body) < 60 or not section.title:
            continue
        if any(k in section.title for k in ("接口总览", "认证", "Base URL", "公共约定")):
            continue
        kind = "rule" if any(k in section.title for k in ("规则", "赛制", "说明", "流程")) else "notice"
        facts.append({
            "kind": kind,
            "summary": section.title[:80],
            "body": _sanitize_secrets(body)[:4000],
            "ref": section.title[:60],
        })
    # 去重（summary 相同）
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for fact in facts:
        if fact["kind"] == "protocol":
            path_match = re.search(r"路径[:：]\s*(\S+)", fact.get("body") or "")
            if path_match and path_match.group(1) in seen_paths:
                continue  # 同一接口已有代码块调用版本，跳过表格重复
            if path_match:
                seen_paths.add(path_match.group(1))
        key = fact["kind"] + "|" + fact["summary"]
        if key in seen:
            continue
        seen.add(key)
        out.append(fact)
    return out


async def extract_facts(
    text: str,
    model: Any = None,
    *,
    use_ai: bool = True,
    timeout_seconds: int = 45,
) -> tuple[list[dict[str, Any]], list[str]]:
    """文档 → 事实列表。

    规则抽取**始终执行**（保证不丢信息）；AI 分析作为增强（补充接口用途/流程/注意事项），
    失败或超时都不影响已有事实。
    """
    facts = heuristic_facts(text)
    notes: list[str] = []
    if not use_ai or model is None:
        notes.append("未启用 AI 增强：已按文档结构抽取事实（接口表/鉴权/约束/章节）")
        return facts, notes
    try:
        from ..models.types import ChatMessage

        resp = await asyncio.wait_for(
            model.chat([ChatMessage(role="user", content=FACT_PROMPT.replace("{document}", text[:24000]))]),
            timeout=timeout_seconds,
        )
        raw = (resp.content or "").strip()
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start:end + 1]) if start >= 0 and end > start else {}
        ai_facts = [
            {
                "kind": str(f.get("kind") or "notice"),
                "summary": str(f.get("summary") or "").strip()[:200],
                "body": str(f.get("body") or "").strip()[:4000],
                "ref": str(f.get("ref") or "")[:80] or None,
            }
            for f in (data.get("facts") or [])
            if str(f.get("summary") or "").strip()
        ]
        # AI 事实与规则事实合并去重（AI 的接口条目更完整时优先保留）
        existing = {f["kind"] + "|" + f["summary"] for f in facts}
        added = 0
        for fact in ai_facts:
            if fact["kind"] not in {"protocol", "rule", "constraint", "notice", "credential"}:
                fact["kind"] = "notice"
            key = fact["kind"] + "|" + fact["summary"]
            if key in existing:
                continue
            existing.add(key)
            facts.append(fact)
            added += 1
        notes.append(f"AI 分析补充 {added} 条事实（共 {len(facts)} 条）")
    except (asyncio.TimeoutError, TimeoutError):
        notes.append(f"AI 分析超时（>{timeout_seconds}s）：已保留规则抽取的 {len(facts)} 条事实")
    except Exception as exc:  # noqa: BLE001 - AI 失败不影响规则结果
        notes.append(f"AI 分析失败（{type(exc).__name__}）：已保留规则抽取的 {len(facts)} 条事实")
    return facts, notes


async def persist_facts(repo: Any, competition_id: str, facts: list[dict[str, Any]]) -> dict[str, int]:
    """把事实逐条写入竞赛全局黑板（append-only）。"""
    counts: dict[str, int] = {}
    for fact in facts:
        kind = fact.get("kind") or "notice"
        await repo.add_fact(
            competition_id,
            kind=kind,
            summary=_sanitize_secrets(str(fact.get("summary") or ""))[:200],
            body=_sanitize_secrets(str(fact.get("body") or ""))[:4000],
            source="doc",
            ref=fact.get("ref"),
        )
        counts[kind] = counts.get(kind, 0) + 1
    return counts
