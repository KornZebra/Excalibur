"""M4 reason 图指纹回归测试（同步风格）。

覆盖：指纹相同抑制 reason / 三类态势变化触发 / 无 checkpoint 触发 / 开关退回旧逻辑。
"""

from __future__ import annotations

from pathlib import Path

from ctfagent.config import AppConfig
from ctfagent.orchestrator.policy import ChallengeSnapshot, Policy, SpawnReason, Wait

REPO_ROOT = Path(__file__).resolve().parents[1]


def _policy(*, fingerprint: bool = True) -> Policy:
    config = AppConfig(db_path=":memory:", project_root=REPO_ROOT)
    config = config.model_copy(
        update={
            "dispatcher": config.dispatcher.model_copy(
                update={"reason_by_fingerprint": fingerprint}
            )
        }
    )
    return Policy(config)


def _snap(**kw) -> ChallengeSnapshot:
    base = dict(
        challenge_id="ch1",
        facts_count=3,
        bootstraps_done=1,   # 已 bootstrap 过，否则会先命中 bootstrap 分支
        memories_count=3,
        hints_count=0,
        running_count=0,
        flag_verified=False,
        since_last_spawn_seconds=None,
    )
    base.update(kw)
    return ChallengeSnapshot(**base)


def test_same_fingerprint_suppresses_reason() -> None:
    snap = _snap(reason_fingerprint=(3, 0, False))
    action = _policy().decide(snap)
    assert isinstance(action, Wait)


def test_new_memory_triggers_reason() -> None:
    snap = _snap(memories_count=4, reason_fingerprint=(3, 0, False))
    action = _policy().decide(snap)
    assert isinstance(action, SpawnReason)


def test_new_hint_triggers_reason() -> None:
    snap = _snap(hints_count=1, reason_fingerprint=(3, 0, False))
    action = _policy().decide(snap)
    assert isinstance(action, SpawnReason)


def test_open_ideas_closed_triggers_reason() -> None:
    snap = _snap(reason_fingerprint=(3, 0, True))
    action = _policy().decide(snap)
    assert isinstance(action, SpawnReason)


def test_no_checkpoint_triggers_reason() -> None:
    snap = _snap(reason_fingerprint=None)
    action = _policy().decide(snap)
    assert isinstance(action, SpawnReason)


def test_switch_off_uses_legacy_heuristic() -> None:
    # 指纹相同但开关关闭 → 退回 last_task_type 判定
    same_fp = _snap(reason_fingerprint=(3, 0, False), last_task_type="explore")
    assert isinstance(_policy(fingerprint=False).decide(same_fp), SpawnReason)

    after_reason = _snap(reason_fingerprint=(3, 0, False), last_task_type="reason")
    assert isinstance(_policy(fingerprint=False).decide(after_reason), Wait)
