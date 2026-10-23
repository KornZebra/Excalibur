# CTF-Agent 运行镜像

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    CTF_AGENT_HOME=/app

WORKDIR /app

# 常用侦察工具（可选）：即使缺失，工具注册表也会把对应配方标记为「不可用」而非报错
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl dnsutils iputils-ping netcat-openbsd nmap file binutils \
      unzip p7zip-full \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md /app/
COPY ctfagent /app/ctfagent
COPY agents /app/agents
COPY tools /app/tools
COPY skills /app/skills
COPY web /app/web
COPY config.example.yaml /app/config.example.yaml
COPY run.py /app/run.py

RUN pip install --upgrade pip && pip install -e .

# 数据与工作区：用卷挂载以保证持久化
RUN mkdir -p /app/data /app/workspace
VOLUME ["/app/data", "/app/workspace"]

EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/api/health',timeout=3)"

# 默认以 CLI 模式运行；docker-compose 会覆盖为 server
CMD ["python", "-m", "ctfagent.server", "--host", "0.0.0.0", "--port", "8080"]