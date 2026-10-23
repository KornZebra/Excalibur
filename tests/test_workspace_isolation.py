"""工作区隔离（U17）的回归测试。

历史故障：同一项目下新建会话、上传附件解题时，会话未绑定题目 → 运行期工作区被解析为
**共享的** ``workspace/challenge/_default``。该目录积累了其它题目的附件、解题脚本与
``writeup.md``（内含已解出的 flag），模型按提示词探索「自己的工作区」时读到旧题数据，
于是直接给出之前题目的答案（串题）。

修复：未绑定题目的会话使用**会话私有**工作区
``workspace/conversations/<cid>/work``，并在路径解析层禁止跨题目/跨会话访问。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ctfagent.config import AppConfig
from ctfagent.tools.builtin.paths import PathScopeError, resolve_path
from ctfagent.tools.context import ToolContext


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig()
    config.project_root = tmp_path
    config.agent.workspace_root = str(tmp_path / "workspace")
    return config


def _ctx(config: AppConfig, *, conversation_id: str, challenge_id: str | None = None, workspace: Path | None = None):
    return ToolContext(
        config=config,
        repos=None,  # type: ignore[arg-type] - 路径解析不需要仓储
        registry=None,  # type: ignore[arg-type]
        execution_service=None,  # type: ignore[arg-type]
        conversation_id=conversation_id,
        challenge_id=challenge_id,
        workspace=workspace,
    )


# ---------------------------------------------------------------------------
# 工作区解析
# ---------------------------------------------------------------------------
def test_unbound_conversation_gets_private_workspace(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = config.run_workspace(None, "conv_new")
    assert path == config.workspace_root / "conversations" / "conv_new" / "work"
    assert "_default" not in str(path)


def test_bound_challenge_keeps_challenge_workspace(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = config.run_workspace("chal_abc", "conv_new")
    assert path == config.workspace_root / "challenge" / "chal_abc"


def test_tool_context_workspace_matches_run_workspace(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ctx = _ctx(config, conversation_id="conv_new")
    assert ctx.resolved_workspace() == config.run_workspace(None, "conv_new")


# ---------------------------------------------------------------------------
# 跨题 / 跨会话访问必须被拒绝
# ---------------------------------------------------------------------------
def test_unbound_conversation_cannot_read_default_workspace(tmp_path: Path) -> None:
    """核心回归：新会话不得读取 _default 里的旧 writeup（旧 flag 就藏在这里）。"""
    config = _config(tmp_path)
    legacy = config.workspace_root / "challenge" / "_default"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "writeup.md").write_text("flag{old_answer}", encoding="utf-8")

    ctx = _ctx(config, conversation_id="conv_new")
    with pytest.raises(PathScopeError) as exc:
        resolve_path("workspace/challenge/_default/writeup.md", ctx=ctx)
    assert "未绑定" in str(exc.value) or "越界" in str(exc.value)


def test_bound_challenge_cannot_read_other_challenge(tmp_path: Path) -> None:
    config = _config(tmp_path)
    other = config.workspace_root / "challenge" / "chal_other"
    other.mkdir(parents=True, exist_ok=True)
    (other / "flag.txt").write_text("flag{x}", encoding="utf-8")

    ctx = _ctx(config, conversation_id="conv_a", challenge_id="chal_mine")
    with pytest.raises(PathScopeError):
        resolve_path("workspace/challenge/chal_other/flag.txt", ctx=ctx)


def test_challenge_can_read_own_workspace(tmp_path: Path) -> None:
    config = _config(tmp_path)
    own = config.workspace_root / "challenge" / "chal_mine"
    own.mkdir(parents=True, exist_ok=True)
    (own / "note.txt").write_text("ok", encoding="utf-8")

    ctx = _ctx(config, conversation_id="conv_a", challenge_id="chal_mine")
    assert resolve_path("workspace/challenge/chal_mine/note.txt", ctx=ctx).read_text() == "ok"


def test_conversation_cannot_read_other_conversation_attachments(tmp_path: Path) -> None:
    config = _config(tmp_path)
    other = config.workspace_root / "conversations" / "conv_other" / "attachments"
    other.mkdir(parents=True, exist_ok=True)
    (other / "a.zip").write_text("x", encoding="utf-8")

    ctx = _ctx(config, conversation_id="conv_new")
    with pytest.raises(PathScopeError):
        resolve_path("workspace/conversations/conv_other/attachments/a.zip", ctx=ctx)


def test_own_attachments_are_readable(tmp_path: Path) -> None:
    config = _config(tmp_path)
    own = config.workspace_root / "conversations" / "conv_new" / "attachments"
    own.mkdir(parents=True, exist_ok=True)
    (own / "a.zip").write_text("x", encoding="utf-8")

    ctx = _ctx(config, conversation_id="conv_new")
    assert resolve_path("workspace/conversations/conv_new/attachments/a.zip", ctx=ctx).exists()


def test_sub_agent_inheriting_parent_workspace_can_read_parent_attachments(tmp_path: Path) -> None:
    """子代理工作区继承父会话时，父会话的附件必须可读（否则子代理看不到题目附件）。"""
    config = _config(tmp_path)
    parent_attachments = config.workspace_root / "conversations" / "conv_parent" / "attachments"
    parent_attachments.mkdir(parents=True, exist_ok=True)
    (parent_attachments / "p.bin").write_text("x", encoding="utf-8")
    parent_work = config.run_workspace(None, "conv_parent")

    child = _ctx(config, conversation_id="conv_child", workspace=parent_work)
    assert resolve_path(
        "workspace/conversations/conv_parent/attachments/p.bin", ctx=child
    ).exists()


def test_relative_paths_land_in_private_workspace(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ctx = _ctx(config, conversation_id="conv_new")
    target = resolve_path("solve.py", ctx=ctx, for_write=True)
    assert target == config.workspace_root / "conversations" / "conv_new" / "work" / "solve.py"
