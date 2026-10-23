"""前端 Steering 静态接线断言（无 Node 构建链，按项目惯例做静态检查）。

验证 web/ 前端包含统一入口、排队角标、队列计数与刷新恢复的关键接线点。
"""

from __future__ import annotations

from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"


def read(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def test_app_js_uses_unified_message_entry():
    app = read("js/app.js")
    # 运行中不再禁用发送（输入框常开）
    assert "refs.btnSend.disabled = false" in app
    # 运行中发送走引导分支（入队），空闲走任务启动
    assert "sendSteerMessage" in app
    assert "sendConversationMessage" in app
    # 不再直接消费 per-run POST SSE 作为唯一通道
    assert "streamConversationEvents" in app


def test_app_js_handles_steering_events():
    app = read("js/app.js")
    for event in ("message_queued", "message_delivered", "task_done", "task_error"):
        assert f"{event}: function" in app, f"缺少 {event} 事件处理"


def test_app_js_filters_stale_run_events():
    """会话级流是长驻的：断点补发可能重放旧任务事件，必须按 runId 过滤。"""
    app = read("js/app.js")
    assert "data.runId !== state.runId" in app


def test_timeline_js_renders_steering_badges():
    timeline = read("js/timeline.js")
    assert "steer-badge" in timeline
    assert "markSteerStatus" in timeline
    # 历史渲染需区分未投递（queued）消息
    assert "deliveryStatus" in timeline


def test_index_html_has_queue_hint():
    html = read("index.html")
    assert 'id="queue-hint"' in html


def test_css_styles_steering_badges_without_hardcoded_colors():
    css = read("css/style.css")
    assert ".steer-badge" in css
    assert ".queue-hint" in css
    # 项目设计系统约束：全站组件禁止硬编码色值（README §14.1）
    import re

    for line in css.splitlines():
        if "steer-badge" in line or "queue-hint" in line:
            assert not re.search(r"#[0-9a-fA-F]{3,8}\b", line), f"硬编码色值：{line}"


def test_api_js_exposes_steering_endpoints():
    api = read("js/api.js")
    for endpoint in ("/messages", "/state", "/cancel"):
        assert f"'/api/conversations/' + encodeURIComponent(id) + '{endpoint}'" in api
    # 会话级事件流（GET）：URL + Last-Event-ID 断点补发
    assert "encodeURIComponent(conversationId) + '/events'" in api
    assert "Last-Event-ID" in api
    assert "streamConversationEvents" in api
