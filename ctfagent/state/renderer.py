"""渲染式上下文：每轮**固定预算**渲染状态块，替代"塞全量历史再被动压缩"。

设计要点
--------
1. **信噪比恒定**：无论任务跑了 10 分钟还是 10 小时，Worker 每轮看到的账本规模都固定在
   ``budget_tokens`` 内（Idea top-K + Memory top-K），不会随历史增长而稀释注意力。
2. **绝不超预算**：超预算时按 **note → evidence → 旧 pending Idea** 的低→高优先级丢弃，
   仍然超预算则压缩交接包、最后硬截断（绝对保证 ``estimate_tokens(block) <= budget_tokens``）。
3. **body 不渲染**：Memory 只渲染一行 summary，完整细节让模型按需用 ``blackboard_query`` 取，
   这样长证据不会挤占上下文。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..agent.context import estimate_tokens

__all__ = ["RenderBudget", "RenderInputs", "render_delta", "render_worker_context"]

TRUNCATION_MARK = "…（已按预算截断）"

#: 丢弃顺序：数字越小越先被丢弃（note → evidence → 旧 pending Idea）
_KIND_DROP_ORDER: dict[str, int] = {"note": 0, "evidence": 1, "fact": 3, "failure": 3, "hint": 4}


@dataclass
class RenderBudget:
    """渲染预算（对应配置 ``state_v2.renderer``）。"""

    budget_tokens: int = 3_000
    idea_top: int = 6
    memory_top: int = 6
    handoff_max_chars: int = 900

    def normalized(self) -> RenderBudget:
        return RenderBudget(
            budget_tokens=max(200, int(self.budget_tokens)),
            idea_top=max(1, int(self.idea_top)),
            memory_top=max(1, int(self.memory_top)),
            handoff_max_chars=max(120, int(self.handoff_max_chars)),
        )


@dataclass
class RenderInputs:
    """渲染所需的数据（由调用方一次性查好，渲染函数保持纯函数、可单测）。"""

    challenge: dict[str, Any] | None = None
    idea_text: str = ""
    task_type: str = "explore"
    handoff: str = ""
    ideas: list[dict[str, Any]] = field(default_factory=list)
    memories: list[dict[str, Any]] = field(default_factory=list)
    own_idea_id: str | None = None
    own_solver_run_id: str | None = None
    #: M2：idea_id → 来源 memory id 列表（因果边，渲染时显示 ← f3+f9）
    idea_sources: dict[str, list[str]] = field(default_factory=dict)
    observer_reminders: list[str] = field(default_factory=list)
    flag_pattern: str = ""
    time_remaining_minutes: float | None = None
    elapsed_minutes: float | None = None


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def _idea_line(
    idea: dict[str, Any],
    *,
    own_idea_id: str | None,
    own_run: str | None,
    sources: list[str] | None = None,
) -> tuple[str, int]:
    """渲染一条 Idea，返回 ``(文本, 丢弃优先级)``。M2 起带因果来源前缀。"""
    status = str(idea.get("status") or "pending")
    badge = {"pending": "待验证", "testing": "进行中", "verified": "已验证", "failed": "已排除"}.get(
        status, status
    )
    claimed = idea.get("claimedBy")
    if idea.get("id") == own_idea_id:
        badge += "·我的"
    elif status == "testing" and claimed and claimed != own_run:
        badge += "·他人进行中，勿重复认领"
    text = " ".join(str(idea.get("text") or "").split())
    if len(text) > 160:
        text = text[:157] + "…"
    origin = f" ← {'+'.join(sources[:3])}" if sources else ""
    return f"- [{badge}]{origin} {text}", 0 if status == "pending" else 1


def _memory_line(memory: dict[str, Any]) -> tuple[str, int]:
    kind = str(memory.get("kind") or "note")
    badge = {
        "fact": "事实",
        "evidence": "证据",
        "failure": "此路不通",
        "note": "观察",
        "hint": "人类提示",
    }.get(kind, kind)
    summary = " ".join(str(memory.get("summary") or "").split())
    if len(summary) > 200:
        summary = summary[:197] + "…"
    origin = f" (from {memory['fromIdeaId']})" if memory.get("fromIdeaId") else ""
    return f"- [{badge}]{origin} {summary}", _KIND_DROP_ORDER.get(kind, 2)


def render_delta(memories: list[dict[str, Any]], *, limit: int = 3) -> str:
    """M3：渲染"其他 Solver 新增事实"的增量摘要（≤ limit 行，供运行中注入）。"""
    lines: list[str] = []
    for memory in memories[: max(1, int(limit))]:
        kind = str(memory.get("kind") or "note")
        badge = {
            "fact": "事实",
            "evidence": "证据",
            "failure": "此路不通",
            "note": "观察",
            "hint": "人类提示",
        }.get(kind, kind)
        summary = " ".join(str(memory.get("summary") or "").split())
        if len(summary) > 120:
            summary = summary[:117] + "…"
        origin = f" ← {memory['fromIdeaId']}" if memory.get("fromIdeaId") else ""
        lines.append(f"- {memory.get('id')} [{badge}]{origin} {summary}")
    return "\n".join(lines)


def _challenge_card(inputs: RenderInputs) -> list[str]:
    challenge = inputs.challenge or {}
    lines = ["[题目卡]"]
    if challenge:
        lines.append(f"- 题目：{challenge.get('name') or '(未命名)'}（{challenge.get('id') or '?'}）")
        lines.append(f"- 分类：{challenge.get('category') or 'misc'}")
        target = challenge.get("target") or "（未填写——若为靶机题必须先向用户澄清）"
        lines.append(f"- 目标：{target}")
        lines.append(
            f"- flag 格式：{inputs.flag_pattern or challenge.get('flagPattern') or '（未指定，默认 xxxx{...}）'}"
        )
        description = " ".join(str(challenge.get("description") or "").split())
        if description:
            lines.append(f"- 描述要点：{description[:400]}")
    else:
        lines.append("- 未登记题目元数据：缺少合法目标时必须停止并输出缺失信息清单。")
    if inputs.elapsed_minutes is not None:
        if inputs.time_remaining_minutes is not None:
            lines.append(
                f"- 时间：已用 {inputs.elapsed_minutes:.0f} 分钟，剩余 {inputs.time_remaining_minutes:.0f} 分钟"
            )
        else:
            lines.append(f"- 时间：已用 {inputs.elapsed_minutes:.0f} 分钟")
    return lines


def _task_block(inputs: RenderInputs) -> list[str]:
    lines = ["", f"[我的任务] task_type={inputs.task_type}"]
    if inputs.idea_text:
        lines.append(f"- 本轮 Idea：{inputs.idea_text}")
    if inputs.handoff:
        lines.append("- 交接包：")
        lines.append(inputs.handoff)
    return lines


def _output_requirements() -> list[str]:
    return [
        "",
        "[输出要求] 本任务结束前必须完成两件事，缺一不可：",
        "1. 用 idea_transition 收尾你认领的 Idea（走通→verified，受阻→failed 并写明已排除的路径）；",
        "2. 至少用 memory_write 沉淀一条本次任务唯一确定的结论（成功→fact；受阻→failure）。",
        "结论只写一行 summary；完整细节放 body，需要时用 blackboard_query 取回。",
    ]


def render_worker_context(
    challenge: dict[str, Any] | None = None,
    solver_run: dict[str, Any] | None = None,
    *,
    budget_tokens: int = 3_000,
    idea_top: int = 6,
    memory_top: int = 6,
    handoff_max_chars: int = 900,
    inputs: RenderInputs | None = None,
) -> str:
    """渲染 Worker 每轮的状态块（保证不超过 ``budget_tokens``）。

    ``challenge`` / ``solver_run`` 是提示词约定的位置参数；需要更细的控制时用 ``inputs``
    直接传入渲染数据（两者可混用：``inputs`` 中的字段优先，缺省回落到 ``solver_run``）。
    """
    budget = RenderBudget(
        budget_tokens=budget_tokens,
        idea_top=idea_top,
        memory_top=memory_top,
        handoff_max_chars=handoff_max_chars,
    ).normalized()

    data = inputs or RenderInputs()
    if challenge is not None and data.challenge is None:
        data.challenge = challenge
    if solver_run:
        data.task_type = str(solver_run.get("taskType") or data.task_type)
        data.idea_text = data.idea_text or str(solver_run.get("ideaText") or "")
        data.handoff = data.handoff or str(solver_run.get("handoff") or "")
        data.own_solver_run_id = data.own_solver_run_id or solver_run.get("id")
        data.own_idea_id = data.own_idea_id or solver_run.get("ideaId")
    if not data.flag_pattern and data.challenge:
        data.flag_pattern = str(data.challenge.get("flagPattern") or "")

    # 交接包预压缩：超限先在此截断（派发方本应预压缩，这里兜底）
    handoff = data.handoff.strip()
    if len(handoff) > budget.handoff_max_chars:
        handoff = handoff[: budget.handoff_max_chars - len(TRUNCATION_MARK)] + TRUNCATION_MARK
    data.handoff = handoff

    # 候选条目（含丢弃优先级）
    idea_items: list[tuple[str, int, str]] = []
    for idea in data.ideas[: budget.idea_top]:
        text, prio = _idea_line(
            idea,
            own_idea_id=data.own_idea_id,
            own_run=data.own_solver_run_id,
            sources=data.idea_sources.get(str(idea.get("id") or "")),
        )
        idea_items.append((text, prio, str(idea.get("id") or "")))
    memory_items: list[tuple[str, int, str]] = []
    for memory in data.memories[: budget.memory_top]:
        text, prio = _memory_line(memory)
        memory_items.append((text, prio, str(memory.get("id") or "")))
    reminder_items: list[tuple[str, int, str]] = [
        (f"- {r}", 1, f"reminder-{i}") for i, r in enumerate(data.observer_reminders)
    ]

    def build() -> str:
        lines = _challenge_card(data)
        lines += _task_block(data)
        lines.append("")
        lines.append(f"[Idea top {budget.idea_top}（状态徽标 + 一行文本）]")
        if idea_items:
            lines.extend(text for text, _, _ in idea_items)
        else:
            lines.append("- （暂无 Idea：请先 idea_upsert 声明方向）")
        lines.append("")
        lines.append(f"[Memory 最近 {budget.memory_top}（hint > failure > fact/evidence > note）]")
        if memory_items:
            lines.extend(text for text, _, _ in memory_items)
        else:
            lines.append("- （暂无 Memory：每确认一条新认知立即 memory_write）")
        if reminder_items:
            lines.append("")
            lines.append("[Observer 提醒]（系统检测到效率问题，请调整策略）")
            lines.extend(text for text, _, _ in reminder_items)
        lines += _output_requirements()
        return "\n".join(lines)

    block = build()

    # ---- 超预算处理：note → evidence → 旧 pending Idea → 提醒/交接包 → 硬截断 ----
    if estimate_tokens(block) > budget.budget_tokens:
        droppable: list[tuple[int, str, str, str]] = []  # (priority, kind, id, "")
        droppable += [(prio, "memory", key, "") for _, prio, key in memory_items]
        droppable += [(prio, "idea", key, "") for _, prio, key in idea_items]
        droppable += [(0, "reminder", key, "") for _, _, key in reminder_items]
        droppable.sort(key=lambda item: item[0])
        while droppable and estimate_tokens(block) > budget.budget_tokens:
            _, kind, key, _ = droppable.pop(0)
            if kind == "memory":
                memory_items[:] = [item for item in memory_items if item[2] != key]
            elif kind == "idea":
                idea_items[:] = [item for item in idea_items if item[2] != key]
            else:
                reminder_items[:] = [item for item in reminder_items if item[2] != key]
            block = build()

    if estimate_tokens(block) > budget.budget_tokens:
        # 仍超预算：先丢掉交接包（派发方本应预压缩，这里兜底）
        data.handoff = ""
        block = build()
    if estimate_tokens(block) > budget.budget_tokens:
        # 极端兜底：按字符收缩并预留截断标记的空间，**绝对保证**不超预算
        marker_cost = estimate_tokens(TRUNCATION_MARK)
        text = block
        while text and estimate_tokens(text) + marker_cost > budget.budget_tokens:
            text = text[: int(len(text) * 0.9)]
        block = text + TRUNCATION_MARK
    return block
