"""v2 状态账本：Idea 状态机 + Memory 类型学 + 渲染式上下文。

模块构成（对应 v2 升级提示词 §1.3）::

    clock.py     时间工具（ISO-8601 UTC 文本，与迁移 002 的表结构约定一致）
    ideas.py     Idea 状态机（pending→testing→verified/failed/skipped）+ 联动写 Memory
    memory.py    Memory 类型学（fact/evidence/failure/note/hint，追加式）
    renderer.py  渲染式上下文（固定预算，绝不超预算）

与 v1 的分工：v1 的 ``facts`` 表是**对话级**黑板（``blackboard_*`` 工具）；
本包是**挑战级**账本（``idea_*`` / ``memory_write`` / ``blackboard_query`` 工具），
两者并存互补：前者记录"我确认了什么"，后者记录"接下来该往哪走 + 已排除什么"。
"""

from __future__ import annotations

__all__ = ["clock", "ideas", "memory", "renderer"]
