"""竞赛引擎（M8）——策略驱动的主循环。

一轮 tick 的顺序：
1. ``sync``：拉规则/题目（只读能力）→ 黑板 + 题目池
2. ``schedule``：选题（pin 优先 + 加权评分）
3. ``prepare_env``：需要环境且未就绪 → build（REST 轮询 / MCP 直取 entrypoint）
4. ``dispatch``：``auto_solve=True`` 时把题交给现有 Dispatcher（可选，默认关闭）
5. ``collect + submit``：把本地已捕获未提交的 flag 交给平台，结果写回黑板

设计：引擎不碰 LLM、不直接解题；所有平台调用经 adapters；所有决策可解释并写黑板。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .adapters import NormalizedResult, PlatformAdapter, build_adapter
from .logbus import LOG_BUS
from .strategy import (
    DEFAULT_STRATEGY,
    backoff_for,
    decide_submission,
    select_next_exercise,
)

logger = logging.getLogger(__name__)

__all__ = ["CompetitionEngine"]


class CompetitionEngine:
    """单个竞赛的自动化引擎（可 start/pause，状态存 DB）。"""

    def __init__(self, runtime: Any, competition_id: str, *, interval_seconds: float = 10.0) -> None:
        self.runtime = runtime
        self.repo = runtime.repos.competitions
        self.competition_id = competition_id
        self.interval = max(3.0, float(interval_seconds))
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self.last_tick: dict[str, Any] = {}

    def _log(self, phase: str, message: str, *, level: str = "info") -> None:
        """写一条带阶段的决策日志（同步，不阻塞主循环）。"""
        LOG_BUS.publish(self.competition_id, phase, message, level=level)

    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _competition(self) -> dict[str, Any]:
        comp = await self.repo.get(self.competition_id)
        if comp is None:
            raise ValueError("竞赛不存在")
        return comp

    async def _constraints(self) -> dict[str, Any]:
        facts = await self.repo.list_facts(self.competition_id, kinds=["constraint"], limit=1)
        if not facts:
            return {}
        try:
            return json.loads(facts[0].get("body") or "{}")
        except ValueError:
            return {}

    async def _strategy(self) -> dict[str, Any]:
        comp = await self._competition()
        return {**DEFAULT_STRATEGY, **(comp.get("strategy") or {})}

    async def adapter(self, *, only_enabled: bool = True) -> PlatformAdapter:
        """黑板驱动的适配器（S3）：能力来自黑板 protocol 事实，而非 capabilities 表。"""
        facts = await self.repo.list_facts(self.competition_id, limit=500)
        caps = self._caps_from_board(facts)
        comp = await self._competition()
        return build_adapter(
            comp, caps, constraints=await self._constraints(),
            only_enabled=only_enabled, runtime=self.runtime,
        )


    # ------------------------------------------------------------------
    # S3：黑板即协议——从 protocol 事实解析出可调用的能力
    # ------------------------------------------------------------------
    def _caps_from_board(self, facts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """从黑板 protocol 事实解析 (method, path) 构造动态能力。

        事实 body 约定含「方法: GET / 路径: /xxx」（doc_parser.heuristic_facts 产出）。
        解析失败的条目跳过（不阻塞其他能力）。
        """
        import re as _re

        caps: dict[str, dict[str, Any]] = {}
        seen: set[tuple[str, str]] = set()
        for fact in facts:
            if fact.get("kind") != "protocol":
                continue
            body = str(fact.get("body") or "")
            method = _re.search(r"方法[:：]\s*([A-Za-z]+)", body)
            path = _re.search(r"路径[:：]\s*(\S+)", body)
            if not (method and path):
                continue
            sig = (method.group(1).upper(), path.group(1))
            if sig in seen:
                continue  # 同一接口的重复事实（文档被多次解析）只保留一条
            seen.add(sig)
            name = "proto:" + str(fact.get("id") or fact.get("summary", ""))[:24]
            caps[name] = {
                "name": name,
                "transport": "rest",
                "enabled": True,
                "spec": {"method": method.group(1).upper(), "path": path.group(1)},
                "summary": str(fact.get("summary") or ""),
            }
        return caps

    @staticmethod
    def _find_cap(caps: dict[str, dict[str, Any]], keywords: tuple[str, ...]) -> str | None:
        """按关键词（summary/path）找能力名；同类多个时优先带路径占位符的版本
        （文档示例里的字面 ID 路径如 /attempt/123/ 不是真接口）。"""
        fallback: str | None = None
        for name, cap in caps.items():
            spec = cap.get("spec") or {}
            haystack = (str(cap.get("summary") or "") + " " + str(spec.get("path") or "")).lower()
            if not any(k.lower() in haystack for k in keywords):
                continue
            if "{" in str(spec.get("path") or ""):
                return name  # 占位符版本优先：可按真实 exercise_key 替换
            if fallback is None:
                fallback = name
        return fallback

    # ------------------------------------------------------------------
    # Arena 协议（NSSCTF Agent Arena）：单题制取题
    # ------------------------------------------------------------------
    @staticmethod
    def _arena_caps(caps: dict[str, dict[str, Any]]) -> tuple[str, str | None] | None:
        """识别 Arena 协议：GET current（查看当前题目）+ POST next（领取）。

        普适性约束：必须同时存在 next（领取）能力，或接口上下文含 arena——
        否则列表式平台（DASCTF 等）的 GET /current-xxx 会被误判成 Arena 协议。
        """
        current_cap: str | None = None
        next_cap: str | None = None
        saw_arena = False
        for name, cap in caps.items():
            spec = cap.get("spec") or {}
            method = str(spec.get("method") or "GET").upper()
            path = str(spec.get("path") or "").lower()
            hay = path + " " + str(cap.get("summary") or "").lower()
            if "arena" in hay:
                saw_arena = True
            if current_cap is None and method == "GET" and (
                "current" in hay or "当前题目" in hay
            ):
                current_cap = name
            elif next_cap is None and ("next" in hay or "领取" in hay):
                next_cap = name
        if current_cap and (next_cap or saw_arena):
            return (current_cap, next_cap)
        return None

    async def _arena_current(
        self, adapter: PlatformAdapter, current_cap: str, errors: list[str]
    ) -> dict[str, Any] | None:
        res = await adapter.call(current_cap)
        if res.ok and isinstance(res.data, dict):
            attempt = res.data.get("attempt")
            return attempt if isinstance(attempt, dict) else None
        errors.append(f"{current_cap}: {res.error_kind} {res.message}")
        self._log("ERROR", f"拉取当前题目失败：{res.error_kind} — {res.message}", level="error")
        return None

    async def _arena_claim(
        self, adapter: PlatformAdapter, next_cap: str, errors: list[str]
    ) -> dict[str, Any] | None:
        res = await adapter.call(next_cap)
        if res.ok and isinstance(res.data, dict):
            attempt = res.data.get("attempt")
            if isinstance(attempt, dict):
                self._log("SYNC", "当前无进行中题目，已通过 next 接口领取一道", level="success")
                return attempt
            return None
        errors.append(f"{next_cap}: {res.error_kind} {res.message}")
        return None

    async def _arena_exercises(
        self,
        adapter: PlatformAdapter,
        current_cap: str,
        next_cap: str | None,
        errors: list[str],
    ) -> list[dict[str, Any]]:
        """Arena 取题：current →（无进行中/已终结）next 领取；返回待落池 exercise 列表。"""
        attempt = await self._arena_current(adapter, current_cap, errors)
        finished: list[dict[str, Any]] = []
        if attempt is not None and str(attempt.get("state_label") or "").lower() in (
            "solved", "abandoned", "expired", "failed", "timeout"
        ):
            finished.append(attempt)  # 平台侧已终结：只作记录
            attempt = None
        if attempt is None and next_cap:
            attempt = await self._arena_claim(adapter, next_cap, errors)
        out = [self._exercise_from_attempt(a) for a in finished]
        if attempt is not None:
            out.append(self._exercise_from_attempt(attempt))
        return out

    async def _upsert_exercises(self, exercises: list[dict[str, Any]]) -> None:
        """落题目池；不把「进行中/已解」打回「开放」（同步会反复跑）。"""
        for ex in exercises:
            status = ex.get("status") or "open"
            existing = await self.repo.get_exercise(self.competition_id, ex["exerciseKey"])
            if existing and status == "open" and existing.get("status") in ("running", "solved"):
                status = str(existing["status"])
            await self.repo.upsert_exercise(
                self.competition_id,
                exercise_key=ex["exerciseKey"],
                name=ex.get("name") or "",
                category=ex.get("category") or "",
                score=float(ex.get("score") or 0),
                difficulty=str(ex.get("difficulty") or ""),
                status=status,
                flags_got=int(ex.get("flagsGot") or 0),
                flags_total=int(ex.get("flagsTotal") or 1),
                env=ex.get("env") or {},
            )

    @staticmethod
    def _problem_brief(exercise: dict[str, Any]) -> str:
        """把 Arena 题面组装成 Challenge 描述（Solver 的直接输入）。"""
        env = exercise.get("env") or {}
        problem = env.get("problem") if isinstance(env.get("problem"), dict) else {}
        parts = [f"竞赛题目 {exercise.get('exerciseKey')}（{exercise.get('name') or 'Arena'}）"]
        if problem.get("content"):
            parts.append(str(problem["content"])[:4000])
        if problem.get("hint"):
            parts.append(f"提示：{problem['hint']}")
        if env.get("annexUrl"):
            parts.append(f"附件下载：{env['annexUrl']}")
        if env.get("entrypoint"):
            parts.append(f"目标地址：{env['entrypoint']}")
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # 1) 同步
    # ------------------------------------------------------------------
    async def sync(self, adapter: PlatformAdapter | None = None) -> dict[str, Any]:
        """黑板驱动同步：从黑板 protocol 事实找「规则/题目列表/公告」接口并调用。"""
        own = adapter is None
        facts = await self.repo.list_facts(self.competition_id, limit=500)
        caps = self._caps_from_board(facts)
        comp = await self._competition()
        constraints = await self._constraints()
        adapter = adapter or build_adapter(comp, caps, constraints=constraints,
                                           only_enabled=False, runtime=self.runtime)
        result: dict[str, Any] = {"rules": None, "exercises": 0, "notices": 0, "errors": [],
                                  "capabilities": len(caps)}

        if not caps:
            self._log(
                "SYNC",
                "黑板中没有可调用的接口事实——Agent 可用 platform_call 工具直接调用，"
                "或确认文档已解析出接口",
                level="warn",
            )
            if own:
                await adapter.close()
            return result

        rules_cap = self._find_cap(caps, ("规则", "注意事项", "match-info", "rule"))
        listing_cap = self._find_cap(caps, ("题目列表", "赛题", "exercise-list", "list_challenges"))
        notices_cap = self._find_cap(caps, ("公告", "notice", "通知"))
        arena = self._arena_caps(caps)

        try:
            if rules_cap:
                rules = await adapter.call(rules_cap)
                if rules.ok and isinstance(rules.data, dict):
                    note = str(rules.data.get("note") or "")
                    rule = str(rules.data.get("rule") or "")
                    if rule or note:
                        await self.repo.add_fact(
                            self.competition_id, kind="rule",
                            summary="竞赛规则（平台实时拉取）",
                            body=f"{note}\n\n{rule}".strip()[:6000],
                            source="platform", ref="platform:match-info",
                        )
                        self._log("SYNC", "已拉取并更新竞赛规则", level="success")
                    result["rules"] = True
                else:
                    result["errors"].append(f"{rules_cap}: {rules.error_kind} {rules.message}")
                    self._log("ERROR", f"拉取规则失败：{rules.error_kind} — {rules.message}", level="error")

            if listing_cap:
                listing = await adapter.call(listing_cap)
                if listing.ok:
                    exercises = self._flatten_exercises(listing.data)
                    await self._upsert_exercises(exercises)
                    result["exercises"] = len(exercises)
                    solved = sum(1 for e in exercises if e["status"] == "solved")
                    locked = sum(1 for e in exercises if e["status"] == "locked")
                    self._log(
                        "SYNC",
                        f"题目同步完成：共 {len(exercises)} 道（已解 {solved}、未解锁 {locked}）",
                        level="success",
                    )
                else:
                    result["errors"].append(f"{listing_cap}: {listing.error_kind} {listing.message}")
                    self._log("ERROR", f"拉取题目失败：{listing.error_kind} — {listing.message}", level="error")
            elif arena:
                # Arena 协议（NSSCTF Agent Arena）：单题制——current 无进行中题就 next 领取
                exercises = await self._arena_exercises(
                    adapter, arena[0], arena[1], result["errors"]
                )
                await self._upsert_exercises(exercises)
                result["exercises"] = len(exercises)
                active = sum(1 for e in exercises if e["status"] not in ("solved", "locked"))
                self._log(
                    "SYNC",
                    f"题目同步完成（Arena 协议）：记录 {len(exercises)} 道，进行中 {active} 道",
                    level="success",
                )

            if notices_cap:
                notices = await adapter.call(notices_cap)
                if notices.ok and isinstance(notices.data, list) and notices.data:
                    await self.repo.add_fact(
                        self.competition_id, kind="notice",
                        summary=f"平台公告 {len(notices.data)} 条",
                        body=json.dumps(notices.data[:5], ensure_ascii=False, indent=2)[:4000],
                        source="platform", ref="platform:notices",
                    )
                    result["notices"] = len(notices.data)
        finally:
            if own:
                await adapter.close()
        return result

    @staticmethod
    def _exercise_from_attempt(attempt: dict[str, Any]) -> dict[str, Any]:
        """Arena attempt → 扁平 exercise（题目身份脱敏，用 attempt.id 作 key）。"""
        problem = attempt.get("problem") if isinstance(attempt.get("problem"), dict) else {}
        container = problem.get("container") if isinstance(problem.get("container"), dict) else {}
        urls = container.get("url") if isinstance(container.get("url"), list) else []
        annex = problem.get("annex") if isinstance(problem.get("annex"), dict) else {}
        state = str(attempt.get("state_label") or "active").lower()
        env: dict[str, Any] = {"problem": problem, "attemptState": state}
        if urls:
            env["entrypoint"] = str(urls[0])
        if annex.get("url"):
            env["annexUrl"] = str(annex["url"])
        if state == "solved":
            status = "solved"
        elif state in ("abandoned", "expired", "failed", "timeout"):
            status = "locked"
        else:
            status = "open"
        return {
            "exerciseKey": str(attempt.get("id")),
            "name": str(problem.get("type_label") or f"arena-{attempt.get('id')}"),
            "category": str(problem.get("type_label") or ""),
            "group": "arena",
            "score": float(problem.get("rating") or 0),
            "difficulty": "",
            "status": status,
            "flagsGot": 1 if state == "solved" else 0,
            "flagsTotal": 1,
            "env": env,
        }

    @staticmethod
    def _flatten_exercises(data: Any) -> list[dict[str, Any]]:
        """把平台返回的题目结构归一为扁平的 exercise 列表。

        兼容两类形态：
        - DASCTF：``[{name:"Web", corpus:[{id,name,isOpen,hasSolved}]}]``
        - 腾讯 MCP：``{challenges:[{code,title,difficulty,flag_count,flag_got_count,total_score}]}``
        """
        # Arena 协议（NSSCTF）：{attempt:{...}} 或 attempt 对象本身（含 problem）
        if isinstance(data, dict):
            nested = data.get("attempt")
            attempt = nested if isinstance(nested, dict) else (
                data if isinstance(data.get("problem"), dict) else None
            )
            if attempt is not None:
                return [CompetitionEngine._exercise_from_attempt(attempt)]
        out: list[dict[str, Any]] = []
        if isinstance(data, list):
            for group in data:
                if not isinstance(group, dict):
                    continue
                corpus = group.get("corpus")
                if isinstance(corpus, list):
                    for item in corpus:
                        if not isinstance(item, dict):
                            continue
                        out.append({
                            "exerciseKey": str(item.get("id") or item.get("code") or ""),
                            "name": str(item.get("name") or item.get("title") or ""),
                            "category": str(group.get("name") or ""),
                            "group": str(group.get("name") or ""),
                            "score": float(item.get("score") or item.get("total_score") or 0),
                            "difficulty": str(item.get("difficulty") or ""),
                            "status": "solved" if item.get("hasSolved") else
                                      ("locked" if item.get("isOpen") is False else "open"),
                            "flagsGot": int(item.get("flag_got_count") or 0),
                            "flagsTotal": int(item.get("flag_count") or 1),
                        })
                elif group.get("id") is not None:
                    out.append({
                        "exerciseKey": str(group.get("id")),
                        "name": str(group.get("name") or ""),
                        "category": "",
                        "group": "",
                        "score": float(group.get("score") or 0),
                        "difficulty": str(group.get("difficulty") or ""),
                        "status": "solved" if group.get("hasSolved") else "open",
                        "flagsGot": 0, "flagsTotal": 1,
                    })
        elif isinstance(data, dict):
            for item in data.get("challenges") or []:
                if not isinstance(item, dict):
                    continue
                got, total = int(item.get("flag_got_count") or 0), int(item.get("flag_count") or 1)
                out.append({
                    "exerciseKey": str(item.get("code") or ""),
                    "name": str(item.get("title") or ""),
                    "category": "",
                    "group": str(item.get("level") or data.get("current_level") or ""),
                    "score": float(item.get("total_score") or 0),
                    "difficulty": str(item.get("difficulty") or ""),
                    "status": "solved" if got >= total and total > 0 else "open",
                    "flagsGot": got, "flagsTotal": total,
                })
        return [ex for ex in out if ex["exerciseKey"]]

    # ------------------------------------------------------------------
    # 2) 一轮调度
    # ------------------------------------------------------------------
    async def tick(self, adapter: PlatformAdapter | None = None) -> dict[str, Any]:
        own = adapter is None
        facts = await self.repo.list_facts(self.competition_id, limit=500)
        caps = self._caps_from_board(facts)
        adapter = adapter or build_adapter(
            comp=await self._competition(), caps=caps,
            constraints=await self._constraints(), only_enabled=False, runtime=self.runtime,
        )
        strategy = await self._strategy()
        constraints = await self._constraints()
        state = await self.repo.list_state(self.competition_id)
        exercises = await self.repo.list_exercises(self.competition_id)
        pinned = {k.split(":", 1)[1] for k, v in state.items() if k.startswith("pinned:") and v}
        cooldown = {k.split(":", 1)[1] for k, v in state.items()
                    if k.startswith("cooldown:") and v}
        # 派发已达上限的题纳入冷却：它本轮就不会再被选中派发，
        # 否则会一直 picked 又跳过，把新领的题阻塞在调度外
        max_dispatch = int((strategy or {}).get("max_dispatch_per_exercise") or 3)
        for ex in exercises:
            dn = int(await self.repo.get_state(
                self.competition_id, f"dispatch:{ex['exerciseKey']}") or 0)
            if dn >= max_dispatch:
                cooldown.add(ex["exerciseKey"])
        running = sum(1 for ex in exercises if ex["status"] == "running")

        picked = select_next_exercise(
            exercises,
            strategy=strategy,
            pinned=pinned,
            running_instances=running,
            max_instances=constraints.get("max_instances"),
            cooldown=cooldown,
        )
        result: dict[str, Any] = {"picked": picked, "env": None, "dispatched": None,
                                  "submitted": [], "notes": []}
        # SCHEDULE 日志去重：同一题连续选中不重复打（10s 一轮的 tick 会把运行时
        # 日志刷爆）；只在选题变化时记录
        last_pick = await self.repo.get_state(self.competition_id, "last_pick")
        pick_changed = picked != last_pick
        if pick_changed:
            await self.repo.set_state(self.competition_id, "last_pick", picked)
        if not picked:
            # Arena 协议：本地池空且黑板有 current/next 接口时，先领取一道再调度
            arena = self._arena_caps(caps)
            if arena:
                claimed = await self._arena_exercises(adapter, arena[0], arena[1], result["notes"])
                if claimed:
                    await self._upsert_exercises(claimed)
                    exercises = await self.repo.list_exercises(self.competition_id)
                    picked = claimed[-1]["exerciseKey"]
                    self._log("SCHEDULE", f"Arena 领取新题：{claimed[-1].get('name') or picked}")
        if picked:
            target = next((ex for ex in exercises if ex["exerciseKey"] == picked), {})
            reasons: list[str] = []
            if pinned:
                reasons.append("用户置顶优先")
            reasons.append(f"分值 {target.get('score', 0)} 权重 {strategy.get('w_score', 0.35)}")
            reasons.append(f"难度 {target.get('difficulty') or '未知'}（先易后难）")
            if int(target.get("attempts") or 0) == 0:
                reasons.append("尚无尝试记录")
            skipped_locked = sum(1 for ex in exercises if ex["status"] == "locked")
            if skipped_locked:
                reasons.append(f"跳过 {skipped_locked} 道未解锁题目")
            if constraints.get("max_instances"):
                reasons.append(
                    f"实例配额 {running}/{constraints['max_instances']}"
                )
            if pick_changed:
                self._log(
                    "SCHEDULE",
                    f"选中题目 {target.get('name') or picked}，理由：" + "，".join(reasons),
                )
        elif pick_changed:
            self._log("SCHEDULE", "无可用题目：已解/锁定/冷却/尝试超限均已排除，等待同步或人工置顶")

        if picked:
            exercise = await self.repo.get_exercise(self.competition_id, picked)
            assert exercise is not None
            # 3) 环境
            if constraints.get("requires_env_running") or exercise["env"] == {}:
                env = await self._ensure_env(adapter, exercise)
                result["env"] = env
                if env.get("error"):
                    result["notes"].append(f"环境准备失败：{env['error']}")
            # 4) 派发（可选）
            if strategy.get("auto_solve"):
                # 守卫一：session 真的活跃 → 跳过（exercise.status 在 session 终止后
                # 仍是 running，只看它会永远跳过派发）
                # 守卫二：派发次数上限——session 被 dispatcher 终止（EXHAUSTED 等）后
                # 放行重派，但无上限会 10s 一轮无限重派同一题：日志刷爆 + 每轮从
                # bootstrap 重头开始、永远无进展。达上限后等 attempt 过期自动换题。
                dispatch_n = int(
                    await self.repo.get_state(self.competition_id, f"dispatch:{picked}") or 0
                )
                max_dispatch = int((strategy or {}).get("max_dispatch_per_exercise") or 3)
                if exercise.get("challengeId") and await self._session_active(
                    exercise["challengeId"]
                ):
                    result["notes"].append(f"{picked} 已派发过 Solver（session 进行中），跳过重复派发")
                elif dispatch_n >= max_dispatch:
                    result["notes"].append(
                        f"{picked} 自动派发已达上限（{max_dispatch} 次），"
                        "等待 attempt 过期或人工重置"
                    )
                    if pick_changed:
                        self._log(
                            "SCHEDULE",
                            f"{picked} 派发次数达上限，暂停自动派发（attempt 到期自动换题）",
                            level="warn",
                        )
                else:
                    result["dispatched"] = await self._dispatch(exercise)

        # 5) 收集并提交
        result["submitted"] = await self._submit_pending(adapter, strategy)
        self.last_tick = result
        if own:
            await adapter.close()
        return result

    async def _ensure_env(self, adapter: PlatformAdapter, exercise: dict[str, Any]) -> dict[str, Any]:
        key = exercise["exerciseKey"]
        if exercise.get("env"):
            return {"ok": True, "cached": True, "env": exercise["env"]}
        self._log("PREPARE_ENV", f"启动环境：{exercise['name'] or key}（build_env）")
        res = await adapter.call("build_env", exercise_key=key)
        if not res.ok:
            await self.repo.add_fact(
                self.competition_id, kind="feedback",
                summary=f"环境启动失败：{key}",
                body=f"{res.error_kind}: {res.message}", source="platform",
                ref=f"exercise:{key}",
            )
            self._log(
                "ERROR", f"环境启动失败：{key} — {res.error_kind}: {res.message}", level="error"
            )
            return {"ok": False, "error": f"{res.error_kind}: {res.message}"}
        env = res.data if isinstance(res.data, dict) else {"entrypoint": str(res.data or "")}
        # REST 平台：启动是异步的，需要轮询详情直到 isNeedCheck=false
        poll = ((exercise.get("env") or {}).get("poll") or {})
        if poll or not env.get("entrypoint"):
            for _ in range(10):
                detail = await adapter.call("get_exercise", exercise_key=key)
                if detail.ok and isinstance(detail.data, dict):
                    data = detail.data
                    ready = not bool(data.get(poll.get("detail_field", "isNeedCheck"))) if poll else True
                    endpoints = data.get(poll.get("endpoints_field", "endpoints")) if poll else None
                    if ready and (endpoints or data.get("entrypoint")):
                        env = {**data}
                        break
                await asyncio.sleep(2.0)
        await self.repo.upsert_exercise(
            self.competition_id, exercise_key=key, name=exercise["name"],
            category=exercise["category"], score=exercise["score"],
            difficulty=exercise["difficulty"], status="running",
            flags_got=exercise["flagsGot"], flags_total=exercise["flagsTotal"], env=env,
        )
        await self.repo.add_fact(
            self.competition_id, kind="feedback", summary=f"环境就绪：{key}",
            body=json.dumps(env, ensure_ascii=False)[:2000], source="platform",
            ref=f"exercise:{key}",
        )
        self._log(
            "PREPARE_ENV",
            f"环境就绪：{key}（{list(env.keys())[:3]}）",
            level="success",
        )
        return {"ok": True, "env": env}

    async def _session_active(self, challenge_id: str) -> bool:
        """该 challenge 最近一个 session 是否仍在进行（决定能否跳过重复派发）。"""
        row = await self.runtime.repos.db.fetchone(
            "SELECT status FROM challenge_sessions WHERE challenge_id=? "
            "ORDER BY started_at DESC LIMIT 1",
            (challenge_id,),
        )
        return bool(row) and str(row["status"] or "") in ("running", "pending", "active")

    async def _dispatch(self, exercise: dict[str, Any]) -> dict[str, Any]:
        """把题交给现有 Dispatcher（auto_solve=True 时）。"""
        key = exercise["exerciseKey"]
        try:
            challenge_id = exercise.get("challengeId")
            if not challenge_id:
                challenge = await self.runtime.repos.challenges.upsert({
                    "name": f"[{key}] {exercise['name'] or 'competition'}",
                    "category": exercise["category"] or "misc",
                    "description": self._problem_brief(exercise),
                    "target": str((exercise.get("env") or {}).get("entrypoint")
                                  or ",".join((exercise.get("env") or {}).get("exposeIps") or [])),
                    "meta": {"source": "competition", "competitionId": self.competition_id,
                             "exerciseKey": key},
                })
                challenge_id = challenge["id"]
                await self.repo.bind_challenge(self.competition_id, key, challenge_id)
            session = await self.runtime.start_challenge_run(challenge_id)
            await self.repo.upsert_exercise(
                self.competition_id, exercise_key=key, name=exercise["name"],
                category=exercise["category"], score=exercise["score"],
                difficulty=exercise["difficulty"], status="running",
                flags_got=exercise["flagsGot"], flags_total=exercise["flagsTotal"],
                env=exercise["env"],
            )
            # 派发计数：同一 exercise 的重派次数上限据此判定（防无限循环）
            n = int(await self.repo.get_state(self.competition_id, f"dispatch:{key}") or 0)
            await self.repo.set_state(self.competition_id, f"dispatch:{key}", n + 1)
            self._log(
                "DISPATCH",
                f"已派发 Solver：{key} → challenge {challenge_id}（auto_solve=true）",
            )
            return {"ok": True, "challengeId": challenge_id, "runId": getattr(session, "run_id", None)}
        except Exception as exc:  # noqa: BLE001 - 派发失败不中断引擎
            logger.warning("竞赛派发失败 %s: %s", key, exc)
            self._log("ERROR", f"派发失败：{key} — {exc}", level="error")
            await self.repo.add_fact(
                self.competition_id, kind="feedback", summary=f"派发失败：{key}",
                body=f"{type(exc).__name__}: {exc}", source="system", ref=f"exercise:{key}",
            )
            return {"ok": False, "error": str(exc)}

    async def _submit_pending(
        self, adapter: PlatformAdapter, strategy: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """把本地已捕获但未提交的 flag 交给平台。"""
        facts = await self.repo.list_facts(self.competition_id, limit=500)
        caps = self._caps_from_board(facts)
        submit_cap = self._find_cap(caps, ("提交", "answer", "submit_flag", "submit"))
        if not submit_cap:
            self._log(
                "SUBMIT",
                "黑板中未识别到提交接口：请确认文档含「提交答案」接口，或用 platform_call 手动提交",
                level="warn",
            )
            return []
        # 语义修正：solver 本地捕获 flag 时 validated 即置 1（本地校验过），
        # 旧逻辑只捞 validated=0 → 真 flag 永远不提交平台。
        # 现按「平台侧无终态提交记录」捞取：correct/invalid/duplicate 都算已处理，
        # error/rate_limited（暂时性故障）允许重试。
        rows = await self.runtime.repos.db.fetchall(
            "SELECT f.id, f.value, f.challenge_id FROM flags f "
            "WHERE f.challenge_id IS NOT NULL "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM submissions s WHERE s.competition_id=? AND s.flag=f.value "
            "  AND s.verdict IN ('correct', 'invalid', 'duplicate')"
            ") ORDER BY f.submitted_at LIMIT 10",
            (self.competition_id,),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            bound = await self.runtime.repos.db.fetchall(
                "SELECT exercise_key FROM platform_exercises WHERE competition_id=? AND challenge_id=?",
                (self.competition_id, row["challenge_id"]),
            )
            if not bound:
                continue
            key = str(bound[0]["exercise_key"])
            exercise = await self.repo.get_exercise(self.competition_id, key)
            if exercise is None:
                continue
            submitted = {s["flag"] for s in await self.repo.list_submissions(self.competition_id)}
            wrong = sum(1 for s in await self.repo.list_submissions(self.competition_id)
                        if s["exerciseKey"] == key and s["verdict"] in ("invalid", "wrong"))
            decision = decide_submission(
                exercise=exercise, flag=str(row["value"]), already_submitted=submitted,
                wrong_count=wrong, strategy=strategy,
            )
            if not decision.allow:
                if decision.cooldown_minutes:
                    await self.repo.set_state(
                        self.competition_id, f"cooldown:{key}", True
                    )
                out.append({"exerciseKey": key, "allowed": False, "reason": decision.reason})
                continue

            self._log("SUBMIT", f"提交 flag：{key}（{len(str(row['value']))} 字符，已脱敏记录）")
            res = await adapter.call(submit_cap, exercise_key=key, flag=str(row["value"]))
            # error_kind → submissions.verdict 词汇表（M5 定义）
            _VERDICT_MAP = {"invalid_flag": "invalid", "duplicate": "duplicate",
                            "rate_limited": "rate_limited", "auth_error": "error"}
            ok, kind = res.ok, res.error_kind
            if ok and isinstance(res.data, dict):
                # 通用裁决兜底：平台在 data 里返回 correct/is_correct=false 即答案错误
                for field_name in ("correct", "is_correct", "passed", "accepted"):
                    if res.data.get(field_name) is False:
                        ok, kind = False, "invalid_flag"
                        break
            verdict = "correct" if ok else _VERDICT_MAP.get(kind, "error")
            await self.repo.add_submission(
                self.competition_id, exercise_key=key, flag=str(row["value"]),
                verdict=verdict, platform_msg=res.message,
            )
            await self.runtime.repos.db.execute(
                "UPDATE flags SET validated=? WHERE id=?", (1 if ok else 0, row["id"])
            )
            if ok:
                got = exercise["flagsGot"] + 1
                total = exercise["flagsTotal"]
                await self.repo.upsert_exercise(
                    self.competition_id, exercise_key=key, name=exercise["name"],
                    category=exercise["category"], score=exercise["score"],
                    difficulty=exercise["difficulty"],
                    status="solved" if got >= total else "running",
                    flags_got=got, flags_total=total, env=exercise["env"],
                )
                self._log(
                    "FEEDBACK",
                    f"提交成功：{key}（{got}/{total}）— {str(res.message or '')[:80]}",
                    level="success",
                )
                await self.repo.add_fact(
                    self.competition_id, kind="feedback",
                    summary=f"提交成功：{key}（{got}/{total}）",
                    body=str(res.message or "")[:1000], source="platform", ref=f"exercise:{key}",
                )
            else:
                await self.repo.bump_attempt(self.competition_id, key)
                backoff = backoff_for(res.error_kind, exercise["attempts"] + 1)
                self._log(
                    "FEEDBACK",
                    f"提交未通过：{key}（{verdict}）— {res.message[:80]}；"
                    f"退避 {backoff.seconds:.0f}s（{backoff.reason}）",
                    level="warn",
                )
                await self.repo.add_fact(
                    self.competition_id, kind="feedback", summary=f"提交失败：{key}（{verdict}）",
                    body=f"{res.error_kind}: {res.message}\n退避 {backoff.seconds:.0f}s（{backoff.reason}）",
                    source="platform", ref=f"exercise:{key}",
                )
                if kind == "auth_error":
                    await self._pause_for_auth()
            out.append({"exerciseKey": key, "allowed": True, "ok": ok, "verdict": verdict})
        return out

    async def _pause_for_auth(self) -> None:
        self._log("ERROR", "鉴权失败：竞赛自动暂停（请检查 auth_ref 指向的凭证）", level="error")
        await self.repo.update(self.competition_id, status="error")
        await self.repo.add_fact(
            self.competition_id, kind="feedback", summary="鉴权失败：竞赛已自动暂停",
            body="平台返回鉴权错误（无效 Token / 队伍禁用）。请检查 auth_ref 指向的凭证后重启。",
            source="system", ref="system:auth",
        )
        self._stopping = True

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> dict[str, Any]:
        if self.running:
            return {"running": True, "already": True}
        await self.repo.update(self.competition_id, status="running")
        self._stopping = False
        self._task = asyncio.create_task(self._loop())
        return {"running": True}

    async def pause(self) -> dict[str, Any]:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            self._task = None
        await self.repo.update(self.competition_id, status="paused")
        return {"running": False}

    async def _loop(self) -> None:
        adapter: PlatformAdapter | None = None
        try:
            adapter = await self.adapter()
            while not self._stopping:
                try:
                    await self.sync(adapter)
                    await self.tick(adapter)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - 单轮失败不影响后续
                    logger.exception("竞赛引擎 tick 失败")
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            raise
        finally:
            if adapter is not None:
                await adapter.close()
