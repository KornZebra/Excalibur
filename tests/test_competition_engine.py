"""M8/S3 竞赛引擎回归测试（黑板事实驱动，FakeAdapter + 临时 DB）。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from ctfagent.competition.adapters import NormalizedResult
from ctfagent.competition.engine import CompetitionEngine
from ctfagent.competition.strategy import (
    DEFAULT_STRATEGY,
    backoff_for,
    decide_submission,
    select_next_exercise,
)
from ctfagent.storage.db import Database
from ctfagent.storage.repos import Repos


def _ex(key: str, **kw) -> dict:
    base = {"exerciseKey": key, "name": key, "category": "web", "score": 100.0,
            "difficulty": "medium", "status": "open", "flagsGot": 0, "flagsTotal": 1,
            "attempts": 0, "env": {}}
    base.update(kw)
    return base


def test_pin_wins_over_scoring() -> None:
    exercises = [_ex("1", score=500), _ex("2", score=10)]
    assert select_next_exercise(exercises) == "1"
    assert select_next_exercise(exercises, pinned={"2"}) == "2"


def test_hard_constraints_filter() -> None:
    exercises = [
        _ex("solved", status="solved"),
        _ex("locked", status="locked"),
        _ex("done", flagsGot=1, flagsTotal=1),
        _ex("exhausted", attempts=5),
        _ex("cool", score=999),
        _ex("ok"),
    ]
    picked = select_next_exercise(exercises, cooldown={"cool"})
    assert picked == "ok"


def test_difficulty_prefers_easy_when_scores_equal() -> None:
    exercises = [_ex("hard", difficulty="hard"), _ex("easy", difficulty="easy")]
    assert select_next_exercise(exercises) == "easy"


def test_instance_quota_limits_new_env() -> None:
    exercises = [_ex("a", status="open"), _ex("b", status="running")]
    picked = select_next_exercise(exercises, running_instances=3, max_instances=3)
    assert picked == "b"


def test_submission_decisions() -> None:
    ex = _ex("1")
    assert decide_submission(exercise=ex, flag="", already_submitted=set()).allow is False
    assert decide_submission(exercise=ex, flag="x" * 300, already_submitted=set()).allow is False
    assert decide_submission(exercise=ex, flag="flag{a}", already_submitted={"flag{a}"}).mark_solved is True
    cooled = decide_submission(exercise=ex, flag="flag{b}", already_submitted=set(), wrong_count=3)
    assert cooled.allow is False and cooled.cooldown_minutes == 10
    ok = decide_submission(exercise=ex, flag="flag{c}", already_submitted=set())
    assert ok.allow is True

    strict = {**DEFAULT_STRATEGY, "flag_regex": r"^flag\{.*\}$"}
    bad = decide_submission(exercise=ex, flag="not-a-flag", already_submitted=set(), strategy=strict)
    assert bad.allow is False and "格式" in bad.reason


def test_backoff_matrix() -> None:
    assert backoff_for("auth_error", 1).seconds == 0.0
    assert backoff_for("invalid_flag", 1).seconds == 0.0
    assert backoff_for("rate_limited", 1).seconds == 2.0
    assert backoff_for("rate_limited", 3).seconds == 8.0
    assert backoff_for("platform_down", 10).seconds == 60.0


# ---------------------------------------------------------------------------
# 引擎（黑板事实驱动）
# ---------------------------------------------------------------------------
class FakeAdapter:
    def __init__(self, responses: dict[str, NormalizedResult]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    async def call(self, capability: str, **kwargs) -> NormalizedResult:
        self.calls.append((capability, kwargs))
        return self.responses.get(capability, NormalizedResult(False, error_kind="unsupported"))

    async def close(self) -> None:
        return None


def _runtime(repos: Repos) -> SimpleNamespace:
    return SimpleNamespace(repos=repos)


def test_sync_flattens_dasctf_listing(tmp_path: Path) -> None:
    async def _inner() -> None:
        db = Database(tmp_path / "e1.db")
        await db.connect()
        try:
            repos = Repos(db)
            comp = await repos.competitions.create(name="赛", base_url="https://x")
            # 黑板事实：接口条目（body 含 方法/路径）
            fact = await repos.competitions.add_fact(
                comp["id"], kind="protocol",
                summary="GET /ctf/exercise-list — 查询题目列表",
                body="方法: GET\n路径: /ctf/exercise-list\n说明: 查询题目列表",
                source="doc", ref="接口总览",
            )
            await repos.competitions.add_fact(
                comp["id"], kind="rule", summary="竞赛规则正文", body="规则...", source="doc",
            )
            adapter = FakeAdapter({
                "proto:" + fact["id"][:24]: NormalizedResult(True, data=[
                    {"id": 10, "name": "Web", "corpus": [
                        {"id": 1001, "name": "easy-web", "isOpen": True, "hasSolved": False},
                        {"id": 1002, "name": "hard-web", "isOpen": False, "hasSolved": False},
                    ]},
                ]),
            })
            engine = CompetitionEngine(_runtime(repos), comp["id"])
            res = await engine.sync(adapter)
            assert res["exercises"] == 2
            items = await repos.competitions.list_exercises(comp["id"])
            by_key = {i["exerciseKey"]: i for i in items}
            assert by_key["1001"]["status"] == "open"
            assert by_key["1002"]["status"] == "locked"
            rule_facts = await repos.competitions.list_facts(comp["id"], kinds=["rule"])
            assert any("竞赛规则正文" in f["summary"] for f in rule_facts)
        finally:
            await db.close()

    asyncio.run(_inner())


def test_tick_submits_pending_flag(tmp_path: Path) -> None:
    async def _inner() -> None:
        db = Database(tmp_path / "e2.db")
        await db.connect()
        try:
            repos = Repos(db)
            comp = await repos.competitions.create(name="赛", base_url="https://x")
            # 黑板：提交接口事实
            await repos.competitions.add_fact(
                comp["id"], kind="protocol",
                summary="POST /answer-panel/answer — 提交答案",
                body="方法: POST\n路径: /answer-panel/answer\n说明: 提交答案",
                source="doc",
            )
            fact = await repos.competitions.add_fact(
                comp["id"], kind="protocol",
                summary="POST /answer-panel/answer — 提交答案",
                body="方法: POST\n路径: /answer-panel/answer", source="doc",
            )
            cap_name = "proto:" + fact["id"][:24]
            await repos.competitions.upsert_exercise(
                comp["id"], exercise_key="1001", name="easy-web", score=100, flags_total=1,
                env={"entrypoint": "http://target:80"},   # 预置环境：tick 跳过 build_env
            )
            challenge = await repos.challenges.upsert({"name": "t", "category": "web"})
            await repos.competitions.bind_challenge(comp["id"], "1001", challenge["id"])
            await repos.flags.add(challenge_id=challenge["id"], value="flag{ok}", source="test")

            adapter = FakeAdapter({
                cap_name: NormalizedResult(True, data={"isCorrect": True}, message="恭喜！获得100分"),
            })
            engine = CompetitionEngine(_runtime(repos), comp["id"])
            res = await engine.tick(adapter)
            submitted = [s for s in res["submitted"] if s.get("ok")]
            assert len(submitted) == 1
            ex = await repos.competitions.get_exercise(comp["id"], "1001")
            assert ex["status"] == "solved"
            assert ex["flagsGot"] == 1
            subs = await repos.competitions.list_submissions(comp["id"])
            assert subs and subs[0]["verdict"] == "correct"
        finally:
            await db.close()

    asyncio.run(_inner())


def test_tick_wrong_flag_marks_attempt(tmp_path: Path) -> None:
    async def _inner() -> None:
        db = Database(tmp_path / "e3.db")
        await db.connect()
        try:
            repos = Repos(db)
            comp = await repos.competitions.create(name="赛", base_url="https://x")
            await repos.competitions.add_fact(
                comp["id"], kind="protocol", summary="POST /answer-panel/answer — 提交答案",
                body="方法: POST\n路径: /answer-panel/answer", source="doc",
            )
            fact = await repos.competitions.add_fact(
                comp["id"], kind="protocol", summary="POST /answer-panel/answer — 提交答案",
                body="方法: POST\n路径: /answer-panel/answer", source="doc",
            )
            cap_name = "proto:" + fact["id"][:24]
            await repos.competitions.upsert_exercise(
                comp["id"], exercise_key="2001", name="w", score=50, flags_total=1,
                env={"entrypoint": "http://target:80"},
            )
            challenge = await repos.challenges.upsert({"name": "t2", "category": "web"})
            await repos.competitions.bind_challenge(comp["id"], "2001", challenge["id"])
            await repos.flags.add(challenge_id=challenge["id"], value="flag{bad}", source="test")

            adapter = FakeAdapter({
                cap_name: NormalizedResult(False, error_kind="invalid_flag", message="答案错误"),
            })
            engine = CompetitionEngine(_runtime(repos), comp["id"])
            res = await engine.tick(adapter)
            assert res["submitted"][0]["verdict"] == "invalid"
            ex = await repos.competitions.get_exercise(comp["id"], "2001")
            assert ex["attempts"] == 1
            assert ex["status"] != "solved"
        finally:
            await db.close()

    asyncio.run(_inner())
