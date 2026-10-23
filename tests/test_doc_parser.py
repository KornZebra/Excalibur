"""M6 文档解析器回归测试（同步风格，全部离线）。"""

from __future__ import annotations

from ctfagent.competition.doc_parser import (
    extract_spec_heuristic,
    split_sections,
    validate_spec,
)

DASCTF_DOC = """
## 公共约定

### Base URL

```text
https://pro.dasctf.com/slab-match/api/v1/agent
```

### 认证 Header

| Header | 必填 | 说明 |
| --- | --- | --- |
| `X-Agent-AccessKey` | 是 | Agent 专用 AccessKey |

## 接口总览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/slab-match/api/v1/agent/match/notice/match-info` | 查询竞赛注意事项和竞赛规则 |
| `GET` | `/slab-match/api/v1/agent/answer-panel/overview` | 查询得分与排名 |
| `POST` | `/slab-match/api/v1/agent/answer-panel/answer` | 提交答案 |
| `GET` | `/slab-match/api/v1/agent/ctf/exercise-list` | 查询题目列表 |
| `GET` | `/slab-match/api/v1/agent/ctf/exercise` | 查询题目详情 |
| `POST` | `/slab-match/api/v1/agent/ctf/build-exercise-env` | 启动题目环境 |
| `POST` | `/slab-match/api/v1/agent/ctf/recover-exercise-env` | 回收题目环境 |
| `GET` | `/slab-match/api/v1/agent/match/notice/now-list` | 查询公告列表 |

提交答案响应 data：`{"isCorrect": true}`；列表响应含 `corpus` 分组；
启动环境为异步，需轮询详情直到 `isNeedCheck=false` 且 `endpoints` 可用；
`expireTime` 为靶机过期时间。
"""

MCP_DOC = """
MCP Server 提供 5 个核心工具，transport 为 streamable_http。

| 工具名 | 作用 | 参数 |
| --- | --- | --- |
| `list_challenges` | 获取赛题列表 | 无 |
| `start_challenge` | 启动赛题容器实例 | code |
| `stop_challenge` | 停止赛题实例 | code |
| `submit_flag` | 提交 Flag | code, flag |
| `view_hint` | 查看提示 | code |

鉴权：`Authorization: Bearer <YOUR_AGENT_TOKEN>`。
每队最多同时运行 3 个实例；每秒最多调用 3 次；首次查看提示会扣除该题总分的 10%。
"""


def test_split_sections() -> None:
    sections = split_sections(DASCTF_DOC)
    titles = [s.title for s in sections if s.title]
    assert "Base URL" in titles
    assert "接口总览" in titles


def test_extract_dasctf_spec() -> None:
    spec = extract_spec_heuristic(DASCTF_DOC)
    assert spec.base_url == "https://pro.dasctf.com/slab-match/api/v1/agent"
    assert spec.auth_header == "X-Agent-AccessKey"
    names = set(spec.capability_names())
    for expected in ("get_rules", "get_scoreboard", "submit_flag", "list_exercises",
                     "get_exercise", "build_env", "recover_env", "get_notices"):
        assert expected in names, f"缺少 {expected}：{sorted(names)}"
    assert validate_spec(spec) == []

    submit = next(c for c in spec.capabilities if c["name"] == "submit_flag")
    assert submit["spec"]["method"] == "POST"
    assert submit["spec"]["verdict"]["field"] == "isCorrect"

    build = next(c for c in spec.capabilities if c["name"] == "build_env")
    assert build["spec"]["poll"]["detail_field"] == "isNeedCheck"

    listing = next(c for c in spec.capabilities if c["name"] == "list_exercises")
    assert listing["spec"]["response"]["item"]["key"] == "corpus[].id"

    assert spec.constraints.get("env_expire_field") == "expireTime"


def test_extract_mcp_spec() -> None:
    spec = extract_spec_heuristic(MCP_DOC)
    assert spec.platform_kind == "tencent_mcp"
    assert spec.auth_kind == "bearer"
    assert spec.auth_header == "Authorization"
    names = set(spec.capability_names())
    assert {"list_exercises", "build_env", "recover_env", "submit_flag", "view_hint"} <= names
    submit = next(c for c in spec.capabilities if c["name"] == "submit_flag")
    assert submit["transport"] == "mcp"
    assert submit["spec"]["tool"] == "submit_flag"
    assert spec.constraints.get("max_instances") == 3
    assert spec.constraints.get("rate_limit_per_sec") == 3
    assert abs(float(spec.constraints.get("hint_penalty_ratio")) - 0.1) < 1e-9


def test_validate_rejects_missing_required() -> None:
    spec = extract_spec_heuristic("## 随便\n\n没有接口表")
    errors = validate_spec(spec)
    assert any("list_exercises" in e for e in errors)
    assert any("submit_flag" in e for e in errors)
