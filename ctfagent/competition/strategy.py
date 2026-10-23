"""竞赛策略引擎（M8）——规则驱动，非 LLM。

职责边界：
- **选题**：硬约束过滤 + 加权评分（分值/难度/新鲜度/关卡/同类经验/环境成本）；
- **提交**：格式预校验 + 去重 + 冷却 + 退避；
- **环境**：启动/轮询/回收的决策（实际调用由 adapters 执行）；
- **不同步 LLM**：所有决策可解释、可单测、可调参。

与现有流水线的关系：引擎只决定「解哪道、何时提交」，解题仍由 Dispatcher/Solver 完成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DEFAULT_STRATEGY",
    "Backoff",
    "SubmissionDecision",
    "backoff_for",
    "decide_submission",
    "filter_candidates",
    "score_exercise",
    "select_next_exercise",
]

#: 默认策略参数（前端可调，落 competitions.strategy）
DEFAULT_STRATEGY: dict[str, Any] = {
    "w_score": 0.35,
    "w_difficulty": 0.25,
    "w_fresh": 0.20,
    "w_level": 0.15,
    "w_category": 0.05,
    "max_attempts": 5,
    # 同一题目的最大自动派发次数（session 被终止后的受控重试上限；
    # 达上限后纳入冷却，等 attempt 过期自动换题）
    "max_dispatch_per_exercise": 3,
    "cooldown_minutes": 10,
    "wrong_before_cooldown": 3,
    "flag_regex": "",
    # 竞赛自动化的意义就是解题：默认开启自动派发（可在策略里显式关掉）
    "auto_solve": True,
    "hint_enabled": False,
}

_DIFFICULTY_RANK = {"easy": 0.2, "简单": 0.2, "medium": 0.5, "中等": 0.5,
                    "hard": 0.8, "困难": 0.8, "expert": 1.0, "专家": 1.0}


@dataclass
class Backoff:
    seconds: float
    reason: str = ""


@dataclass
class SubmissionDecision:
    allow: bool
    reason: str = ""
    cooldown_minutes: float = 0.0
    mark_solved: bool = False


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _difficulty_ratio(difficulty: str) -> float:
    text = str(difficulty or "").strip().lower()
    if text in _DIFFICULTY_RANK:
        return _DIFFICULTY_RANK[text]
    try:
        return min(1.0, max(0.0, float(text) / 10.0))
    except ValueError:
        return 0.5


# ---------------------------------------------------------------------------
# 选题
# ---------------------------------------------------------------------------
def filter_candidates(
    exercises: list[dict[str, Any]],
    *,
    running_instances: int = 0,
    max_instances: int | None = None,
    cooldown: set[str] | None = None,
    max_attempts: int = 5,
) -> list[dict[str, Any]]:
    """硬约束过滤：已解 / locked / 冷却 / 尝试超限 / 实例配额。"""
    cooling = cooldown or set()
    out: list[dict[str, Any]] = []
    for ex in exercises:
        if ex.get("status") in {"solved", "locked"}:
            continue
        if int(ex.get("flagsGot") or 0) >= int(ex.get("flagsTotal") or 1):
            continue
        key = str(ex.get("exerciseKey") or "")
        if key in cooling:
            continue
        if int(ex.get("attempts") or 0) >= max_attempts:
            continue
        out.append(ex)
    # 实例配额已满时，只保留"已在运行"的题（不能新起环境）
    if max_instances is not None and running_instances >= max_instances:
        out = [ex for ex in out if ex.get("status") == "running"]
    return out


def score_exercise(
    exercise: dict[str, Any],
    *,
    strategy: dict[str, Any] | None = None,
    max_score: float | None = None,
    current_level: str | None = None,
    preferred_categories: set[str] | None = None,
) -> float:
    """加权评分（越高越优先）。"""
    cfg = {**DEFAULT_STRATEGY, **(strategy or {})}
    score = _num(exercise.get("score"))
    norm_score = score / max_score if max_score and max_score > 0 else (1.0 if score > 0 else 0.0)
    diff = _difficulty_ratio(str(exercise.get("difficulty") or ""))
    attempts = int(exercise.get("attempts") or 0)
    max_attempts = max(1, int(cfg.get("max_attempts") or 5))
    freshness = 1.0 - min(1.0, attempts / max_attempts)
    level_affinity = 1.0 if (current_level is None or str(exercise.get("group") or "") == str(current_level)) else 0.3
    category_fit = 1.0 if (preferred_categories and str(exercise.get("category") or "") in preferred_categories) else 0.0
    return (
        float(cfg.get("w_score") or 0) * norm_score
        + float(cfg.get("w_difficulty") or 0) * (1.0 - diff)
        + float(cfg.get("w_fresh") or 0) * freshness
        + float(cfg.get("w_level") or 0) * level_affinity
        + float(cfg.get("w_category") or 0) * category_fit
    )


def select_next_exercise(
    exercises: list[dict[str, Any]],
    *,
    strategy: dict[str, Any] | None = None,
    pinned: set[str] | None = None,
    running_instances: int = 0,
    max_instances: int | None = None,
    cooldown: set[str] | None = None,
    current_level: str | None = None,
    preferred_categories: set[str] | None = None,
) -> str | None:
    """选出下一道要解的题；``pinned``（用户指定）优先于一切评分。"""
    cfg = {**DEFAULT_STRATEGY, **(strategy or {})}
    candidates = filter_candidates(
        exercises,
        running_instances=running_instances,
        max_instances=max_instances,
        cooldown=cooldown,
        max_attempts=int(cfg.get("max_attempts") or 5),
    )
    if not candidates:
        return None
    pinned = pinned or set()
    pinned_hits = [ex for ex in candidates if str(ex.get("exerciseKey")) in pinned]
    pool = pinned_hits or candidates
    max_score = max((_num(ex.get("score")) for ex in pool), default=0.0)
    best = max(
        pool,
        key=lambda ex: (
            score_exercise(
                ex, strategy=cfg, max_score=max_score,
                current_level=current_level, preferred_categories=preferred_categories,
            ),
            str(ex.get("exerciseKey")),
        ),
    )
    return str(best.get("exerciseKey"))


# ---------------------------------------------------------------------------
# 提交
# ---------------------------------------------------------------------------
def decide_submission(
    *,
    exercise: dict[str, Any],
    flag: str,
    already_submitted: set[str],
    wrong_count: int = 0,
    strategy: dict[str, Any] | None = None,
) -> SubmissionDecision:
    """提交前决策：格式 / 去重 / 冷却 / 已解。"""
    cfg = {**DEFAULT_STRATEGY, **(strategy or {})}
    text = (flag or "").strip()
    if not text:
        return SubmissionDecision(False, "空 flag")
    if len(text) > 256:
        return SubmissionDecision(False, "flag 超过 256 字符（平台限制）")
    regex = str(cfg.get("flag_regex") or "").strip()
    if regex:
        import re

        if not re.search(regex, text):
            return SubmissionDecision(False, f"flag 不匹配平台格式：{regex}")
    if text in already_submitted:
        return SubmissionDecision(False, "重复提交（已提交过同一 flag）", mark_solved=True)
    if exercise.get("status") == "solved":
        return SubmissionDecision(False, "该题已解")
    if int(exercise.get("flagsGot") or 0) >= int(exercise.get("flagsTotal") or 1):
        return SubmissionDecision(False, "该题 flag 已收齐", mark_solved=True)
    if wrong_count >= int(cfg.get("wrong_before_cooldown") or 3):
        return SubmissionDecision(
            False, f"连续 {wrong_count} 次错误，进入冷却",
            cooldown_minutes=float(cfg.get("cooldown_minutes") or 10),
        )
    return SubmissionDecision(True, "允许提交")


def backoff_for(error_kind: str, attempt: int) -> Backoff:
    """错误 → 退避秒数（指数退避，封顶 60s；鉴权错误不退避而是暂停）。"""
    if error_kind == "auth_error":
        return Backoff(0.0, "鉴权失败：应暂停竞赛并人工检查凭证")
    if error_kind == "invalid_flag":
        return Backoff(0.0, "答案错误：不重试，记录失败模式")
    if error_kind in {"unsupported", "spec_error"}:
        return Backoff(0.0, "能力不可用：应停用该能力")
    base = {"rate_limited": 2.0, "platform_down": 5.0}.get(error_kind, 3.0)
    return Backoff(min(60.0, base * (2 ** max(0, attempt - 1))), f"{error_kind} 退避")
